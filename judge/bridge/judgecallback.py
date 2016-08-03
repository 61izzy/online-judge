import logging
import os
import time

from django import db
from django.utils import timezone
from django.core.exceptions import ObjectDoesNotExist

from judge import event_poster as event
from judge.caching import finished_submission
from judge.models import Submission, SubmissionTestCase, Problem, Judge, Language, LanguageLimit, RuntimeVersion
from .judgehandler import JudgeHandler

logger = logging.getLogger('judge.bridge')

UPDATE_RATE_LIMIT = 5
UPDATE_RATE_TIME = 0.5
TIMER = [time.time, time.clock][os.name == 'nt']


def _ensure_connection():
    try:
        db.connection.cursor().execute('SELECT 1').fetchall()
    except Exception:
        db.connection.close()


class DjangoJudgeHandler(JudgeHandler):
    def __init__(self, server, socket):
        super(DjangoJudgeHandler, self).__init__(server, socket)

        # each value is (updates, last reset)
        self.update_counter = {}

    def on_close(self):
        super(DjangoJudgeHandler, self).on_close()
        if self._working:
            submission = Submission.objects.get(id=self._working)
            submission.status = 'IE'
            submission.save()

    def get_related_submission_data(self, submission):
        _ensure_connection()  # We are called from the django-facing daemon thread. Guess what happens.

        pid, time, memory, short_circuit, lid, is_pretested = Submission.objects.filter(id=submission).\
            values_list('problem__id', 'problem__time_limit', 'problem__memory_limit',
                        'problem__short_circuit', 'language__id', 'is_pretested')[0]

        try:
            limit = LanguageLimit.objects.get(problem__id=pid, language__id=lid)
        except LanguageLimit.DoesNotExist:
            pass
        else:
            time, memory = limit.time_limit, limit.memory_limit
        return time, memory, short_circuit, is_pretested

    def _authenticate(self, id, key):
        try:
            judge = Judge.objects.get(name=id)
        except Judge.DoesNotExist:
            return False
        return judge.auth_key == key

    def _connected(self):
        judge = Judge.objects.get(name=self.name)
        judge.start_time = timezone.now()
        judge.online = True
        judge.problems = Problem.objects.filter(code__in=self.problems.keys())
        judge.runtimes = Language.objects.filter(key__in=self.executors.keys())
        for lang in judge.runtimes.all():
            runtimes = []
            for idx, data in enumerate(self.executors[lang.key]):
                name, version = data
                runtimes.append(RuntimeVersion(language=lang, name=name, version='.'.join(map(str, version)),
                                               priority=idx, judge=judge))
            RuntimeVersion.objects.bulk_create(runtimes)
        judge.last_ip = self.client_address[0]
        judge.save()

    def _disconnected(self):
        Judge.objects.filter(name=self.name).update(online=False)
        RuntimeVersion.objects.filter(judge__name=self.name).delete()

    def _update_ping(self):
        try:
            Judge.objects.filter(name=self.name).update(ping=self.latency, load=self.load)
        except Exception as e:
            # What can I do? I don't want to tie this to MySQL.
            if e.__class__.__name__ == 'OperationalError' and e.__module__ == '_mysql_exceptions' and e.args[0] == 2006:
                db.connection.close()

    def on_submission_processing(self, packet):
        try:
            submission = Submission.objects.get(id=packet['submission-id'])
        except Submission.DoesNotExist:
            logger.warning('Unknown submission: %d', packet['submission-id'])
            return

        try:
            submission.judged_on = Judge.objects.get(name=self.name)
        except Judge.DoesNotExist:
            # Just in case. Is not necessary feature and is not worth the crash.
            pass

        submission.status = 'P'
        submission.save()
        event.post('sub_%d' % submission.id, {'type': 'processing'})
        if not submission.problem.is_public:
            return
        event.post('submissions', {'type': 'update-submission', 'id': submission.id,
                                   'state': 'processing', 'contest': submission.contest_key,
                                   'user': submission.user_id, 'problem': submission.problem_id})

    def on_grading_begin(self, packet):
        super(DjangoJudgeHandler, self).on_grading_begin(packet)
        try:
            submission = Submission.objects.get(id=packet['submission-id'])
        except Submission.DoesNotExist:
            logger.warning('Unknown submission: %d', packet['submission-id'])
            return
        submission.status = 'G'

        # Update pretest state now that we know for sure whether the problem has pretest data
        submission.is_pretested = packet['pretested']
        submission.current_testcase = 1
        submission.batch = False
        submission.save()
        SubmissionTestCase.objects.filter(submission_id=submission.id).delete()
        event.post('sub_%d' % submission.id, {'type': 'grading-begin'})
        if not submission.problem.is_public:
            return
        event.post('submissions', {'type': 'update-submission', 'id': submission.id,
                                   'state': 'grading-begin', 'contest': submission.contest_key,
                                   'user': submission.user_id, 'problem': submission.problem_id})

    def _submission_is_batch(self, id):
        submission = Submission.objects.get(id=id)
        submission.batch = True
        submission.save()

    def on_grading_end(self, packet):
        super(DjangoJudgeHandler, self).on_grading_end(packet)
        try:
            submission = Submission.objects.get(id=packet['submission-id'])
        except Submission.DoesNotExist:
            logger.warning('Unknown submission: %d', packet['submission-id'])
            return

        time = 0
        memory = 0
        points = 0.0
        total = 0
        status = 0
        status_codes = ['SC', 'AC', 'WA', 'MLE', 'TLE', 'IR', 'RTE', 'OLE']
        batches = {}  # batch number: (points, total)

        for case in SubmissionTestCase.objects.filter(submission=submission):
            time += case.time
            if not case.batch:
                points += case.points
                total += case.total
            else:
                if case.batch in batches:
                    batches[case.batch][0] = min(batches[case.batch][0], case.points)
                    batches[case.batch][1] = max(batches[case.batch][1], case.total)
                else:
                    batches[case.batch] = [case.points, case.total]
            memory = max(memory, case.memory)
            i = status_codes.index(case.status)
            if i > status:
                status = i

        for i in batches:
            points += batches[i][0]
            total += batches[i][1]

        points = round(points, 1)
        total = round(total, 1)
        submission.case_points = points
        submission.case_total = total

        sub_points = round(points / total * submission.problem.points if total > 0 else 0, 1)
        if not submission.problem.partial and sub_points != submission.problem.points:
            sub_points = 0

        submission.status = 'D'
        submission.time = time
        submission.memory = memory
        submission.points = sub_points
        submission.result = status_codes[status]
        submission.save()

        submission.user.calculate_points()

        if hasattr(submission, 'contest'):
            contest = submission.contest
            contest.points = round(points / total * contest.problem.points if total > 0 else 0, 1)
            if not contest.problem.partial and contest.points != contest.problem.points:
                contest.points = 0
            contest.save()
            submission.contest.participation.recalculate_score()
            submission.contest.participation.update_cumtime()

        finished_submission(submission)

        event.post('sub_%d' % submission.id, {
            'type': 'grading-end',
            'time': time,
            'memory': memory,
            'points': float(points),
            'total': float(submission.problem.points),
            'result': submission.result
        })
        if hasattr(submission, 'contest'):
            participation = submission.contest.participation
            event.post('contest_%d' % participation.contest_id, {'type': 'update'})
        if not submission.problem.is_public:
            return
        event.post('submissions', {'type': 'done-submission', 'id': submission.id,
                                   'contest': submission.contest_key,
                                   'user': submission.user_id, 'problem': submission.problem_id})

    def on_compile_error(self, packet):
        super(DjangoJudgeHandler, self).on_compile_error(packet)
        try:
            submission = Submission.objects.get(id=packet['submission-id'])
        except Submission.DoesNotExist:
            logger.warning('Unknown submission: %d', packet['submission-id'])
            return
        submission.status = submission.result = 'CE'
        submission.error = packet['log']
        submission.save()
        event.post('sub_%d' % submission.id, {
            'type': 'compile-error',
            'log': packet['log']
        })
        if not submission.problem.is_public:
            return
        event.post('submissions', {'type': 'update-submission', 'id': submission.id,
                                   'state': 'compile-error', 'contest': submission.contest_key,
                                   'user': submission.user_id, 'problem': submission.problem_id})

    def on_compile_message(self, packet):
        super(DjangoJudgeHandler, self).on_compile_message(packet)
        try:
            submission = Submission.objects.get(id=packet['submission-id'])
        except Submission.DoesNotExist:
            logger.warning('Unknown submission: %d', packet['submission-id'])
            return
        submission.error = packet['log']
        submission.save()
        event.post('sub_%d' % submission.id, {
            'type': 'compile-message'
        })

    def on_internal_error(self, packet):
        super(DjangoJudgeHandler, self).on_internal_error(packet)
        try:
            submission = Submission.objects.get(id=packet['submission-id'])
        except Submission.DoesNotExist:
            logger.warning('Unknown submission: %d', packet['submission-id'])
            return
        submission.status = submission.result = 'IE'
        submission.error = packet['message']
        submission.save()
        event.post('sub_%d' % submission.id, {
            'type': 'internal-error'
        })
        if not submission.problem.is_public:
            return
        event.post('submissions', {'type': 'update-submission', 'id': submission.id,
                                   'state': 'internal-error', 'contest': submission.contest_key,
                                   'user': submission.user_id, 'problem': submission.problem_id})

    def on_submission_terminated(self, packet):
        super(DjangoJudgeHandler, self).on_submission_terminated(packet)
        try:
            submission = Submission.objects.get(id=packet['submission-id'])
        except Submission.DoesNotExist:
            logger.warning('Unknown submission: %d', packet['submission-id'])
            return
        submission.status = submission.result = 'AB'
        submission.save()
        if not submission.problem.is_public:
            return
        event.post('sub_%d' % submission.id, {
            'type': 'aborted-submission'
        })
        event.post('submissions', {'type': 'update-submission', 'id': submission.id,
                                   'state': 'terminated', 'contest': submission.contest_key,
                                   'user': submission.user_id, 'problem': submission.problem_id})

    def on_test_case(self, packet):
        super(DjangoJudgeHandler, self).on_test_case(packet)
        try:
            submission = Submission.objects.get(id=packet['submission-id'])
        except Submission.DoesNotExist:
            logger.warning('Unknown submission: %d', packet['submission-id'])
            return
        test_case = SubmissionTestCase(submission=submission, case=packet['position'])
        status = packet['status']
        if status & 4:
            test_case.status = 'TLE'
        elif status & 8:
            test_case.status = 'MLE'
        elif status & 64:
            test_case.status = 'OLE'
        elif status & 2:
            test_case.status = 'RTE'
        elif status & 16:
            test_case.status = 'IR'
        elif status & 1:
            test_case.status = 'WA'
        elif status & 32:
            test_case.status = 'SC'
        else:
            test_case.status = 'AC'
        test_case.time = packet['time']
        test_case.memory = packet['memory']
        test_case.points = packet['points']
        test_case.total = packet['total-points']
        test_case.batch = self.batch_id if self.in_batch else None
        test_case.feedback = packet.get('feedback', None) or ''
        test_case.output = packet['output']
        submission.current_testcase = packet['position'] + 1
        submission.save()
        test_case.save()

        do_post = True

        if submission.id in self.update_counter:
            cnt, reset = self.update_counter[submission.id]
            cnt += 1
            if TIMER() - reset > UPDATE_RATE_TIME:
                del self.update_counter[submission.id]
            else:
                self.update_counter[submission.id] = (cnt, reset)
                if cnt > UPDATE_RATE_LIMIT:
                    do_post = False
        if submission.id not in self.update_counter:
            self.update_counter[submission.id] = (1, TIMER())

        if do_post:
            event.post('sub_%d' % submission.id, {
                'type': 'test-case',
                'id': packet['position'],
                'status': test_case.status,
                'time': "%.3f" % round(float(packet['time']), 3),
                'memory': packet['memory'],
                'points': float(test_case.points),
                'total': float(test_case.total),
                'output': packet['output']
            })
            if not submission.problem.is_public:
                return
            event.post('submissions', {'type': 'update-submission', 'id': submission.id,
                                       'state': 'test-case', 'contest': submission.contest_key,
                                       'user': submission.user_id, 'problem': submission.problem_id})

    def on_supported_problems(self, packet):
        super(DjangoJudgeHandler, self).on_supported_problems(packet)

        judge = Judge.objects.get(name=self.name)
        judge.problems = Problem.objects.filter(code__in=self.problems.keys())
        judge.save()
