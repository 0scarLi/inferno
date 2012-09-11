import logging
import sys
import time
import ujson
import urllib2
import urllib
import datetime
import pprint

from disco.core import Params
from disco.job import Job

from inferno.lib.archiver import Archiver
from inferno.lib.disco_ext import get_disco_handle
from inferno.lib.job_options import JobOptions
from inferno.lib.result import reduce_result

log = logging.getLogger(__name__)

JOB_ARCHIVE = 'job.archive'
JOB_CLEANUP = 'job.cleanup'
JOB_BLOBS = 'job.blobs'
JOB_DONE = 'job.done'
JOB_PROCESS = 'job.process'
JOB_PROFILE = 'job.profile'
JOB_PURGE = 'job.purge'
JOB_RESULTS = 'job.results'
JOB_RUN = 'job.run'
JOB_START = 'job.start'
JOB_TAG = 'job.tag'
JOB_WAIT = 'job.wait'
JOB_ERROR = 'job.error'

class InfernoJob(object):

    def __init__(self, rule, settings, parent=None):
        self.disco, self.ddfs = get_disco_handle(settings.get('server'))
        self.job_options = JobOptions(rule, settings)
        self.rule = rule
        self.settings = settings
        rule_params = dict(rule.params.__dict__)
        rule_params.update(settings)
        self.params = Params(**rule_params)

        try:
            # attempt to allow for overriden worker class from settings file
            worker_mod, dot, worker_class = settings.get('worker').rpartition('.')
            mod = __import__(worker_mod, {}, {}, worker_mod)
            worker = getattr(mod, worker_class)()
            self.job = Job(name=rule.name,
                master=self.disco.master,
                worker=worker)
        except Exception as e:
            log.warn("Error instantiating worker: %s %s - loading default worker"
                        % (settings.get('worker'), e))
            self.job = Job(name=rule.name,
                master=self.disco.master)
        self.full_job_id = None
        self.parent = parent
        self.jobinfo = None
        self._update_state(JOB_START)

    @property
    def job_name(self):
        return self.job.name

    @property
    def rule_name(self):
        return self.rule.qualified_name

    @property
    def job_msg(self):
        return dict(
            job_name=self.job_name,
            rule_name=self.rule_name,
            settings=self.settings,
            child_data=self.jobinfo,
            current_stage=self.current_stage,
        )

    def start(self):
        # process the map-results option (ie. skip map phase and grab map results from job id/ddfs
        self.archiver = self._determine_job_blobs()
        job_blobs = self.archiver.job_blobs
        map_function = self.rule.map_function
        self.start_time = time.time()
        if self.settings.get('just_query'):
            self.query()
            return None
        if self._enough_blobs(self.archiver.blob_count):
            if self.rule.rule_init_function:
                self.rule.rule_init_function(self.params)
            self.job.run(name=self.rule.name,
                  input=job_blobs,
                  map=map_function,
                  reduce=self.rule.reduce_function,
                  params=self.params,
                  partitions=self.rule.partitions,
                  map_input_stream=self.rule.map_input_stream,
                  map_output_stream=self.rule.map_output_stream,
                  map_init=self.rule.map_init_function,
                  save=self.rule.save or self.rule.result_tag is not None,
                  scheduler=self.rule.scheduler,
                  reduce_output_stream=self.rule.reduce_output_stream,
                  sort=self.rule.sort,
                  profile=self.settings.get('profile'),
                  partition=self.rule.partition_function,
                  required_files=self.rule.required_files,
                  required_modules=self.rule.required_modules)

            # actual id is only assigned after starting the job
            self.full_job_id = self.job.name
            return self.job
        return None

    def query(self):
        log.info ("Query information:")
        pprint.pprint({'source query': self.archiver.tags,
                       'tag results': self.archiver.tag_map,
                       'total_blobs': self.archiver.blob_count})

    def _safe_str(self, value):
        try:
            return str(value)
        except UnicodeEncodeError:
            return unicode(value).encode('utf-8')

    def wait(self):
        blob_count = self.archiver.blob_count
        log.info('Started job %s processing %i blobs',
            self.job.name, blob_count)
        self._notify_parent(JOB_WAIT)
        try:
            jobout = self.job.wait()
            log.info('Done waiting for job %s', self.job.name)
            results = self._get_job_results(jobout)
            self._profile(self.job)
            self._tag_results(self.job.name)
            if not self.settings.get('debug'):
                self._process_results(results, self.job.name)
            else:
                reduce_result(results)
            self._purge(self._safe_str(self.job.name))
        except Exception as e:
            log.error('Job %s failed: %s',
                      self.job.name, e, exc_info=sys.exc_info())
            self._notify_parent(JOB_ERROR)
        else:
            if not self.settings.get('debug'):
                self._archive_tags(self.archiver)
            if self.rule.rule_cleanup:
                self._notify_parent(JOB_CLEANUP)
                self.rule.rule_cleanup(self, )
            self._notify_parent(JOB_DONE)
        log.info('Finished job %s', self.job.name)

    def _determine_job_blobs(self):
        self._update_state(JOB_BLOBS)
        tags = self.job_options.tags
        urls = self.job_options.urls
        log.info('Processing tags: %s', tags)
        archiver = Archiver(
            ddfs=self.ddfs,
            archive_prefix=self.rule.archive_tag_prefix,
            archive_mode=self.rule.archive,
            max_blobs=self.rule.max_blobs,
            tags=tags,
            urls=urls)
        return archiver

    def _get_job_results(self, jobout):
        if self.rule.result_iterator:
            self._notify_parent(JOB_RESULTS)
            return self.rule.result_iterator(jobout)

    def _profile(self, job):
        if self.settings.get('profile'):
            self._notify_parent(JOB_PROFILE)
            job.profile_stats().sort_stats('cumulative').print_stats()

    def _tag_results(self, job_name):
        if self.job_options.result_tag:
            self._notify_parent(JOB_TAG)
            result_name = 'disco:job:results:%s' % job_name
            tag_name = '%s:%s' % (self.job_options.result_tag, job_name)
            log.info('Tagging result: %s', tag_name)
            try:
                self.ddfs.tag(tag_name, list(self.ddfs.blobs(result_name)))
            except Exception as e:
                log.error('Error tagging result %s: %s',
                    tag_name, e, exc_info=sys.exc_info())

    def _process_results(self, results, job_id):
        if self.rule.result_processor:
            self._notify_parent(JOB_PROCESS)
            self.rule.result_processor(
                results, params=self.params, job_id=job_id)

    def _purge(self, job_name):
        if not self.settings.get('no_purge'):
            self._notify_parent(JOB_PURGE)
            self.disco.purge(job_name)

    def _archive_tags(self, archiver):
        if archiver.archive_mode:
            self._notify_parent(JOB_ARCHIVE)
            archiver.archive()

    def _update_state(self, stage):
        try:
            self.jobinfo = self.job.jobinfo()
        except:
            pass
        self.current_stage = stage

    def _notify_parent(self, stage):
        self._update_state(stage)
        # if we are daemon spawn, tell mommy where we are
        if self.parent and self.full_job_id:
            try:
                msg = ujson.dumps(self.job_msg)
                msg = urllib.urlencode([('msg', msg)])
                url = '%s/_status/%s' % (self.parent, self.full_job_id)
                urllib2.urlopen(url, data=msg) #, timeout=5)
            except Exception as e:
                import traceback
                trace = traceback.format_exc(15)
                log.error("Error communicating with parent: %s stage= %s\n%s" % (e, stage, trace))

    def _enough_blobs(self, blob_count):
        if not blob_count or (blob_count < self.rule.min_blobs and
                              not self.settings.get('force')):
            log.info('Skipping job %s: %d blobs required, have only %d',
                self.rule.name, self.rule.min_blobs, blob_count)
            return False
        return True

    def __str__(self):
        return '<InfernoJob for: %s>' % self.rule.name
