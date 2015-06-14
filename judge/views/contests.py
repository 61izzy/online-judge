from collections import namedtuple
from operator import attrgetter
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ObjectDoesNotExist
from django.core.urlresolvers import reverse
from django.db import connection
from django.db.models import Max, Count
from django.http import HttpResponseRedirect, HttpResponseBadRequest, Http404
from django.shortcuts import render
from django.template import RequestContext
from django.utils import timezone
from django.utils.functional import SimpleLazyObject
from django.views.generic import ListView

from judge.comments import CommentedDetailView
from judge.models import Contest, ContestParticipation, ContestProblem, Profile
from judge.utils.ranker import ranker
from judge.utils.views import TitleMixin, generic_message
from judge import event_poster as event


__all__ = ['ContestList', 'ContestDetail', 'contest_ranking', 'join_contest', 'leave_contest', 'contest_ranking_ajax']


def _find_contest(request, key, private_check=True):
    try:
        contest = Contest.objects.get(key=key)
        if private_check and not contest.is_public and not request.user.has_perm('judge.see_private_contest'):
            raise ObjectDoesNotExist()
    except ObjectDoesNotExist:
        return generic_message(request, 'No such contest',
                               'Could not find a contest with the key "%s".' % key, status=404), False
    return contest, True


class ContestList(TitleMixin, ListView):
    model = Contest
    template_name = 'contest/list.jade'
    title = 'Contests'

    def get_queryset(self):
        queryset = Contest.objects.order_by('-start_time', 'key')
        if not self.request.user.has_perm('judge.see_private_contest'):
            queryset = queryset.filter(is_public=True)
        queryset = queryset.annotate(participation_count=Count('users'))
        return queryset

    def get_context_data(self, **kwargs):
        context = super(ContestList, self).get_context_data(**kwargs)
        now = timezone.now()
        past, present, future = [], [], []
        for contest in self.get_queryset():
            if contest.end_time < now:
                past.append(contest)
            elif contest.start_time > now:
                future.append(contest)
            else:
                present.append(contest)
        future.sort(key=attrgetter('start_time'))
        context['current_contests'] = present
        context['past_contests'] = past
        context['future_contests'] = future
        return context


class ContestMixin(object):
    context_object_name = 'contest'
    model = Contest
    slug_field = 'key'
    slug_url_kwarg = 'key'
    private_check = True

    def get_object(self, queryset=None):
        contest = super(ContestMixin, self).get_object(queryset)
        if self.private_check and not contest.is_public and not self.request.user.has_perm('judge.see_private_contest'):
            raise Http404()
        return contest

    def dispatch(self, request, *args, **kwargs):
        try:
            return super(ContestMixin, self).dispatch(request, *args, **kwargs)
        except Http404:
            key = kwargs.get(self.slug_url_kwarg, None)
            if key:
                return generic_message(request, 'No such contest',
                                       'Could not find a contest with the key "%s".' % key)
            else:
                return generic_message(request, 'No such contest',
                                       'Could not find such contest.')


class ContestDetail(ContestMixin, TitleMixin, CommentedDetailView):
    template_name = 'contest/contest.jade'

    def get_comment_page(self):
        return 'c:%s' % self.object.key

    def get_title(self):
        return self.object.name

    def get_context_data(self, **kwargs):
        context = super(ContestDetail, self).get_context_data(**kwargs)
        if self.request.user.is_authenticated():
            contest_profile = self.request.user.profile.contest
            try:
                context['participation'] = contest_profile.history.get(contest=self.object)
            except ContestParticipation.DoesNotExist:
                context['participating'] = False
                context['participation'] = None
            else:
                context['participating'] = True
            context['in_contest'] = contest_profile.current is not None and contest_profile.current.contest == self.object
        else:
            context['participating'] = False
            context['participation'] = None
            context['in_contest'] = False
        context['now'] = timezone.now()
        return context


@login_required
def join_contest(request, key):
    contest, exists = _find_contest(request, key)
    if not exists:
        return contest

    if not contest.can_join:
        return generic_message(request, 'Contest not ongoing',
                               '"%s" is not currently ongoing.' % contest.name)

    contest_profile = request.user.profile.contest
    if contest_profile.current is not None:
        return generic_message(request, 'Already in contest',
                               'You are already in a contest: "%s".' % contest_profile.current.contest.name)

    participation, created = ContestParticipation.objects.get_or_create(
        contest=contest, profile=contest_profile, defaults={
            'real_start': timezone.now()
        }
    )

    if not created and participation.ended:
        return generic_message(request, 'Time limit exceeded',
                               'Too late! You already used up your time limit for "%s".' % contest.name)

    contest_profile.current = participation
    contest_profile.save()
    return HttpResponseRedirect(reverse('problem_list'))


@login_required
def leave_contest(request, key):
    # No public checking because if we hide the contest people should still be able to leave.
    # No lock ins.
    contest, exists = _find_contest(request, key, False)
    if not exists:
        return contest

    contest_profile = request.user.profile.contest
    if contest_profile.current is None or contest_profile.current.contest != contest:
        return generic_message(request, 'No such contest',
                               'You are not in contest "%s".' % key, 404)
    contest_profile.current = None
    contest_profile.save()
    return HttpResponseRedirect(reverse('contest_view', args=(key,)))


ContestRankingProfile = namedtuple('ContestRankingProfile',
                                   'id user display_rank long_display_name points cumtime problems rating organization')
BestSolutionData = namedtuple('BestSolutionData', 'code points time state')


def contest_ranking_list(contest, problems):
    cursor = connection.cursor()
    cursor.execute('''
        SELECT part.id, cp.id, prob.code, MAX(cs.points) AS best, MAX(sub.date) AS `last`
        FROM judge_contestproblem cp CROSS JOIN judge_contestparticipation part INNER JOIN
             judge_problem prob ON (cp.problem_id = prob.id) LEFT OUTER JOIN
             judge_contestsubmission cs ON (cs.problem_id = cp.id AND cs.participation_id = part.id) LEFT OUTER JOIN
             judge_submission sub ON (sub.id = cs.submission_id)
        WHERE cp.contest_id = %s AND part.contest_id = %s
        GROUP BY cp.id, part.id
    ''', (contest.id, contest.id))
    data = {(part, prob): (code, best, last) for part, prob, code, best, last in cursor.fetchall()}
    problems = map(attrgetter('id', 'points'), problems)
    cursor.close()

    def make_ranking_profile(participation):
        contest_profile = participation.profile
        user = contest_profile.user
        part = participation.id
        return ContestRankingProfile(
            id=contest_profile.user_id,
            user=user.user,
            display_rank=user.display_rank,
            long_display_name=user.long_display_name,
            points=participation.score,
            cumtime=participation.cumtime,
            organization=user.organization,
            rating=participation.rating.rating if hasattr(participation, 'rating') else None,
            problems=[BestSolutionData(
                code=data[part, prob][0], points=data[part, prob][1],
                time=data[part, prob][2] - participation.start,
                state='failed-score' if not data[part, prob][1] else
                      ('full-score' if data[part, prob][1] == points else 'partial-score'),
            ) if data[part, prob][1] is not None else None for prob, points in problems]
        )

    return map(make_ranking_profile,
               contest.users.select_related('profile__user__user', 'profile__user__organization', 'rating')
                      .defer('profile__user__about', 'profile__user__organization__about')
                      .order_by('-score', 'cumtime'))


def contest_ranking_ajax(request, key):
    contest, exists = _find_contest(request, key)
    if not exists:
        return HttpResponseBadRequest('Invalid contest', content_type='text/plain')
    problems = list(contest.contest_problems.select_related('problem').defer('problem__description').order_by('order'))
    return render(request, 'contest/ranking_table.jade', {
        'users': ranker(contest_ranking_list(contest, problems), key=attrgetter('points', 'cumtime')),
        'problems': problems,
        'contest': contest,
        'show_organization': True,
        'has_rating': contest.ratings.exists(),
    })


def contest_ranking_view(request, contest):
    if not request.user.has_perm('judge.see_private_contest'):
        if not contest.is_public:
            raise Http404()
        if contest.start_time is not None and contest.start_time > timezone.now():
            raise Http404()

    problems = list(contest.contest_problems.select_related('problem').defer('problem__description').order_by('order'))
    return render(request, 'contest/ranking.jade', {
        'users': ranker(contest_ranking_list(contest, problems), key=attrgetter('points', 'cumtime')),
        'title': '%s Rankings' % contest.name,
        'content_title': contest.name,
        'subtitle': 'Rankings',
        'problems': problems,
        'contest': contest,
        'show_organization': True,
        'last_msg': event.last(),
        'has_rating': contest.ratings.exists(),
    })


def contest_ranking(request, key):
    contest, exists = _find_contest(request, key)
    if not exists:
        return contest
    return contest_ranking_view(request, contest)
