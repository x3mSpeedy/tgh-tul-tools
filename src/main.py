#!/usr/bin/python
# -*- coding: utf-8 -*-
# author:   Jan Hybs

import os, sys, time, logging, json

from simplejson import JSONEncoder
from jobs.job_control import JobControl, JobCode
from jobs.job_request import JobRequest
from utils import plucklib

from utils.daemon import Daemon
from utils.globals import Langs, Problems, ProcessException, Config, ensure_path
from utils.logger import Logger
from subprocess import call

from config import runner_pidfile, runner_sleep

# utf hax
# reload(sys)
# sys.setdefaultencoding("utf-8")


class TGHProcessor(Daemon):
    def __init__(self, config_json=None):
        super(TGHProcessor, self).__init__(name='tgh-processor', pidfile=runner_pidfile)

        if not config_json:
            return

        with open(config_json, 'r') as fp:
            self.config = json.load(fp)

        Config.watch_dir = self.config['jobs']
        Config.problems = self.config['problems']
        Config.data = self.config['data']
        Config.config_dir = self.config['config']
        Config.log_file = self.config['log_file']

        logging.root.setLevel(logging.INFO)
        Logger._global_logger = Logger(
            name='ROOT',
            stream_level=logging.DEBUG,
            file_level=logging.INFO,
            fmt=Logger.default_format,
            log_file=Config.log_file
        )
        Logger.instance().info('Logging on')

    @staticmethod
    def get_jobs():
        """
        :rtype : list[jobs.job_request.JobRequest]
        """
        jobs = os.listdir(Config.watch_dir)
        jobs = [j for j in jobs if j.startswith('job-')]
        jobs = [os.path.join(Config.watch_dir, j) for j in jobs]
        jobs = [j for j in jobs if os.path.isdir(j)]
        jobs = [j for j in jobs if 'config.json' in os.listdir(j)]
        jobs = [j for j in jobs if '.delete-me' in os.listdir(j)]
        
        # reload config file if there are jobs
        if jobs:
            Langs.reload()
            Problems.reload()

        json_jobs = [JobRequest(os.path.join(j, 'config.json')) for j in jobs]

        return json_jobs

    def run(self):
        Langs.init(os.path.join(Config.config_dir, 'langs.yaml'))
        Problems.init(os.path.join(Config.config_dir, 'problems.yaml'))

        while True:
            jobs = self.get_jobs()
            if jobs:
                Logger.instance().info('{} job/s found'.format(len(jobs)))
                for job in jobs:
                    Logger.instance().info('Processing {}'.format(job))
                    try:
                        # delete file to let PHP now we are working on it
                        os.unlink(job.delete_me_file)
                        result = JobControl.process(job)
                    except ProcessException as e:
                        result = e.info
                    except Exception as e:
                        result = dict(
                            result=JobCode.UNKNOWN_ERROR,
                            error=str(e),
                        )

                    # add info about result
                    self.save_result(job, result)
            else:
                Logger.instance().debug('no jobs found')
            time.sleep(runner_sleep)

    def save_result(self, job, result):
        """
        :type result: list[jobs.job_control.CaseResult] or jobs.job_control.CaseResult
        """
        user_dir = os.path.join(Config.data, job.nameuser, job.problem.id)
        ensure_path(user_dir, is_file=False)

        # get max status from all cases (excluding global timeout and skipped)
        max_status = self.get_max_result(result)
        Logger.instance().info('Max status = {}({})'.format(repr(max_status), max_status()))

        # prepare user directory
        attempts = os.listdir(user_dir)
        attempt_no = [int(x.split("-")[0]) for x in attempts] or [0]
        next_attempt = max(attempt_no) + 1
        attempt_dir = '{:02d}-{}-{}'.format(next_attempt, job.username, max_status.shortname)
        Logger.instance().info("Output dir will be named as {}".format(attempt_dir))

        # ensure it exists
        dest_dir = os.path.join(user_dir, attempt_dir)
        dest_output_dir = os.path.join(dest_dir)
        ensure_path(dest_output_dir, is_file=False)

        # set permission so PHP can delete files
        call(['chmod', '-R', '777', job.root])

        if type(result) is not list:
            Logger.instance().info(str(result))
            Logger.instance().info('Error during execution! ')
            Logger.instance().info(result.err_file.value())

            result_json = json.dumps(result.get_error(), indent=4, cls=MyEncoder, sort_keys=True)
            with open(job.result_file, 'w') as fp:
                fp.write(result_json)

            call(['cp', '-r', job.output_root, dest_output_dir])
            call(['cp', job.result_file, dest_dir])
            call(['cp', job.main_file, dest_dir])

            return None, None
        else:
            # confirm results
            [r.confirm(job, dest_dir) for r in result]

            summary = self.get_result_summary(job, result, next_attempt).encode('utf8')
            with open(os.path.join(dest_dir, 'result.txt'), 'wb') as fp:
                fp.write(summary)

            # create global result
            main_result = dict()
            main_result['summary'] = summary
            main_result['attempt_dir'] = dest_dir
            main_result['result'] = result
            main_result['max_result'] = max_status()
            main_result['max_result_str'] = max_status.longname

            # save results
            result_json = json.dumps(main_result, indent=4, cls=MyEncoder, sort_keys=True)
            with open(job.result_file, 'w') as fp:
                fp.write(result_json)

            # copy files
            call(['cp', '-r', job.output_root, dest_output_dir])
            call(['cp', job.result_file, dest_dir])
            call(['cp', job.main_file, dest_dir])

            # print summary into logger
            Logger.instance().info('Summary: \n{}'.format(summary))
            return summary, attempt_dir

    @classmethod
    def get_max_result(cls, result):
        """
        Extract result from all cases - GLOBAL_TIMEOUT and SKIPPED and
        return max value (or UNKNOWN_ERROR if nothings left)
        :rtype: jobs.job_control.JobCode.L
        """
        try:
            # all results
            results = {x for x in plucklib.pluck(result, 'result')}
            Logger.instance().info('Statuses = {}'.format(results))

            # filtered
            results -= {JobCode.GLOBAL_TIMEOUT, JobCode.SKIPPED}
            Logger.instance().info('Filtered = {}'.format(results))

            # max result
            max_result = max(results)
            Logger.instance().info('max = {}'.format(max_result))

        except Exception as e:
            Logger.instance().info('max_status exception = {}'.format(e))
            max_result = JobCode.UNKNOWN_ERROR
        return max_result

    @staticmethod
    def get_result_summary(job, results, attempt_no):
        """
        :param attempt_no: int
        :type results: list[jobs.job_control.CaseResult]
        :type job: jobs.job_request.JobRequest
        """
        summary = list()
        summary.append(u'{:15s}{job.problem.name} ({job.problem.id})'.format('uloha', job=job))
        summary.append(u'{:15s}{job.lang.name} ({job.lang.version})'.format('jazyk', job=job))
        summary.append(u'{:15s}{job.username}'.format('student', job=job))
        summary.append(u'{:15s}{job.timestamp}'.format('datum', job=job))
        summary.append(u'{:15s}{}.'.format('pokus', attempt_no))
        summary.append('')

        for r in results:

            # reference job
            if job.reference:
                info = u'  [{r.result.longname:^14s}] sada {r.case_id:<20s} {r.problem_size_str}{r.random_str}'.format(**locals())
                summary.append(u'{info:70s}{r.duration:10.3f} ms'.format(**locals()))

            # standard student job
            elif not job.reference:
                info = u'  [{r.result.longname:^14s}] sada {r.case_id:<20s}'.format(**locals())
                summary.append(u'{info:70s}{r.duration:10.3f} ms'.format(**locals()))

                # in case of wrong output print what went wrong
                if r.result is JobCode.WRONG_OUTPUT:
                    if r.problem.multiple_solution:
                        method = u'CHYBNY_VYSTUP na zaklade vysledku referencniho skriptu'
                    else:
                        method = u'CHYBNY_VYSTUP na zaklade porovnani souboru'

                    summary.append(u'{:19s}{method}'.format('', **locals()))

            # add more info if case went wrong
            if r.error:
                summary.append(u'{:19s}{r.error}'.format('', **locals()))

        summary.append(u'')
        summary.append(u'')

        # final message
        if max(plucklib.pluck(results, 'result')) <= JobCode.TIMEOUT_CORRECT_OUTPUT:
            summary.append(u'Odevzdane reseni je SPRAVNE')
        else:
            summary.append(u'Odevzdane reseni je CHYBNE')

        return u'\n'.join(summary)


def usage(msg=''):
    if msg:
        print(msg)
    print('usage: main.py start|stop|restart|debug <config.json>')
    sys.exit(1)


class MyEncoder(JSONEncoder):
    def default(self, o):
        try:
            return o.to_json()
        except:
            return str(o)

if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) < 1:
        usage('Specify action!')

    action = str(args[0]).lower()
    if action.lower() not in ('start', 'stop', 'restart', 'debug'):
        usage('Invalid action')

    if action in ('start', 'restart', 'debug'):
        if len(args) < 2: usage('Missing <config.json> arg')
        config_json = os.path.abspath(args[1])

        processor = TGHProcessor(config_json=config_json)
        if action == 'debug':
            Logger.instance().info('Debugging service')
            processor.run()
            sys.exit(0)

        if action == 'restart':
            Logger.instance().info('Stopping service...')
            processor.stop()
        Logger.instance().info('Watching dir "{:s}"'.format(Config.watch_dir))
        processor.start()
        sys.exit(0)

    if action == 'stop':
        processor = TGHProcessor()
        Logger.instance().info('Stopping service...')
        processor.stop()
        sys.exit(0)
