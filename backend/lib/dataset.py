import collections
import hashlib
import random
import typing
import json
import time
import re

from pathlib import Path
from csv import DictWriter

import config
import backend
from backend.lib.job import Job, JobNotFoundException


class DataSet:
	"""
	Provide interface to safely register and run operations on a dataset

	A dataset is a collection of:
	- A unique identifier
	- A set of parameters that demarcate the data contained within
	- The data

	The data is usually stored in a file on the disk; the parameters are stored
	in a database. The handling of the data, et cetera, is done by other
	workers; this class defines method to create and manipulate the dataset's
	properties.x
	"""
	db = None
	data = None
	folder = None
	parameters = {}
	is_new = True

	children = []
	processors = {}
	genealogy = []

	def __init__(self, parameters={}, key=None, job=None, data=None, db=None, parent=None, extension="csv",
				 type="search"):
		"""
		Create new dataset object

		If the dataset is not in the database yet, it is added.

		:param parameters:  Parameters, e.g. search query, date limits, et cetera
		:param db:  Database connection
		"""
		self.db = db
		self.folder = Path(config.PATH_ROOT, config.PATH_DATA)

		if key is not None:
			self.key = key
			current = self.db.fetchone("SELECT * FROM queries WHERE key = %s", (self.key,))
			if not current:
				raise TypeError("DataSet() requires a valid dataset key for its 'key' argument, \"%s\" given" % key)

			self.dataset = current["query"]
		elif job is not None:
			current = self.db.fetchone("SELECT * FROM queries WHERE parameters::json->>'job' = %s", (job,))
			if not current:
				raise TypeError("DataSet() requires a valid job ID for its 'job' argument")

			self.dataset = current["query"]
			self.key = current["key"]
		elif data is not None:
			current = data
			if "query" not in data or "key" not in data or "parameters" not in data or "key_parent" not in data:
				raise ValueError("DataSet() requires a complete dataset record for its 'data' argument")

			self.dataset = current["query"]
			self.key = current["key"]
		else:
			if parameters is None:
				raise TypeError("DataSet() requires either 'key', or 'parameters' to be given")

			self.dataset = self.get_label(parameters, default=type)
			self.key = self.get_key(self.dataset, parameters, parent)
			current = self.db.fetchone("SELECT * FROM queries WHERE key = %s AND query = %s", (self.key, self.dataset))

		if current:
			self.data = current
			self.parameters = json.loads(self.data["parameters"])
			self.is_new = False
		else:
			self.data = {
				"key": self.key,
				"query": self.get_label(parameters, default=type),
				"parameters": json.dumps(parameters),
				"result_file": "",
				"status": "",
				"type": type,
				"timestamp": int(time.time()),
				"is_finished": False,
				"num_rows": 0
			}
			self.parameters = parameters

			if parent:
				self.data["key_parent"] = parent

			self.db.insert("queries", data=self.data)
			self.reserve_result_file(parameters, extension)

		# retrieve analyses and processors that may be run for this dataset
		analyses = self.db.fetchall("SELECT * FROM queries WHERE key_parent = %s ORDER BY timestamp ASC", (self.key,))
		self.children = [DataSet(data=analysis, db=self.db) for analysis in analyses]
		self.processors = self.get_available_processors()

	def check_dataset_finished(self):
		"""
		Checks if dataset is finished. Returns path to results file is not empty,
		or 'empty_file' when there were not matches.

		Only returns a path if the dataset is complete. In other words, if this
		method returns a path, a file with the complete results for this dataset
		will exist at that location.

		:return: A path to the results file, 'empty_file', or `None`
		"""
		if self.data["is_finished"] and self.data["num_rows"] > 0:
			return self.folder.joinpath(self.data["result_file"])
		elif self.data["is_finished"] and self.data["num_rows"] == 0:
			return 'empty'
		else:
			return None

	def get_results_path(self):
		"""
		Get path to results file

		Always returns a path, that will at some point contain the dataset
		data, but may not do so yet. Use this to get the location to write
		generated results to.

		:return Path:  A path to the results file
		"""
		return self.folder.joinpath(self.data["result_file"])

	def get_temporary_path(self):
		"""
		Get path to a temporary folder

		This folder must be created before use, but is guaranteed to not exist
		yet. The folder may be used as a staging area for the dataset data
		while it is being processed.

		:return Path:  Path to folder
		"""
		results_file = self.get_results_path()

		results_dir_base = Path(results_file.parent)
		results_dir = results_file.name.replace(".", "") + "-staging"
		results_path = results_dir_base.joinpath(results_dir)
		index = 1
		while results_path.exists():
			results_path = results_dir_base.joinpath(results_dir + "-" + str(index))
			index += 1

		# create temporary folder
		return results_path

	def get_results_dir(self):
		"""
		Get path to results directory

		Always returns a path, that will at some point contain the dataset
		data, but may not do so yet. Use this to get the location to write
		generated results to.

		:return str:  A path to the results directory
		"""
		return self.folder

	def finish(self, num_rows=0):
		"""
		Declare the dataset finished
		"""
		if self.data["is_finished"]:
			raise RuntimeError("Cannot finish a finished dataset again")

		self.db.update("queries", where={"key": self.data["key"]},
					   data={"is_finished": True, "num_rows": num_rows})
		self.data["is_finished"] = True
		self.data["num_rows"] = num_rows

	def delete(self):
		"""
		Delete the dataset, and all its children

		Deletes both database records and result files. Note that manipulating
		a dataset object after it has been deleted is undefined behaviour.
		"""
		# first, recursively delete children
		children = self.db.fetchall("SELECT * FROM queries WHERE key_parent = %s", (self.key,))
		for child in children:
			child = DataSet(key=child["key"], db=self.db)
			child.delete()

		# delete from database
		self.db.execute("DELETE FROM queries WHERE key = %s", (self.key,))

		# delete from drive
		try:
			self.get_results_path().unlink()
		except FileNotFoundError:
			# already deleted, apparently
			pass

	def is_finished(self):
		"""
		Check if dataset is finished
		:return bool:
		"""
		return self.data["is_finished"] is True

	def get_parameters(self):
		"""
		Get dataset parameters

		The dataset parameters are stored as JSON in the database - parse them
		and return the resulting object

		:return:  Dataset parameters as originally stored
		"""
		try:
			return json.loads(self.data["parameters"])
		except json.JSONDecodeError:
			return {}

	def get_label(self, parameters=None, default="Query"):
		if not parameters:
			parameters = self.parameters

		if "body_query" in parameters and parameters["body_query"] and parameters["body_query"] != "empty":
			return parameters["body_query"]
		elif "body_match" in parameters and parameters["body_match"] and parameters["body_match"] != "empty":
			return parameters["body_match"]
		elif "subject_query" in parameters and parameters["subject_query"] and parameters["subject_query"] != "empty":
			return parameters["subject_query"]
		elif "subject_match" in parameters and parameters["subject_match"] and parameters["subject_match"] != "empty":
			return parameters["subject_match"]
		elif "country_flag" in parameters and parameters["country_flag"] and parameters["country_flag"] != "all":
			return "Flag: %s" % parameters["country_flag"]
		elif "country_code" in parameters and parameters["country_code"] and parameters["country_code"] != "all":
			return "Country: %s" % parameters["country_code"]
		elif "filename" in parameters and parameters["filename"]:
			return parameters["filename"]
		else:
			return default

	def reserve_result_file(self, parameters=None, extension="csv"):
		"""
		Generate a unique path to the results file for this dataset

		This generates a file name for the data file of this dataset, and makes sure
		no file exists or will exist at that location other than the file we
		expect (i.e. the data for this particular dataset).

		:param str extension: File extension, "csv" by default
		:param parameters:  Dataset parameters
		:return bool:  Whether the file path was successfully reserved
		"""
		if self.data["is_finished"]:
			raise RuntimeError("Cannot reserve results file for a finished dataset")

		# Use 'random' for random post queries
		if "random_amount" in parameters and parameters["random_amount"] > 0:
			file = 'random-' + str(parameters["random_amount"]) + '-' + self.data["key"]
		# Use country code for country flag queries
		elif "country_flag" in parameters and parameters["country_flag"] != 'all':
			file = 'countryflag-' + str(parameters["country_flag"]) + '-' + self.data["key"]
		# Use the query string for all other queries
		else:
			query_bit = self.data["query"].replace(" ", "-").lower()
			query_bit = re.sub(r"[^a-z0-9\-]", "", query_bit)
			query_bit = query_bit[:100] # Crop to avoid OSError
			file = query_bit + "-" + self.data["key"]
			file = re.sub(r"[-]+", "-", file)

		path = self.folder.joinpath(file + "." + extension.lower())
		index = 1
		while path.is_file():
			path = self.folder.joinpath(file + "-" + str(index) + "." + extension.lower())
			index += 1

		file = path.name
		updated = self.db.update("queries", where={"query": self.data["query"], "key": self.data["key"]},
								 data={"result_file": file})
		self.data["result_file"] = file
		return updated > 0

	def get_key(self, query, parameters, parent=""):
		"""
		Generate a unique key for this dataset that can be used to identify it

		The key is a hash of a combination of the query string and parameters.
		You never need to call this, really: it's used internally.

		:param str query:  Query string
		:param parameters:  Dataset parameters
		:param parent: Parent dataset's key (if applicable)

		:return str:  Dataset key
		"""
		# Return a unique key if random posts are queried
		if parameters.get("random_amount", None):
			random_int = str(random.randint(1, 10000000))
			return hashlib.md5(random_int.encode("utf-8")).hexdigest()

		# Return a hash based on parameters for other datasets
		else:
			# we're going to use the hash of the parameters to uniquely identify
			# the dataset, so make sure it's always in the same order, or we might
			# end up creating multiple keys for the same dataset if python
			# decides to return the dict in a different order
			param_key = collections.OrderedDict()
			for key in sorted(parameters):
				param_key[key] = parameters[key]

			parent_key = str(parent) if parent else ""
			plain_key = repr(param_key) + str(query) + parent_key
			return hashlib.md5(plain_key.encode("utf-8")).hexdigest()

	def get_status(self):
		"""
		Get Dataset status

		:return string: Dataset status
		"""
		return self.data["status"]

	def update_status(self, status):
		"""
		Update dataset status

		The status is a string that may be displayed to a user to keep them
		updated and informed about the progress of a dataset. No memory is kept
		of earlier dataset statuses; the current status is overwritten when
		updated.

		:param string status:  Dataset status
		:return bool:  Status update successful?
		"""
		self.data["status"] = status
		updated = self.db.update("queries", where={"key": self.data["key"]}, data={"status": status})

		return updated > 0

	def update_version(self, version):
		"""
		Update software version used for this dataset

		This can be used to verify the code that was used to process this dataset.

		:param string version:  Version identifier
		:return bool:  Update successul?
		"""
		self.data["software_version"] = version
		updated = self.db.update("queries", where={"key": self.data["key"]}, data={"software_version": version})

		return updated > 0

	def get_version_url(self, file):
		"""
		Get a versioned github URL for the version this dataset was processed with

		:param file:  File to link within the repository
		:return:  URL, or an empty string
		"""
		if not self.data["software_version"] or not config.GITHUB_URL:
			return ""

		return config.GITHUB_URL + "/blob/" + self.data["software_version"] + "/" + file

	def write_csv_and_finish(self, data):
		"""
		Write data as csv to results file and finish dataset

		Determines result file path using dataset's path determination helper
		methods. After writing results, the dataset is marked finished.

		:param data: A list or tuple of dictionaries, all with the same keys
		"""
		if not (isinstance(data, typing.List) or isinstance(data, typing.Tuple)) or isinstance(data, str):
			raise TypeError("write_as_csv requires a list or tuple of dictionaries as argument")

		if not data:
			raise ValueError("write_as_csv requires a dictionary with at least one item")

		if not isinstance(data[0], dict):
			raise TypeError("write_as_csv requires a list or tuple of dictionaries as argument")

		self.update_status("Writing results file")
		with self.get_results_path().open("w", encoding="utf-8", newline='') as results:
			writer = DictWriter(results, fieldnames=data[0].keys())
			writer.writeheader()

			for row in data:
				writer.writerow(row)

		self.update_status("Finished")
		self.finish(len(data))

	def top_key(self):
		"""
		Get key of root dataset

		Traverses the tree of datasets this one is part of until it finds one
		with no parent dataset, then returns that dataset's key.

		Not to be confused with top kek.

		:return str: Parent key.
		"""
		genealogy = self.get_genealogy()
		return genealogy[0].key

	def get_genealogy(self):
		"""
		Get genealogy of this dataset

		Creates a list of DataSet objects, with the first one being the
		'top' dataset, and each subsequent one being a child of the previous
		one, ending with the current dataset.

		:return list:  Dataset genealogy, oldest dataset first
		"""
		if self.genealogy or not self.key_parent:
			return self.genealogy

		key_parent = self.key_parent
		genealogy = []

		while True:
			try:
				parent = DataSet(key=key_parent, db=self.db)
			except TypeError:
				break

			genealogy.append(parent)
			if parent.key_parent:
				key_parent = parent.key_parent
			else:
				break

		genealogy.reverse()
		genealogy.append(self)
		self.genealogy = genealogy
		return self.genealogy

	def get_breadcrumbs(self):
		"""
		Get breadcrumbs navlink for use in permalinks

		Returns a string representing this dataset's genealogy that may be used
		to uniquely identify it.

		:return str: Nav link
		"""
		genealogy = self.get_genealogy()

		return ",".join([dataset.key for dataset in genealogy])

	def get_compatible_processors(self):
		"""
		Get list of processors compatible with this dataset

		Checks whether this dataset type is one that is listed as being accepted
		by the processor, for each known type: if the processor does
		not specify accepted types (via the `accepts` attribute of the class),
		it is assumed it accepts 'search' datasets as an input.

		:return dict:  Compatible processors, `name => properties` mapping
		"""
		processors = backend.all_modules.processors

		available = collections.OrderedDict()
		for processor in processors.values():
			if (self.data["type"] == "search" and not processor["accepts"] and (
					not processor["datasources"] or self.parameters.get("datasource") in processor["datasources"])) or \
					self.data["type"] in processor["accepts"]:
				available[processor["id"]] = processor

		return available

	def get_available_processors(self):
		"""
		Get list of processors that may be run for this dataset

		Returns all compatible processors except for those that are already
		queued or finished and have no options. Processors that have been
		run but have options are included so they may be run again with a
		different configuration

		:return dict:  Available processors, `name => properties` mapping
		"""
		processors = self.get_compatible_processors()

		for analysis in self.children:
			if analysis.type not in processors:
				continue

			if not processors[analysis.type]["options"]:
				del processors[analysis.type]

		return processors

	def link_job(self, job):
		"""
		Link this dataset to a job ID

		Updates the dataset data to include a reference to the job that will be
		executing (or has already executed) this job.

		Note that if no job can be found for this dataset, this method silently
		fails.

		:param Job job:  The job that will run this dataset

		:todo: If the job column ever gets used, make sure it always contains
		       a valid value, rather than silently failing this method.
		"""
		if type(job) != Job:
			raise TypeError("link_job requires a Job object as its argument")

		if "id" not in job.data:
			try:
				job = Job.get_by_remote_ID(self.key, self.db, jobtype=self.data["type"])
			except JobNotFoundException:
				return

		self.db.update("queries", where={"key": self.key}, data={"job": job.data["id"]})

	def __getattr__(self, attr):
		"""
		Getter so we don't have to use .data all the time

		:param attr:  Data key to get
		:return:  Value
		"""
		if attr in self.data:
			return self.data[attr]
		else:
			print(self.data)
			raise KeyError("DataSet instance has no attribute %s" % attr)