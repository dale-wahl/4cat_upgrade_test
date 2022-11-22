""" 
Script for importing a 4chan posts csv generated by 4CAT itself.
Useful to import datasets from other 4CAT instances.

psql command to export and compress a csv from 4CAT:
psql -d fourcat -c "COPY (SELECT *, timestamp_deleted FROM posts_4chan LEFT JOIN posts_4chan_deleted ON posts_4chan.id_seq = posts_4chan_deleted.id_seq WHERE board='BOARD') TO stdout WITH HEADER CSV DELIMITER ',';" | gzip > /path/file.csv.gz
"""

import argparse
import json
import time
import csv
import sys
import os
import re

from pathlib import Path

from psycopg2.errors import UniqueViolation

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)) + "/..")
from common.lib.database import Database
from common.lib.logger import Logger

def commit(posts, post_fields, db, datasource, fast=False):
	posts_added = 0
	
	if fast:
		post_fields_sql = ", ".join(post_fields)
		try:
			db.execute_many("INSERT INTO posts_" + datasource + " (" + post_fields_sql + ") VALUES %s", posts)
			db.commit()
		except psycopg2.IntegrityError as e:
			print(repr(e))
			print(e)
			sys.exit(1)
		posts_added = len(posts)

	else:

		db.execute("START TRANSACTION")
		for post in posts:
			new_post = db.insert("posts_" + datasource, data={post_fields[i]: post[i] for i in range(0, len(post))}, safe=True, constraints=("id", "board"), commit= False)
			if new_post:
				posts_added += 1

		db.commit()

	return posts_added

# parse parameters
cli = argparse.ArgumentParser()
cli.add_argument("-i", "--input", required=True, help="File to read from, containing a CSV dump")
cli.add_argument("-d", "--datasource", type=str, required=True, help="Datasource ID")
cli.add_argument("-a", "--batch", type=int, default=1000000,
				 help="Size of post batches; every so many posts, they are saved to the database")
cli.add_argument("-b", "--board", type=str, required=True, help="Board name")
cli.add_argument("-s", "--skip", type=int, default=0, help="How many posts to skip")
cli.add_argument("-e", "--end", type=int, default=sys.maxsize,
				 help="At which post to stop processing. Starts counting at 0 (so not affected by --skip)")
cli.add_argument("-f", "--fast", default=False, type=bool,
				 help="Use batch queries instead of inserting posts individually. This is far faster than 'slow' mode, "
					  "but will crash if trying to insert a duplicate post, so it should only be used on an empty "
					  "database or when you're sure datasets don't overlap.")
args = cli.parse_args()

if not Path(args.input).exists() or not Path(args.input).is_file():
	print("%s is not a valid file name." % args.input)
	sys.exit(1)

print("Opening %s." % args.input)

if args.skip > 0:
	print("Skipping %i posts." % args.skip)

if args.fast:
	print("Fast mode enabled.")

logger = Logger()
db = Database(logger=logger, appname="queue-dump")

csvnone = re.compile(r"^N$")

thread_keys = set()

deleted_ids = set() # We insert deleted posts separately because we
				# need their `id_seq` for the posts_{datasource}_deleted table

post_fields = ("id", "board", "thread_id", "timestamp", "subject", "body", "author", "author_type", "author_type_id", "author_trip", "country_code", "country_name", "image_file", "image_url", "image_4chan", "image_md5", "image_dimensions", "image_filesize", "semantic_url", "unsorted_data")


with open(args.input, encoding="utf-8") as inputfile:

	reader = csv.DictReader(inputfile)
	fieldnames = reader.fieldnames
	
	# Skip headers
	next(reader, None)

	postbuffer = []
	threads = {}
	posts = 0
	threads_added = 0
	posts_added = 0

	# We keep count of what threads we have last encounterd.
	# This is done to prevent RAM hogging: we're inserting threads
	# we haven't seen in a while to the db and removing them from this dict. 
	threads_last_seen = {}

	for post in reader:

		posts += 1

		# Skip rows if needed. Can be useful when importing didn't go correctly.
		if posts < args.skip:
			if posts % 1000000 == 0:
				print("Skipped %s/%s rows (%.2f%%)..." % (posts, args.skip, (posts / args.skip) * 100))
			continue

		if posts >= args.end:
			break

		# Sanitise post data
		post = {k: csvnone.sub("", post[k]) if post[k] else "" for k in post}

		# We collect thread data first
		if post["thread_id"] not in threads:
			threads_added += 1
			thread_keys.add(int(post["thread_id"]))
			thread = {
				"id": post["thread_id"],
				"board": post["board"],
				"timestamp": 0,
				"timestamp_scraped": int(time.time()),
				"timestamp_modified": 0,
				"timestamp_deleted": 0,
				"num_unique_ips": -1,
				"num_images": 0,
				"num_replies": 0,
				"limit_bump": False,
				"limit_image": False,
				"is_sticky": False,
				"is_closed": False,
				"post_last": 0
			}
		else:
			thread = threads[post["thread_id"]]

		# Keep track of some OP data
		if post["thread_id"] == post["id"]:
			thread["timestamp"] = int(post["timestamp"])
			# Mark thread as deleted if the OP was removed
			if post.get("timestamp_deleted"):
				thread["timestamp_deleted"] = max(int(thread.get("timestamp_modified") or 0), int(post["timestamp"]))

		if post["image_file"]:
			thread["num_images"] += 1

		thread["num_replies"] += 1
		thread["post_last"] = max(int(thread.get("post_last") or 0), int(post["id"]))
		thread["timestamp_modified"] = max(int(thread.get("timestamp_modified") or 0), int(post["timestamp"]))

		threads[post["thread_id"]] = thread

		# If the post is deleted, we're going to add it to the post_{datasource}_deleted table
		# which is used to filter out deleted posts.
		if post.get("timestamp_deleted"):
			deleted_ids.add(int(post["id"]))

		postdata = (
			post["id"],
			post["board"],
			post["thread_id"],
			post["timestamp"],
			post["subject"],
			post["body"],
			post["author"],
			post["author_type"],
			post["author_type_id"],
			post["author_trip"],
			post["country_code"],
			post["country_name"],
			post["image_file"],
			post["image_url"],
			post["image_4chan"],
			post["image_md5"],
			post["image_dimensions"],
			post["image_filesize"],
			post["semantic_url"],
			post["unsorted_data"]
		)
		
		postbuffer.append(postdata)

		# For speed, we only commit every so many posts
		if len(postbuffer) % args.batch == 0:
			new_posts = commit(postbuffer, post_fields, db, args.datasource, fast=args.fast)
			posts_added += new_posts
			print("Row %i - %i. %i new posts added." % (posts - args.batch, posts, posts_added))
			postbuffer = []

# Add the last posts and threads as well
print("Commiting leftover posts")
commit(postbuffer, post_fields, db, args.datasource, fast=args.fast)

# Insert deleted posts, and get their id_seq to use in the posts_{datasource}_deleted table
if deleted_ids:
	print("\nAlso committing %i deleted posts to posts_%s_deleted table." % (len(deleted_ids), args.datasource))
	for deleted_id in deleted_ids:
		result = db.fetchone("SELECT id_seq, timestamp FROM posts_" + args.datasource + " WHERE id = %s AND board = '%s' " % (deleted_id, args.board))
		db.insert("posts_" + args.datasource + "_deleted", {"id_seq": result["id_seq"], "timestamp_deleted": result["timestamp"]}, safe=True)
	db.commit()

# update threads
print("Updating threads.")

for thread_id in threads:
	thread = threads[thread_id]

	exists = db.fetchone("SELECT * FROM threads_" + args.datasource + " WHERE id = %s AND board = %s", (thread_id, args.board,))

	if not exists:
		db.insert("threads_" + args.datasource, thread)

	# We don't know if we have all the thread data here (things might be cutoff)
	# so do some quick checks if values are higher/newer than before
	else:
		thread["is_sticky"] = True if (exists["is_sticky"] or thread["is_sticky"]) else False
		thread["is_closed"] = True if (exists["is_sticky"] or thread["is_sticky"]) else False
		thread["post_last"] = max(int(thread.get("post_last") or 0), int(exists.get("post_last") or 0))
		thread["timestamp_deleted"] = max(int(thread.get("timestamp_deleted") or 0), int(exists.get("timestamp_deleted") or 0))
		thread["timestamp_archived"] = max(int(thread.get("timestamp_archived") or 0), int(exists.get("timestamp_archived") or 0))
		thread["timestamp_modified"] = max(int(thread.get("timestamp_modified") or 0), int(exists.get("timestamp_modified") or 0))
		thread["timestamp"] = min(int(thread.get("timestamp") or 0), int(exists.get("timestamp") or 0))

		db.update("threads_" + args.datasource, data=thread, where={"id": thread_id, "board": args.board})
	db.commit()

print("Updating thread statistics.")
db.execute(
	"UPDATE threads_" + args.datasource + " AS t SET num_replies = ( SELECT COUNT(*) FROM posts_" + args.datasource + " AS p WHERE p.thread_id = t.id) WHERE t.id IN %s AND board = %s",
	(tuple(thread_keys), args.board,))
db.execute(
	"UPDATE threads_" + args.datasource + " AS t SET num_images = ( SELECT COUNT(*) FROM posts_" + args.datasource + " AS p WHERE p.thread_id = t.id AND image_file != '') WHERE t.id IN %s AND board = %s",
	(tuple(thread_keys), args.board,))

db.commit()

print("Done")