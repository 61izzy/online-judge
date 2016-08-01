from calendar import Calendar, SUNDAY
from collections import namedtuple, defaultdict
from datetime import timedelta, date, datetime, time
from operator import attrgetter

import pytz
from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist, ImproperlyConfigured
from django.core.urlresolvers import reverse
from django.db import connection
from django.db.models import Count, Q, Min, Max
from django.http import HttpResponseRedirect, HttpResponseBadRequest, Http404, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.html import escape
from django.utils.timezone import make_aware
from django.utils.translation import ugettext as _, ugettext_lazy
from django.views.generic import ListView, TemplateView
from django.views.generic.detail import BaseDetailView, DetailView

from judge import event_poster as event
from judge.comments import CommentedDetailView
from judge.models import Contest, ContestParticipation, ContestTag
from judge.utils.ranker import ranker
from judge.utils.views import TitleMixin, generic_message

__all__ = ['ContestList', 'ContestDetail', 'contest_ranking', 'ContestJoin', 'ContestLeave', 'ContestCalendar',
           'contest_ranking_ajax']


def _find_contest(request, key, private_check=True):
    try:
        contest = Contest.objects.get(key=key)
        if private_check and not contest.is_public and not request.user.has_perm('judge.see_private_contest') and (
                not request.user.has_perm('judge.edit_own_contest') or
                not contest.organizers.filter(id=request.user.profile.id).exists()):
            raise ObjectDoesNotExist()
    except ObjectDoesNotExist:
        return generic_message(request, _('No such contest'),
                               _('Could not find a contest with the key "%s".') % key, status=404), False
    return contest, True


class ContestListMixin(object):
    def get_queryset(self):
        queryset = Contest.objects.all()
        if not self.request.user.has_perm('judge.see_private_contest'):
            queryset = queryset.filter(is_public=True)
        if not self.request.user.has_perm('judge.edit_all_contest'):
            q = Q(is_private=False)
            if self.request.user.is_authenticated():
                q |= Q(organizations__in=self.request.user.profile.organizations.all())
            queryset = queryset.filter(q)
        return queryset


class ContestList(TitleMixin, ContestListMixin, ListView):
    model = Contest
    template_name = 'contest/list.jade'
    title = ugettext_lazy('Contests')

    def get_queryset(self):
        return super(ContestList, self).get_queryset().annotate(participation_count=Count('users')) \
            .order_by('-start_time', 'key').prefetch_related('tags')

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
        context['now'] = timezone.now()
        return context


class PrivateContestError(Exception):
    def __init__(self, name, orgs):
        self.name = name
        self.orgs = orgs


class ContestMixin(object):
    context_object_name = 'contest'
    model = Contest
    slug_field = 'key'
    slug_url_kwarg = 'contest'

    def get_object(self, queryset=None):
        contest = super(ContestMixin, self).get_object(queryset)
        user = self.request.user
        profile = self.request.user.profile if user.is_authenticated() else None

        if (profile is not None and
                ContestParticipation.objects.filter(id=profile.current_contest_id, contest_id=contest.id).exists()):
            return contest

        if not contest.is_public and not user.has_perm('judge.see_private_contest') and (
                not user.has_perm('judge.edit_own_contest') or
                not contest.organizers.filter(id=profile.id).exists()):
            raise Http404()

        if contest.is_private:
            if profile is None or (not user.has_perm('judge.edit_all_contest') and
                                   not contest.organizations.filter(id__in=profile.organizations.all()).exists()):
                raise PrivateContestError(contest.name, contest.organizations.all())
        return contest

    def dispatch(self, request, *args, **kwargs):
        try:
            return super(ContestMixin, self).dispatch(request, *args, **kwargs)
        except Http404:
            key = kwargs.get(self.slug_url_kwarg, None)
            if key:
                return generic_message(request, _('No such contest'),
                                       _('Could not find a contest with the key "%s".') % key)
            else:
                return generic_message(request, _('No such contest'),
                                       _('Could not find such contest.'))
        except PrivateContestError as e:
            return render(request, 'contest/private.jade', {
                'orgs': e.orgs, 'title': _('Access to contest "%s" denied') % escape(e.name)
            }, status=403)


class ContestDetail(ContestMixin, TitleMixin, CommentedDetailView):
    template_name = 'contest/contest.jade'

    def get_comment_page(self):
        return 'c:%s' % self.object.key

    def get_title(self):
        return self.object.name

    def get_context_data(self, **kwargs):
        context = super(ContestDetail, self).get_context_data(**kwargs)
        if self.request.user.is_authenticated():
            profile = self.request.user.profile
            try:
                context['participation'] = profile.contest_history.get(contest=self.object)
            except ContestParticipation.DoesNotExist:
                context['participating'] = False
                context['participation'] = None
            else:
                context['participating'] = True
            context['in_contest'] = (profile.current_contest is not None and
                                     profile.current_contest.contest == self.object)
        else:
            context['participating'] = False
            context['participation'] = None
            context['in_contest'] = False
        context['now'] = timezone.now()
        context['og_image'] = self.object.og_image
        return context


class ContestJoin(LoginRequiredMixin, ContestMixin, BaseDetailView):
    def get(self, request, *args, **kwargs):
        contest = self.get_object()
        if not contest.can_join:
            return generic_message(request, _('Contest not ongoing'),
                                   _('"%s" is not currently ongoing.') % contest.name)

        profile = request.user.profile
        if profile.current_contest is not None:
            return generic_message(request, _('Already in contest'),
                                   _('You are already in a contest: "%s".') % profile.current_contest.contest.name)

        participation, created = ContestParticipation.objects.get_or_create(
                contest=contest, user=profile, defaults={
                    'real_start': timezone.now()
                }
        )

        if not created and participation.ended:
            return generic_message(request, _('Time limit exceeded'),
                                   _('Too late! You already used up your time limit for "%s".') % contest.name)

        profile.current_contest = participation
        profile.save()
        return HttpResponseRedirect(reverse('problem_list'))


class ContestLeave(LoginRequiredMixin, ContestMixin, BaseDetailView):
    def get(self, request, *args, **kwargs):
        contest = self.get_object()

        profile = request.user.profile
        if profile.current_contest is None or profile.current_contest.contest_id != contest.id:
            return generic_message(request, _('No such contest'),
                                   _('You are not in contest "%s".') % contest.key, 404)
        profile.current_contest = None
        profile.save()
        return HttpResponseRedirect(reverse('contest_view', args=(contest.key,)))


ContestDay = namedtuple('ContestDay', 'date weekday is_pad is_today starts ends oneday')


class ContestCalendar(ContestListMixin, TemplateView):
    firstweekday = SUNDAY
    weekday_classes = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat']
    template_name = 'contest/calendar.jade'

    def get(self, request, *args, **kwargs):
        try:
            self.year = int(kwargs['year'])
            self.month = int(kwargs['month'])
        except (KeyError, ValueError):
            raise ImproperlyConfigured(_('ContestCalender requires integer year and month'))
        self.today = timezone.now().date()
        return self.render()

    def render(self):
        context = self.get_context_data()
        return self.render_to_response(context)

    def get_contest_data(self, start, end):
        end += timedelta(days=1)
        contests = self.get_queryset().filter(Q(start_time__gte=start, start_time__lt=end) |
                                              Q(end_time__gte=start, end_time__lt=end)).defer('description')
        starts, ends, oneday = (defaultdict(list) for i in xrange(3))
        for contest in contests:
            start_date = timezone.localtime(contest.start_time).date()
            end_date = timezone.localtime(contest.end_time).date()
            if start_date == end_date:
                oneday[start_date].append(contest)
            else:
                starts[start_date].append(contest)
                ends[end_date].append(contest)
        return starts, ends, oneday

    def get_table(self):
        calendar = Calendar(self.firstweekday).monthdatescalendar(self.year, self.month)
        starts, ends, oneday = self.get_contest_data(timezone.make_aware(datetime.combine(calendar[0][0], time.min)),
                                                     timezone.make_aware(datetime.combine(calendar[-1][-1], time.min)))
        return [[ContestDay(
                date=date, weekday=self.weekday_classes[weekday], is_pad=date.month != self.month,
                is_today=date == self.today, starts=starts[date], ends=ends[date], oneday=oneday[date],
        ) for weekday, date in enumerate(week)] for week in calendar]

    def get_context_data(self, **kwargs):
        context = super(ContestCalendar, self).get_context_data(**kwargs)

        try:
            context['month'] = date(self.year, self.month, 1)
        except ValueError:
            raise Http404()

        dates = Contest.objects.aggregate(min=Min('start_time'), max=Max('end_time'))
        min_month = dates['min'].year, dates['min'].month
        max_month = max((dates['max'].year, dates['max'].month), (self.today.year, self.today.month))

        month = (self.year, self.month)
        if month < min_month or month > max_month:
            # 404 is valid because it merely declares the lack of existence, without any reason
            raise Http404()

        context['calendar'] = self.get_table()

        if month > min_month:
            context['prev_month'] = date(self.year - (self.month == 1), 12 if self.month == 1 else self.month - 1, 1)
        else:
            context['prev_month'] = None

        if month < max_month:
            context['next_month'] = date(self.year + (self.month == 12), 1 if self.month == 12 else self.month + 1, 1)
        else:
            context['next_month'] = None
        return context


class CachedContestCalendar(ContestCalendar):
    def render(self):
        key = 'contest_cal:%d:%d' % (self.year, self.month)
        cached = cache.get(key)
        if cached is not None:
            return HttpResponse(cached)
        response = super(CachedContestCalendar, self).render()
        response.render()
        cached.set(key, response.content)
        return response


ContestRankingProfile = namedtuple('ContestRankingProfile',
                                   'id user display_rank long_display_name points cumtime problems rating organization')
BestSolutionData = namedtuple('BestSolutionData', 'code points time state')


def contest_ranking_list(contest, problems, tz=pytz.timezone(getattr(settings, 'TIME_ZONE', 'UTC'))):
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
    data = {(part, prob): (code, best, last and make_aware(last, tz)) for part, prob, code, best, last in
            cursor.fetchall()}
    problems = map(attrgetter('id', 'points'), problems)
    cursor.close()

    def make_ranking_profile(participation):
        user = participation.user
        part = participation.id
        return ContestRankingProfile(
                id=user.id,
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
               contest.users.select_related('user__user', 'rating')
               .prefetch_related('user__organizations')
               .defer('user__about', 'user__organizations__about')
               .order_by('-score', 'cumtime'))


def contest_ranking_ajax(request, contest):
    contest, exists = _find_contest(request, contest)
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


def contest_ranking(request, contest):
    contest, exists = _find_contest(request, contest)
    if not exists:
        return contest
    return contest_ranking_view(request, contest)


class ContestTagDetailAjax(TitleMixin, DetailView):
    model = ContestTag
    slug_field = slug_url_kwarg = 'name'
    context_object_name = 'tag'
    template_name = 'contest/tag_ajax.jade'


class ContestTagDetail(ContestTagDetailAjax):
    template_name = 'contest/tag.jade'

    def get_title(self):
        return _('Contest tag: %s') % self.object.name
