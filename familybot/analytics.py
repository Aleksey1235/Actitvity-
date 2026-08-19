from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Mapping, Sequence

from .repository import GuildSettings, Repository
from .timeutil import UTC, day_group, local_date, local_segment, parse_iso


RATING_LABELS = {
    "very_high": "🔥 Очень высокая",
    "high": "🟢 Высокая",
    "stable": "🟡 Стабильная",
    "irregular": "🟠 Нерегулярная",
    "low": "🔴 Практически не участвует",
    "insufficient": "⚪ Недостаточно данных",
    "newcomer": "🆕 Новый участник",
    "vacation": "💤 Отпуск",
}

CATEGORY_LABELS = {
    "training": "🏋️ Тренировки",
    "family": "🏠 Семейный контент",
    "faction": "🏛 Фракционный контент",
}


@dataclass(slots=True)
class EventRecord:
    id: int
    category: str
    title: str
    scheduled_for: datetime
    ended_at: datetime | None
    analytical: bool
    spontaneous: bool
    audience_type: str
    min_rank: int | None
    max_rank: int | None
    audience_group: str | None = None
    started_at: datetime | None = None

    @property
    def actual_time(self) -> datetime:
        """Best trustworthy factual timestamp for attendance/history.

        Fair-opportunity calculations deliberately use ``scheduled_for`` because a
        member's chance to attend is determined by the announced schedule. Factual
        charts prefer the recorded real start, but only when it is plausibly close to
        the schedule. This protects analytics from an organizer accidentally pressing
        Start many hours/days early on a future card.
        """
        if self.started_at is not None:
            if abs(self.started_at - self.scheduled_for) <= timedelta(hours=12):
                return self.started_at
        return self.scheduled_for

    @property
    def effective_end(self) -> datetime:
        """End used only for conflicting-opportunity clustering.

        A forgotten close must never glue unrelated activities into a single huge
        opportunity, so the measured runtime is normalized to 10 min..6 h.
        """
        duration = timedelta(minutes=90)
        if self.started_at and self.ended_at and self.ended_at > self.started_at:
            measured = self.ended_at - self.started_at
            duration = min(max(measured, timedelta(minutes=10)), timedelta(hours=6))
        return self.actual_time + duration


@dataclass(slots=True)
class MemberContext:
    membership_id: int
    person_id: int
    nickname: str
    static_id: str
    discord_user_id: int | None
    joined_at: datetime
    left_at: datetime | None
    current_rank: int


@dataclass(slots=True)
class Opportunity:
    events: list[EventRecord]
    attended_event_ids: set[int]

    @property
    def attended(self) -> bool:
        return bool(self.attended_event_ids)

    @property
    def scheduled_for(self) -> datetime:
        return min(e.scheduled_for for e in self.events)


@dataclass(slots=True)
class CategoryStat:
    category: str
    eligible_events: int = 0
    attended_eligible_events: int = 0
    attended_total: int = 0


@dataclass(slots=True)
class MemberStats:
    membership_id: int
    nickname: str
    static_id: str
    period_start: datetime
    period_end: datetime

    # Fair denominator: planned, analytical, audience/time/member/vacation eligible,
    # with overlapping events merged into a single physical opportunity.
    opportunities: int
    attended_opportunities: int
    missed_opportunities: int
    attendance_rate: float | None
    eligible_planned_events: int
    overlap_merged_events: int
    opportunity_days: int
    eligibility_exclusions: dict[str, int]
    opportunity_weeks: int
    attended_opportunity_weeks: int
    opportunity_week_rate: float | None

    # Factual attendance: independent of denominator eligibility. This is the first
    # thing users should see, so spontaneous attendance never looks like "0/0".
    total_attended_events: int
    planned_attended_events: int
    spontaneous_attended: int
    extra_attended: int
    active_days: int
    active_weeks: int
    last_activity_at: datetime | None

    category_stats: dict[str, CategoryStat]
    rating_key: str
    rating_label: str
    reasons: list[str]
    availability_configured: bool
    on_vacation_entire_period: bool


@dataclass(slots=True)
class FamilyPulse:
    score: int
    published_score: int | None
    label: str
    confidence: float
    provisional: bool

    group_mode_enabled: bool
    main_members: int
    academy_members: int
    unclassified_members: int
    conflict_members: int
    group_data_completeness: float

    # Components. None means there was not enough applicable data, not "0%".
    schedule_coverage: float | None
    reach: float | None
    regularity: float | None
    median_attendance: float | None
    rhythm: float | None

    evaluable_members: int
    pulse_members: int
    schedule_profile_missing: int
    data_completeness: float
    active_members: int
    unique_attendees: int
    analytical_events: int
    planned_events: int
    spontaneous_events: int
    event_days: int
    regularity_target_days: int
    regularity_base_members: int
    category_counts: dict[str, int]
    time_segment_counts: dict[str, int]
    insufficient_opportunity_members: int
    explanations: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PeriodBundle:
    settings: GuildSettings
    members: list[MemberContext]
    events: list[EventRecord]
    availability: dict[int, list]
    vacations: dict[int, list]
    rank_history: dict[int, list]
    groups: dict[int, list]
    attendees_by_event: dict[int, set[int]]
    attendance_rows_by_member: dict[int, list]
    custom_audience: dict[int, set[int]]


def _pulse_label(score: int, confidence: float) -> str:
    if confidence < 0.45:
        return "⚪ Недостаточно данных для уверенной оценки"
    if confidence < 0.70:
        return "🟣 Предварительная оценка семьи"
    if score >= 90:
        return "🔥 Очень высокий актив семьи"
    if score >= 75:
        return "🟢 Высокий актив семьи"
    if score >= 60:
        return "🟡 Стабильный актив семьи"
    if score >= 40:
        return "🟠 Актив семьи проседает"
    return "🔴 Низкий актив семьи"


def _rank_at(member: MemberContext, history_rows: Sequence, when: datetime) -> int:
    # Initial rank history exists for new memberships. For old imported data where it
    # may not, current_rank is the safest backwards-compatible fallback.
    rank = member.current_rank
    eligible = [r for r in history_rows if parse_iso(r["changed_at"]) <= when]
    if eligible:
        rank = int(eligible[-1]["new_rank"])
    return rank


def _member_exists_at(member: MemberContext, when: datetime) -> bool:
    if member.joined_at > when:
        return False
    if member.left_at is not None and member.left_at <= when:
        return False
    return True


def _vacation_intervals(
    rows: Sequence,
    tz_name: str,
    cutover_at: str | None,
) -> list[tuple[datetime, datetime]]:
    """Normalize legacy/date and v1.2 exact vacation rows into UTC intervals.

    Discord role periods created by v1.2 are exact. Startup/periodic reconciliation
    intentionally begins at the moment the role was observed, because Discord does
    not expose a trustworthy historical assignment timestamp after bot downtime.
    Legacy manual vacations stop being authoritative at the role-integration cutover.
    """
    tz = ZoneInfo(tz_name)
    cutover = parse_iso(cutover_at) if cutover_at else None
    infinity = datetime.max.replace(tzinfo=UTC)
    intervals: list[tuple[datetime, datetime]] = []
    for row in rows:
        source = str(row["source"]) if "source" in row.keys() else "legacy"
        starts_at_raw = row["starts_at"] if "starts_at" in row.keys() else None
        ends_at_raw = row["ends_at"] if "ends_at" in row.keys() else None

        if source == "discord_role" and starts_at_raw:
            start = parse_iso(str(starts_at_raw))
            end = parse_iso(str(ends_at_raw)) if ends_at_raw else infinity
        else:
            start_day = date.fromisoformat(str(row["starts_on"]))
            end_day = date.fromisoformat(str(row["ends_on"]))
            start = datetime.combine(start_day, datetime.min.time(), tzinfo=tz).astimezone(UTC)
            end = datetime.combine(end_day + timedelta(days=1), datetime.min.time(), tzinfo=tz).astimezone(UTC)

        if source == "legacy" and cutover is not None:
            if start >= cutover:
                continue
            end = min(end, cutover)
        if end > start:
            intervals.append((start, end))

    intervals.sort(key=lambda x: x[0])
    return intervals


def _vacation_on(
    rows: Sequence,
    when: datetime,
    tz_name: str,
    cutover_at: str | None = None,
) -> bool:
    return any(start <= when < end for start, end in _vacation_intervals(rows, tz_name, cutover_at))


def _availability_matches(rows: Sequence, when: datetime, tz_name: str) -> tuple[bool, bool]:
    active_rows = []
    for row in rows:
        start = parse_iso(row["effective_from"])
        end = parse_iso(row["effective_to"]) if row["effective_to"] else None
        if start <= when and (end is None or end > when):
            active_rows.append(row)
    if not active_rows:
        return False, False
    group = day_group(when, tz_name)
    segment = local_segment(when, tz_name)
    matching_group = [r for r in active_rows if r["day_group"] == group]
    if not matching_group:
        return True, False
    segments = {r["segment"] for r in matching_group}
    return True, ("floating" in segments or segment in segments)


def _group_at(rows: Sequence, when: datetime) -> str | None:
    applicable = []
    for row in rows:
        start = parse_iso(str(row["starts_at"]))
        end = parse_iso(str(row["ends_at"])) if row["ends_at"] else None
        if start <= when and (end is None or end > when):
            applicable.append(row)
    if not applicable:
        return None
    applicable.sort(key=lambda r: (parse_iso(str(r["starts_at"])), int(r["id"])))
    return str(applicable[-1]["group_name"])


def _group_mode_enabled(settings: GuildSettings) -> bool:
    return bool(settings.academy_role_id and settings.main_role_id and settings.group_role_cutover_at)


def _audience_matches(
    member: MemberContext,
    event: EventRecord,
    custom_audience: Mapping[int, set[int]],
    rank_history: Mapping[int, Sequence],
    group_history: Mapping[int, Sequence],
) -> bool:
    if event.audience_group is not None:
        if _group_at(group_history.get(member.membership_id, []), event.scheduled_for) != event.audience_group:
            return False
    if event.audience_type == "all":
        return True
    if event.audience_type == "custom":
        return member.membership_id in custom_audience.get(event.id, set())
    rank = _rank_at(member, rank_history.get(member.membership_id, []), event.scheduled_for)
    low = event.min_rank if event.min_rank is not None else -math.inf
    high = event.max_rank if event.max_rank is not None else math.inf
    return low <= rank <= high


def eligible_for_denominator(
    member: MemberContext,
    event: EventRecord,
    bundle: PeriodBundle,
) -> tuple[bool, str | None]:
    """Whether absence at this event can fairly count against the member."""
    if not event.analytical:
        return False, "not_analytical"
    if event.spontaneous:
        return False, "spontaneous"
    if not _member_exists_at(member, event.scheduled_for):
        return False, "not_member_at_time"
    if event.scheduled_for < member.joined_at + timedelta(days=bundle.settings.newcomer_days):
        return False, "newcomer"
    if _vacation_on(
        bundle.vacations.get(member.membership_id, []),
        event.scheduled_for,
        bundle.settings.timezone,
        bundle.settings.vacation_role_cutover_at,
    ):
        return False, "vacation"
    if not _audience_matches(member, event, bundle.custom_audience, bundle.rank_history, bundle.groups):
        return False, "audience"
    configured, matches = _availability_matches(
        bundle.availability.get(member.membership_id, []),
        event.scheduled_for,
        bundle.settings.timezone,
    )
    if not configured:
        return False, "availability_missing"
    if not matches:
        return False, "time_mismatch"
    return True, None


def cluster_opportunities(
    events: Sequence[EventRecord], attended_event_ids: set[int]
) -> list[Opportunity]:
    """Merge overlapping activities into one physical opportunity.

    If two activities overlap, a member cannot reasonably be expected to attend both.
    Attendance at any event in that cluster uses the opportunity.
    """
    if not events:
        return []
    ordered = sorted(events, key=lambda e: e.actual_time)
    clusters: list[list[EventRecord]] = []
    current: list[EventRecord] = [ordered[0]]
    current_end = ordered[0].effective_end
    for event in ordered[1:]:
        if event.actual_time < current_end:
            current.append(event)
            current_end = max(current_end, event.effective_end)
        else:
            clusters.append(current)
            current = [event]
            current_end = event.effective_end
    clusters.append(current)
    return [
        Opportunity(
            events=c,
            attended_event_ids={e.id for e in c if e.id in attended_event_ids},
        )
        for c in clusters
    ]


def rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def determine_member_rating(
    *,
    opportunities: int,
    attended: int,
    opportunity_weeks: int,
    attended_opportunity_weeks: int,
    total_attended_events: int,
    min_opportunities: int,
    newcomer: bool,
    on_vacation_entire_period: bool,
) -> tuple[str, list[str]]:
    """Qualitative evaluation without personal points.

    The rating is based on fair planned opportunities. Factual spontaneous/extra
    attendance is always shown, but never creates a negative denominator.
    """
    if on_vacation_entire_period:
        return "vacation", ["Весь выбранный период участник находился в отпуске; оценка приостановлена."]
    if newcomer:
        return "newcomer", ["Период адаптации ещё не завершён; отсутствие на активностях не оценивается."]
    if opportunities < min_opportunities:
        reasons = [
            f"Для честной оценки нужно минимум {min_opportunities} подходящих запланированных возможностей; было {opportunities}."
        ]
        if total_attended_events:
            reasons.append(
                f"При этом фактически посещено {total_attended_events} аналитических активностей — участие сохранено и видно отдельно."
            )
        return "insufficient", reasons

    attendance_rate = attended / opportunities if opportunities else 0.0
    consistency = (
        attended_opportunity_weeks / opportunity_weeks if opportunity_weeks else 0.0
    )

    if attendance_rate >= 0.80 and consistency >= 0.75:
        key = "very_high"
    elif attendance_rate >= 0.65 and consistency >= 0.50:
        key = "high"
    elif attendance_rate >= 0.45:
        key = "stable"
    elif attendance_rate >= 0.20 or attended >= 2 or total_attended_events >= 3:
        key = "irregular"
    else:
        key = "low"

    reasons = [
        f"Посещено {attended} из {opportunities} честно доступных возможностей ({attendance_rate:.0%}).",
        f"Участие было в {attended_opportunity_weeks} из {opportunity_weeks} недель, где реально были подходящие возможности ({consistency:.0%}).",
        f"Всего фактически посещено аналитических активностей: {total_attended_events}.",
    ]
    return key, reasons


async def load_period_bundle(
    repo: Repository, guild_id: int, start: datetime, end: datetime
) -> PeriodBundle:
    settings = await repo.ensure_guild_settings(guild_id)
    member_rows = await repo.memberships_overlapping_period(guild_id, start, end)
    members = [
        MemberContext(
            membership_id=int(r["id"]),
            person_id=int(r["person_id"]),
            nickname=str(r["nickname"]),
            static_id=str(r["static_id"]),
            discord_user_id=int(r["discord_user_id"]) if r["discord_user_id"] is not None else None,
            joined_at=parse_iso(r["joined_at"]),
            left_at=parse_iso(r["left_at"]) if r["left_at"] else None,
            current_rank=int(r["rank"]),
        )
        for r in member_rows
    ]
    membership_ids = [m.membership_id for m in members]
    activity_rows = await repo.list_activities_period(guild_id, start, end)
    events = [
        EventRecord(
            id=int(r["id"]),
            category=str(r["category"]),
            title=str(r["title"]),
            scheduled_for=parse_iso(r["scheduled_for"]),
            ended_at=parse_iso(r["ended_at"]) if r["ended_at"] else None,
            analytical=bool(r["analytical"]),
            spontaneous=bool(r["is_spontaneous"]),
            audience_type=str(r["audience_type"]),
            audience_group=str(r["audience_group"]) if r["audience_group"] is not None else None,
            min_rank=int(r["min_rank"]) if r["min_rank"] is not None else None,
            max_rank=int(r["max_rank"]) if r["max_rank"] is not None else None,
            started_at=parse_iso(r["started_at"]) if r["started_at"] else None,
        )
        for r in activity_rows
    ]
    availability_rows = await repo.availability_for_memberships_period(membership_ids, start, end)
    vacation_rows = await repo.vacations_for_memberships_period(
        membership_ids,
        local_date(start, settings.timezone),
        local_date(end - timedelta(seconds=1), settings.timezone),
    )
    rank_rows = await repo.rank_history_for_memberships(membership_ids)
    group_rows = await repo.group_periods_for_memberships_period(membership_ids, start, end)
    attendance_rows = await repo.attendance_for_guild_period(guild_id, start, end)
    custom_rows = await repo.custom_audience_for_activities([e.id for e in events])

    availability: dict[int, list] = defaultdict(list)
    for row in availability_rows:
        availability[int(row["membership_id"])].append(row)
    vacations: dict[int, list] = defaultdict(list)
    for row in vacation_rows:
        vacations[int(row["membership_id"])].append(row)
    rank_history: dict[int, list] = defaultdict(list)
    for row in rank_rows:
        rank_history[int(row["membership_id"])].append(row)
    groups: dict[int, list] = defaultdict(list)
    for row in group_rows:
        groups[int(row["membership_id"])].append(row)
    attendees_by_event: dict[int, set[int]] = defaultdict(set)
    attendance_rows_by_member: dict[int, list] = defaultdict(list)
    for row in attendance_rows:
        attendees_by_event[int(row["activity_id"])].add(int(row["membership_id"]))
        attendance_rows_by_member[int(row["membership_id"])].append(row)
    custom_audience: dict[int, set[int]] = defaultdict(set)
    for row in custom_rows:
        custom_audience[int(row["activity_id"])].add(int(row["membership_id"]))

    return PeriodBundle(
        settings=settings,
        members=members,
        events=events,
        availability=dict(availability),
        vacations=dict(vacations),
        rank_history=dict(rank_history),
        groups=dict(groups),
        attendees_by_event=dict(attendees_by_event),
        attendance_rows_by_member=dict(attendance_rows_by_member),
        custom_audience=dict(custom_audience),
    )


def _vacation_covers_period(
    bundle: PeriodBundle,
    member: MemberContext,
    start: datetime,
    end: datetime,
) -> bool:
    if end <= start:
        return False
    intervals = _vacation_intervals(
        bundle.vacations.get(member.membership_id, []),
        bundle.settings.timezone,
        bundle.settings.vacation_role_cutover_at,
    )
    cursor = start
    for interval_start, interval_end in intervals:
        if interval_end <= cursor:
            continue
        if interval_start > cursor:
            return False
        cursor = max(cursor, interval_end)
        if cursor >= end:
            return True
    return False

def member_stats_from_bundle(
    bundle: PeriodBundle,
    member: MemberContext,
    start: datetime,
    end: datetime,
) -> MemberStats:
    attended_rows = bundle.attendance_rows_by_member.get(member.membership_id, [])
    attended_event_ids = {int(r["activity_id"]) for r in attended_rows}

    eligible_events: list[EventRecord] = []
    exclusion_counts: Counter[str] = Counter()
    planned_analytical_events = [e for e in bundle.events if e.analytical and not e.spontaneous]
    for event in planned_analytical_events:
        eligible, reason = eligible_for_denominator(member, event, bundle)
        if eligible:
            eligible_events.append(event)
        elif reason:
            exclusion_counts[reason] += 1

    opportunities = cluster_opportunities(eligible_events, attended_event_ids)
    attended_opportunities = sum(1 for o in opportunities if o.attended)
    opportunity_days_set = {
        local_date(o.scheduled_for, bundle.settings.timezone) for o in opportunities
    }
    overlap_merged_events = max(0, len(eligible_events) - len(opportunities))

    opp_week_keys: set[tuple[int, int]] = set()
    attended_opp_week_keys: set[tuple[int, int]] = set()
    for opportunity in opportunities:
        local = opportunity.scheduled_for.astimezone(ZoneInfo(bundle.settings.timezone))
        iso_cal = local.isocalendar()
        key = (iso_cal.year, iso_cal.week)
        opp_week_keys.add(key)
        if opportunity.attended:
            attended_opp_week_keys.add(key)

    analytical_events_by_id = {e.id: e for e in bundle.events if e.analytical}
    eligible_event_ids = {e.id for e in eligible_events}
    active_dates: set[date] = set()
    active_weeks: set[tuple[int, int]] = set()
    last_activity_at: datetime | None = None
    spontaneous_attended = 0
    extra_attended = 0
    planned_attended_events = 0
    total_attended_events = 0

    category_stats: dict[str, CategoryStat] = {
        key: CategoryStat(category=key) for key in CATEGORY_LABELS
    }

    for event in eligible_events:
        stat = category_stats.setdefault(event.category, CategoryStat(event.category))
        stat.eligible_events += 1
        if event.id in attended_event_ids:
            stat.attended_eligible_events += 1

    for row in attended_rows:
        event_id = int(row["activity_id"])
        event = analytical_events_by_id.get(event_id)
        if event is None:
            continue
        total_attended_events += 1
        category_stats.setdefault(event.category, CategoryStat(event.category)).attended_total += 1
        when = event.actual_time
        active_dates.add(local_date(when, bundle.settings.timezone))
        local = when.astimezone(ZoneInfo(bundle.settings.timezone))
        iso_cal = local.isocalendar()
        active_weeks.add((iso_cal.year, iso_cal.week))
        if last_activity_at is None or when > last_activity_at:
            last_activity_at = when
        if event.spontaneous:
            spontaneous_attended += 1
        else:
            planned_attended_events += 1
            if event_id not in eligible_event_ids:
                extra_attended += 1

    on_vacation_entire_period = _vacation_covers_period(bundle, member, start, end)
    newcomer = end <= member.joined_at + timedelta(days=bundle.settings.newcomer_days)
    availability_configured = bool(bundle.availability.get(member.membership_id))

    rating_key, reasons = determine_member_rating(
        opportunities=len(opportunities),
        attended=attended_opportunities,
        opportunity_weeks=len(opp_week_keys),
        attended_opportunity_weeks=len(attended_opp_week_keys),
        total_attended_events=total_attended_events,
        min_opportunities=bundle.settings.min_member_opportunities,
        newcomer=newcomer,
        on_vacation_entire_period=on_vacation_entire_period,
    )
    if not availability_configured and rating_key not in {"newcomer", "vacation"}:
        rating_key = "insufficient"
        reasons = [
            "Не настроено обычное время участия, поэтому бот не может честно определить пропущенные возможности.",
            f"Фактических посещений за период: {total_attended_events}.",
        ]

    return MemberStats(
        membership_id=member.membership_id,
        nickname=member.nickname,
        static_id=member.static_id,
        period_start=start,
        period_end=end,
        opportunities=len(opportunities),
        attended_opportunities=attended_opportunities,
        missed_opportunities=max(0, len(opportunities) - attended_opportunities),
        attendance_rate=rate(attended_opportunities, len(opportunities)),
        eligible_planned_events=len(eligible_events),
        overlap_merged_events=overlap_merged_events,
        opportunity_days=len(opportunity_days_set),
        eligibility_exclusions=dict(exclusion_counts),
        opportunity_weeks=len(opp_week_keys),
        attended_opportunity_weeks=len(attended_opp_week_keys),
        opportunity_week_rate=rate(len(attended_opp_week_keys), len(opp_week_keys)),
        total_attended_events=total_attended_events,
        planned_attended_events=planned_attended_events,
        spontaneous_attended=spontaneous_attended,
        extra_attended=extra_attended,
        active_days=len(active_dates),
        active_weeks=len(active_weeks),
        last_activity_at=last_activity_at,
        category_stats=category_stats,
        rating_key=rating_key,
        rating_label=RATING_LABELS[rating_key],
        reasons=reasons,
        availability_configured=availability_configured,
        on_vacation_entire_period=on_vacation_entire_period,
    )


async def member_stats(
    repo: Repository,
    guild_id: int,
    membership_id: int,
    start: datetime,
    end: datetime,
) -> MemberStats:
    bundle = await load_period_bundle(repo, guild_id, start, end)
    member = next((m for m in bundle.members if m.membership_id == membership_id), None)
    if member is None:
        row = await repo.get_membership(membership_id)
        if not row:
            raise ValueError("Membership not found")
        member = MemberContext(
            membership_id=int(row["id"]),
            person_id=int(row["person_id"]),
            nickname=str(row["nickname"]),
            static_id=str(row["static_id"]),
            discord_user_id=int(row["discord_user_id"]) if row["discord_user_id"] is not None else None,
            joined_at=parse_iso(row["joined_at"]),
            left_at=parse_iso(row["left_at"]) if row["left_at"] else None,
            current_rank=int(row["rank"]),
        )
        bundle.members.append(member)
    return member_stats_from_bundle(bundle, member, start, end)


def compare_member_stats(current: MemberStats, previous: MemberStats) -> list[str]:
    lines: list[str] = []
    attendance_delta = current.total_attended_events - previous.total_attended_events
    days_delta = current.active_days - previous.active_days
    if attendance_delta:
        icon = "📈" if attendance_delta > 0 else "📉"
        lines.append(
            f"{icon} Посещённых активностей: **{previous.total_attended_events} → {current.total_attended_events}** ({attendance_delta:+d})."
        )
    if days_delta:
        icon = "📈" if days_delta > 0 else "📉"
        lines.append(
            f"{icon} Дней с участием: **{previous.active_days} → {current.active_days}** ({days_delta:+d})."
        )
    if current.attendance_rate is not None and previous.attendance_rate is not None:
        delta = current.attendance_rate - previous.attendance_rate
        if abs(delta) >= 0.05:
            icon = "📈" if delta > 0 else "📉"
            lines.append(
                f"{icon} Посещаемость доступных возможностей: **{previous.attendance_rate:.0%} → {current.attendance_rate:.0%}** ({delta * 100:+.0f} п.п.)."
            )
    if not lines:
        lines.append("➖ Существенных изменений относительно предыдущего сравнимого периода нет.")
    return lines


def _period_days(start: datetime, end: datetime) -> int:
    return max(1, math.ceil((end - start).total_seconds() / 86400))


def _full_period_vacation(bundle: PeriodBundle, member: MemberContext, start: datetime, end: datetime) -> bool:
    return _vacation_covers_period(bundle, member, start, end)


def _weighted_score(components: Sequence[tuple[float | None, int]]) -> int:
    applicable = [(value, weight) for value, weight in components if value is not None]
    if not applicable:
        return 0
    total_weight = sum(weight for _, weight in applicable)
    return round(sum(float(value) * weight for value, weight in applicable) / total_weight * 100)


def family_pulse_from_bundle(
    bundle: PeriodBundle,
    start: datetime,
    end: datetime,
) -> FamilyPulse:
    analytical_events = [e for e in bundle.events if e.analytical]
    planned_events = [e for e in analytical_events if not e.spontaneous]
    spontaneous_events = [e for e in analytical_events if e.spontaneous]

    stats_by_member: dict[int, MemberStats] = {
        member.membership_id: member_stats_from_bundle(bundle, member, start, end)
        for member in bundle.members
    }

    # Pulse describes the state of the family at the end of the selected period.
    # People who left earlier remain in historical attendance, but do not depress the
    # current roster evaluation after they are no longer part of the family.
    endpoint = end - timedelta(seconds=1)
    group_mode_enabled = _group_mode_enabled(bundle.settings)
    if group_mode_enabled:
        # Academy-only content belongs to Academy analytics, not to the main-roster
        # Family Pulse rhythm/sample. Activities for the whole family still count.
        analytical_events = [e for e in analytical_events if e.audience_group != "academy"]
        planned_events = [e for e in analytical_events if not e.spontaneous]
        spontaneous_events = [e for e in analytical_events if e.spontaneous]

    # Group counts describe the current active organization, independently from the
    # Pulse eligibility rules (newcomer/vacation). They are useful for role integrity
    # diagnostics and never rewrite historical group intervals.
    main_members_count = 0
    academy_members_count = 0
    unclassified_members_count = 0
    conflict_members_count = 0
    current_roster_count = 0
    if group_mode_enabled:
        for member in bundle.members:
            if not _member_exists_at(member, endpoint):
                continue
            current_roster_count += 1
            state = _group_at(bundle.groups.get(member.membership_id, []), endpoint)
            if state == "main":
                main_members_count += 1
            elif state == "academy":
                academy_members_count += 1
            elif state == "conflict":
                conflict_members_count += 1
            else:
                unclassified_members_count += 1
    group_data_completeness = (
        (main_members_count + academy_members_count) / current_roster_count
        if group_mode_enabled and current_roster_count
        else 1.0
    )

    pulse_members: list[MemberContext] = []
    for member in bundle.members:
        if not _member_exists_at(member, endpoint):
            continue
        # A newcomer joins Family Pulse only from the next comparison period after
        # completing adaptation. This prevents a Sunday join from depressing a whole week.
        if member.joined_at + timedelta(days=bundle.settings.newcomer_days) > start:
            continue
        if _full_period_vacation(bundle, member, start, end):
            continue
        # Once Academy/Main integration is configured, Family Pulse intentionally
        # describes only the established main roster. Academy stays visible in a
        # separate overview and can never depress or inflate the main Pulse.
        if group_mode_enabled:
            state = _group_at(bundle.groups.get(member.membership_id, []), endpoint)
            if state != "main":
                continue
        pulse_members.append(member)

    availability_missing = sum(
        1 for m in pulse_members if not stats_by_member[m.membership_id].availability_configured
    )
    evaluable = [
        m for m in pulse_members if stats_by_member[m.membership_id].availability_configured
    ]
    data_completeness = len(evaluable) / len(pulse_members) if pulse_members else 0.0

    period_days = _period_days(start, end)
    required_opportunities = min(
        bundle.settings.weekly_min_opportunities,
        max(1, math.ceil(bundle.settings.weekly_min_opportunities * period_days / 7)),
    )

    schedule_coverage: float | None
    if evaluable:
        sufficiently_covered = sum(
            1
            for m in evaluable
            if stats_by_member[m.membership_id].opportunities >= required_opportunities
        )
        schedule_coverage = sufficiently_covered / len(evaluable)
    else:
        schedule_coverage = None

    # Reach is deliberately independent of schedule-profile completeness: it answers
    # the simple factual question "what share of established roster participated at
    # least once?". Missing schedule profiles must not artificially inflate reach.
    pulse_member_ids = {m.membership_id for m in pulse_members}
    attended_pulse_ids: set[int] = set()
    for event in analytical_events:
        for mid in bundle.attendees_by_event.get(event.id, set()):
            if mid in pulse_member_ids:
                attended_pulse_ids.add(mid)
    reach = (
        len(attended_pulse_ids) / len(pulse_members)
        if pulse_members and analytical_events
        else None
    )

    event_days_set = {
        local_date(e.actual_time, bundle.settings.timezone)
        for e in analytical_events
    }
    event_days = len(event_days_set)
    regularity_target_days = 0
    regularity_base_members = 0
    regularity: float | None = None
    if attended_pulse_ids:
        regularity_target_days = 1 if event_days <= 1 else 2
        # For a two-day repeat requirement, only include people who actually had
        # at least two fair opportunity days. This avoids punishing schedule groups
        # that leadership only served once in the period.
        regularity_base = [
            mid
            for mid in attended_pulse_ids
            if regularity_target_days == 1
            or stats_by_member[mid].opportunity_days >= regularity_target_days
        ]
        regularity_base_members = len(regularity_base)
        if regularity_base:
            regular_count = sum(
                1
                for mid in regularity_base
                if stats_by_member[mid].active_days >= regularity_target_days
            )
            regularity = regular_count / len(regularity_base)

    event_rates: list[float] = []
    for event in planned_events:
        eligible_members = [
            m for m in evaluable if eligible_for_denominator(m, event, bundle)[0]
        ]
        if not eligible_members:
            continue
        attended_ids = bundle.attendees_by_event.get(event.id, set())
        attended_eligible = sum(
            1 for m in eligible_members if m.membership_id in attended_ids
        )
        event_rates.append(attended_eligible / len(eligible_members))
    median_attendance = statistics.median(event_rates) if event_rates else None

    rhythm_target = min(5, max(1, math.ceil(5 * period_days / 7)))
    rhythm = min(event_days / float(rhythm_target), 1.0)

    # V2 weights emphasize actual participation first, while still auditing whether
    # leadership provides fair opportunities across schedules.
    score = _weighted_score(
        [
            (reach, 30),
            (regularity, 20),
            (schedule_coverage, 20),
            (median_attendance, 25),
            (rhythm, 5),
        ]
    )

    activity_sample = min(len(analytical_events) / 3.0, 1.0)
    planned_sample = min(len(event_rates) / 3.0, 1.0)
    period_sample = min(period_days / 7.0, 1.0)
    roster_sample = min(len(pulse_members) / 5.0, 1.0)
    confidence = (
        data_completeness * 0.25
        + activity_sample * 0.20
        + planned_sample * 0.20
        + period_sample * 0.20
        + roster_sample * 0.15
    )
    if group_mode_enabled:
        # Bad Academy/Main role hygiene lowers trust in the main-roster evaluation,
        # but does not erase otherwise valid attendance data.
        confidence *= 0.80 + 0.20 * group_data_completeness
    confidence = max(0.0, min(confidence, 1.0))
    published_score = score if analytical_events and confidence >= 0.45 else None
    provisional = published_score is None or confidence < 0.70

    category_counts = Counter(e.category for e in analytical_events)
    time_segment_counts = Counter({"morning": 0, "day": 0, "evening": 0, "night": 0})
    time_segment_counts.update(
        local_segment(e.actual_time, bundle.settings.timezone)
        for e in analytical_events
    )
    insufficient = sum(
        1
        for m in evaluable
        if stats_by_member[m.membership_id].opportunities < required_opportunities
    )
    # Factual unique attendance is intentionally broader than the Pulse denominator:
    # newcomers and members later excluded from expectations still really attended.
    # Reach itself remains established-roster-only, so this cannot inflate the score.
    unique_attendee_ids = {
        mid
        for e in analytical_events
        for mid in bundle.attendees_by_event.get(e.id, set())
    }
    if group_mode_enabled:
        unique_attendee_ids &= pulse_member_ids
    unique_attendees = len(unique_attendee_ids)

    explanations: list[str] = []
    if published_score is None:
        explanations.append(
            f"⚠️ Family Pulse пока не публикуется числом: достоверность **{confidence:.0%}** или ещё нет закрытых аналитических активностей."
        )
    elif confidence < 0.70:
        explanations.append(
            f"⚠️ Оценка пока предварительная: достоверность **{confidence:.0%}**. Нужны дополнительные закрытые активности и заполненные временные профили."
        )
    if availability_missing:
        explanations.append(
            f"⚠️ У {availability_missing} участников не настроено обычное время участия; полнота профилей **{data_completeness:.0%}**."
        )
    if group_mode_enabled:
        if conflict_members_count:
            explanations.append(
                f"⚠️ У {conflict_members_count} участников конфликт ролей: одновременно Academy и Mein Rank."
            )
        if unclassified_members_count:
            explanations.append(
                f"⚠️ У {unclassified_members_count} участников не определена группа: нет ни Academy, ни Mein Rank."
            )
    if insufficient:
        explanations.append(
            f"⚠️ {insufficient} участникам с настроенным графиком было предоставлено меньше {required_opportunities} подходящих запланированных возможностей."
        )
    if reach is not None:
        if reach >= 0.80:
            explanations.append("🟢 За период в активностях поучаствовала большая часть установленного состава.")
        elif pulse_members:
            explanations.append("🟠 Охват состава невысок: заметная часть установленного состава не участвовала ни в одной аналитической активности.")
    if median_attendance is not None:
        if median_attendance >= 0.70:
            explanations.append("🟢 Типичная посещаемость запланированных активностей высокая.")
        elif median_attendance < 0.45:
            explanations.append("🔴 Типичная посещаемость запланированных активностей ниже 45% от честно доступной аудитории.")
        else:
            explanations.append("🟠 Типичная посещаемость запланированных активностей средняя.")
    if time_segment_counts:
        least = min(time_segment_counts.items(), key=lambda x: x[1])
        most = max(time_segment_counts.items(), key=lambda x: x[1])
        if most[1] >= 3 and most[1] >= max(least[1] * 3, least[1] + 3):
            labels = {"morning": "утро", "day": "день", "evening": "вечер", "night": "ночь"}
            explanations.append(
                f"🕐 Расписание заметно перекошено: {labels[most[0]]} — {most[1]} активностей, {labels[least[0]]} — {least[1]}."
            )

    return FamilyPulse(
        score=score,
        published_score=published_score,
        label=_pulse_label(score, confidence) if published_score is not None else "⚪ Недостаточно данных для публикации Family Pulse",
        confidence=confidence,
        provisional=provisional,
        group_mode_enabled=group_mode_enabled,
        main_members=main_members_count,
        academy_members=academy_members_count,
        unclassified_members=unclassified_members_count,
        conflict_members=conflict_members_count,
        group_data_completeness=group_data_completeness,
        schedule_coverage=schedule_coverage,
        reach=reach,
        regularity=regularity,
        median_attendance=median_attendance,
        rhythm=rhythm,
        evaluable_members=len(evaluable),
        pulse_members=len(pulse_members),
        schedule_profile_missing=availability_missing,
        data_completeness=data_completeness,
        active_members=len(pulse_members),
        unique_attendees=unique_attendees,
        analytical_events=len(analytical_events),
        planned_events=len(planned_events),
        spontaneous_events=len(spontaneous_events),
        event_days=event_days,
        regularity_target_days=regularity_target_days,
        regularity_base_members=regularity_base_members,
        category_counts=dict(category_counts),
        time_segment_counts=dict(time_segment_counts),
        insufficient_opportunity_members=insufficient,
        explanations=explanations,
    )


async def family_pulse(
    repo: Repository, guild_id: int, start: datetime, end: datetime
) -> FamilyPulse:
    bundle = await load_period_bundle(repo, guild_id, start, end)
    return family_pulse_from_bundle(bundle, start, end)


def compare_pulses(current: FamilyPulse, previous: FamilyPulse) -> list[str]:
    metrics = [
        ("Покрытие расписанием", current.schedule_coverage, previous.schedule_coverage),
        ("Охват состава", current.reach, previous.reach),
        ("Регулярность участия", current.regularity, previous.regularity),
        ("Типичная посещаемость", current.median_attendance, previous.median_attendance),
        ("Ритм активных дней", current.rhythm, previous.rhythm),
    ]
    candidates = [
        (name, cur, prev)
        for name, cur, prev in metrics
        if cur is not None and prev is not None
    ]
    deltas = sorted(candidates, key=lambda x: abs(float(x[1]) - float(x[2])), reverse=True)
    explanations: list[str] = []
    for name, cur, prev in deltas[:3]:
        assert cur is not None and prev is not None
        delta = cur - prev
        if abs(delta) < 0.03:
            continue
        icon = "🟢" if delta > 0 else "🔴"
        explanations.append(
            f"{icon} {name}: {prev:.0%} → {cur:.0%} ({delta * 100:+.0f} п.п.)."
        )
    if current.published_score is not None and previous.published_score is not None:
        score_delta = current.published_score - previous.published_score
        if not explanations and score_delta:
            explanations.append(
                f"{'🟢' if score_delta > 0 else '🔴'} Family Pulse: {previous.published_score} → {current.published_score} ({score_delta:+d})."
            )
    elif not explanations:
        explanations.append("⚪ Числовой Family Pulse пока нельзя корректно сравнить: одному из периодов не хватает достоверности.")
    if not explanations:
        explanations.append("➖ Ключевые сопоставимые показатели почти не изменились.")
    if current.provisional or previous.provisional:
        explanations.append("ℹ️ Сравнение включает предварительные данные; ориентируйся также на абсолютные значения и достоверность.")
    return explanations


def daily_unique_attendance(bundle: PeriodBundle, start: datetime, end: datetime) -> dict[date, int]:
    by_day: dict[date, set[int]] = defaultdict(set)
    for event in bundle.events:
        if not event.analytical:
            continue
        d = local_date(event.actual_time, bundle.settings.timezone)
        for membership_id in bundle.attendees_by_event.get(event.id, set()):
            by_day[d].add(membership_id)
    current = local_date(start, bundle.settings.timezone)
    last = local_date(end - timedelta(seconds=1), bundle.settings.timezone)
    result: dict[date, int] = {}
    while current <= last:
        result[current] = len(by_day.get(current, set()))
        current += timedelta(days=1)
    return result

@dataclass(slots=True)
class GroupOverview:
    group_name: str
    members: int
    unique_attendees: int
    coverage: float | None
    active_days_total: int
    total_attendances: int
    rating_counts: dict[str, int]
    candidate_membership_ids: list[int]


def group_overview_from_bundle(
    bundle: PeriodBundle,
    start: datetime,
    end: datetime,
    group_name: str,
) -> GroupOverview:
    if group_name not in {"academy", "main"}:
        raise ValueError("Unsupported group")
    endpoint = end - timedelta(seconds=1)
    members = [
        member
        for member in bundle.members
        if _member_exists_at(member, endpoint)
        and _group_at(bundle.groups.get(member.membership_id, []), endpoint) == group_name
    ]
    stats = [member_stats_from_bundle(bundle, member, start, end) for member in members]
    unique_attendees = sum(1 for stat in stats if stat.total_attended_events > 0)
    rating_counts = Counter(stat.rating_key for stat in stats)
    candidate_ids: list[int] = []
    if group_name == "academy":
        for member, stat in zip(members, stats, strict=True):
            # This is only a "review candidate" signal, never an automatic transfer.
            # It deliberately requires both a positive rating and real attendance.
            if (
                stat.rating_key in {"very_high", "high"}
                and stat.total_attended_events >= 3
                and end >= member.joined_at + timedelta(days=bundle.settings.newcomer_days)
            ):
                candidate_ids.append(member.membership_id)
    return GroupOverview(
        group_name=group_name,
        members=len(members),
        unique_attendees=unique_attendees,
        coverage=(unique_attendees / len(members)) if members else None,
        active_days_total=sum(stat.active_days for stat in stats),
        total_attendances=sum(stat.total_attended_events for stat in stats),
        rating_counts=dict(rating_counts),
        candidate_membership_ids=candidate_ids,
    )


async def group_overview(
    repo: Repository,
    guild_id: int,
    start: datetime,
    end: datetime,
    group_name: str,
) -> GroupOverview:
    bundle = await load_period_bundle(repo, guild_id, start, end)
    return group_overview_from_bundle(bundle, start, end, group_name)
