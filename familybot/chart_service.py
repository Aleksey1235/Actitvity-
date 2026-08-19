from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from io import BytesIO

from .analytics import family_pulse, load_period_bundle
from .charting import (
    WEEKDAY_RU,
    activity_heatmap_png,
    category_distribution_png,
    group_coverage_png,
    pulse_history_png,
    weekly_comparison_png,
)
from .repository import DomainError, Repository
from .timeutil import current_week_bounds, local_date, local_segment, parse_iso, utcnow, validate_timezone


MONTHS_RU = [
    "янв", "фев", "мар", "апр", "май", "июн",
    "июл", "авг", "сен", "окт", "ноя", "дек",
]


@dataclass(slots=True)
class ChartPayload:
    title: str
    description: str
    filename: str
    image: BytesIO


@dataclass(slots=True)
class GroupCoverageMetrics:
    main_members: int
    main_unique: int
    academy_members: int
    academy_unique: int



def _day_month(dt: datetime, tz_name: str) -> str:
    local = dt.astimezone(validate_timezone(tz_name))
    return f"{local.day} {MONTHS_RU[local.month - 1]}"


def _period_label(start: datetime, end: datetime, tz_name: str) -> str:
    last = end - timedelta(microseconds=1)
    return f"{_day_month(start, tz_name)} — {_day_month(last, tz_name)}"


async def build_week_chart(repo: Repository, guild_id: int) -> ChartPayload:
    settings = await repo.ensure_guild_settings(guild_id)
    now = utcnow()
    start, week_end = current_week_bounds(now, settings.timezone)
    end = min(now, week_end)
    previous_start = start - timedelta(days=7)
    previous_end = previous_start + (end - start)

    from .analytics import daily_unique_attendance

    current_bundle = await load_period_bundle(repo, guild_id, start, end)
    previous_bundle = await load_period_bundle(repo, guild_id, previous_start, previous_end)
    current_map = daily_unique_attendance(current_bundle, start, end)
    previous_map = daily_unique_attendance(previous_bundle, previous_start, previous_end)
    current_vals = list(current_map.values())
    previous_vals = list(previous_map.values())
    length = max(len(current_vals), len(previous_vals))
    labels = WEEKDAY_RU[:length]
    current_vals += [0] * (length - len(current_vals))
    previous_vals += [0] * (length - len(previous_vals))
    image = await asyncio.to_thread(
        weekly_comparison_png,
        labels,
        current_vals,
        previous_vals,
        subtitle=f"Текущая неделя: {_period_label(start, end, settings.timezone)} · сравнение с теми же днями прошлой",
    )
    return ChartPayload(
        title="📈 Активность по дням",
        description=(
            "Количество **уникальных участников**, посетивших хотя бы одну аналитическую активность в каждый день. "
            "Несколько мероприятий одного человека в один день не увеличивают показатель."
        ),
        filename="activity_week_comparison.png",
        image=image,
    )


async def build_pulse_history_chart(repo: Repository, guild_id: int, *, weeks: int = 12) -> ChartPayload:
    settings = await repo.ensure_guild_settings(guild_id)
    rows = await repo.db.fetchall(
        """
        SELECT week_start, week_end, pulse_score, metrics_json
        FROM weekly_reports
        WHERE guild_id=?
        ORDER BY week_start DESC
        LIMIT ?
        """,
        (guild_id, int(max(2, min(26, weeks)))),
    )
    rows = list(reversed(rows))
    labels: list[str] = []
    scores: list[float | None] = []
    confidences: list[float | None] = []
    for row in rows:
        metrics = json.loads(str(row["metrics_json"]))
        start = parse_iso(str(row["week_start"]))
        published = metrics.get("published_score") if "published_score" in metrics else row["pulse_score"]
        confidence = metrics.get("confidence")
        labels.append(_day_month(start, settings.timezone))
        scores.append(float(published) if published is not None else None)
        confidences.append(float(confidence) if confidence is not None else None)

    # Add the current live week as a final point without mutating immutable snapshots.
    now = utcnow()
    current_start, current_week_end = current_week_bounds(now, settings.timezone)
    current_end = min(now, current_week_end)
    current = await family_pulse(repo, guild_id, current_start, current_end)
    if not labels or (rows and parse_iso(str(rows[-1]["week_start"])) < current_start):
        labels.append(f"{_day_month(current_start, settings.timezone)}*" )
        scores.append(float(current.published_score) if current.published_score is not None else None)
        confidences.append(float(current.confidence))

    image = await asyncio.to_thread(pulse_history_png, labels, scores, confidences)
    return ChartPayload(
        title="💓 Family Pulse по неделям",
        description=(
            "История опубликованных недельных оценок. `*` — текущая незавершённая неделя. "
            "Если достоверности недостаточно, бот оставляет разрыв вместо ложного нуля."
        ),
        filename="family_pulse_history.png",
        image=image,
    )


async def build_categories_chart(repo: Repository, guild_id: int, *, days: int = 28) -> ChartPayload:
    settings = await repo.ensure_guild_settings(guild_id)
    end = utcnow()
    start = end - timedelta(days=days)
    bundle = await load_period_bundle(repo, guild_id, start, end)
    counts = {"training": 0, "family": 0, "faction": 0}
    for event in bundle.events:
        if event.analytical:
            counts[event.category] = counts.get(event.category, 0) + 1
    label = f"Последние {days} дней · {_period_label(start, end, settings.timezone)}"
    return ChartPayload(
        title="🎮 Категории контента",
        description="Сколько аналитических активностей каждого типа провела семья за выбранный период.",
        filename="activity_categories.png",
        image=await asyncio.to_thread(category_distribution_png, counts, period_label=label),
    )


def _group_at_rows(rows, when: datetime) -> str | None:
    for row in rows:
        starts = parse_iso(str(row["starts_at"]))
        ends = parse_iso(str(row["ends_at"])) if row["ends_at"] else None
        if starts <= when and (ends is None or when < ends):
            return str(row["group_name"])
    return None


def group_coverage_metrics(bundle, end: datetime) -> GroupCoverageMetrics:
    endpoint = end - timedelta(microseconds=1)
    current_groups: dict[int, str] = {}
    for member in bundle.members:
        group = _group_at_rows(bundle.groups.get(member.membership_id, []), endpoint)
        if group in {"main", "academy"}:
            current_groups[member.membership_id] = group

    main_ids = {mid for mid, group in current_groups.items() if group == "main"}
    academy_ids = {mid for mid, group in current_groups.items() if group == "academy"}
    main_attendees: set[int] = set()
    academy_attendees: set[int] = set()

    for event in bundle.events:
        if not event.analytical:
            continue
        for mid in bundle.attendees_by_event.get(event.id, set()):
            current_group = current_groups.get(mid)
            if current_group is None:
                continue
            group_then = _group_at_rows(bundle.groups.get(mid, []), event.actual_time)
            if current_group == "main" and group_then == "main":
                main_attendees.add(mid)
            elif current_group == "academy" and group_then == "academy":
                academy_attendees.add(mid)

    return GroupCoverageMetrics(
        main_members=len(main_ids),
        main_unique=len(main_attendees),
        academy_members=len(academy_ids),
        academy_unique=len(academy_attendees),
    )


async def build_groups_chart(repo: Repository, guild_id: int, *, days: int = 28) -> ChartPayload:
    settings = await repo.ensure_guild_settings(guild_id)
    if not settings.academy_role_id or not settings.main_role_id:
        raise DomainError("Сначала настрой роли Academy и Mein Rank через `/семья настройка`.")
    end = utcnow()
    start = end - timedelta(days=days)
    bundle = await load_period_bundle(repo, guild_id, start, end)
    # Historical correctness: a later Academy→Main transition must not rewrite
    # which group received the earlier attendance.
    metrics = group_coverage_metrics(bundle, end)
    label = f"Последние {days} дней · {_period_label(start, end, settings.timezone)}"
    return ChartPayload(
        title="👥 Охват Main и Academy",
        description=(
            "Сравнение текущих групп по охвату. Посещение засчитывается группе только если человек состоял "
            "в ней в момент активности; переход Academy → Main не переписывает прошлое."
        ),
        filename="group_coverage.png",
        image=await asyncio.to_thread(
            group_coverage_png,
            main_members=metrics.main_members,
            main_unique=metrics.main_unique,
            academy_members=metrics.academy_members,
            academy_unique=metrics.academy_unique,
            period_label=label,
        ),
    )


async def build_schedule_heatmap(repo: Repository, guild_id: int, *, days: int = 28) -> ChartPayload:
    settings = await repo.ensure_guild_settings(guild_id)
    end = utcnow()
    start = end - timedelta(days=days)
    bundle = await load_period_bundle(repo, guild_id, start, end)
    segment_index = {"morning": 0, "day": 1, "evening": 2, "night": 3}
    matrix = [[0, 0, 0, 0] for _ in range(7)]
    counted = 0
    for event in bundle.events:
        if not event.analytical:
            continue
        weekday = event.scheduled_for.astimezone(validate_timezone(settings.timezone)).weekday()
        segment = local_segment(event.scheduled_for, settings.timezone)
        matrix[weekday][segment_index[segment]] += 1
        counted += 1
    label = f"Последние {days} дней · {_period_label(start, end, settings.timezone)} · событий: {counted}"
    return ChartPayload(
        title="🕐 Карта расписания активностей",
        description=(
            "Показывает, **когда руководство планирует аналитический контент**. Это диагностический график для поиска перекосов: "
            "например, если почти все активности проходят вечером, ночную группу нельзя честно обвинять в низкой посещаемости."
        ),
        filename="activity_schedule_heatmap.png",
        image=await asyncio.to_thread(activity_heatmap_png, matrix, period_label=label),
    )


async def build_chart(
    repo: Repository,
    guild_id: int,
    chart_type: str,
    *,
    days: int = 28,
) -> ChartPayload:
    normalized = chart_type.strip().lower()
    if normalized == "week":
        return await build_week_chart(repo, guild_id)
    if normalized == "pulse":
        return await build_pulse_history_chart(repo, guild_id)
    if normalized == "categories":
        return await build_categories_chart(repo, guild_id, days=days)
    if normalized == "groups":
        return await build_groups_chart(repo, guild_id, days=days)
    if normalized == "schedule":
        return await build_schedule_heatmap(repo, guild_id, days=days)
    raise DomainError("Неизвестный тип графика")
