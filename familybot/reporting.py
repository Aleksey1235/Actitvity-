from __future__ import annotations

import asyncio
import csv
import io
from collections import Counter
from datetime import datetime, timedelta

import discord

from .analytics import (
    CATEGORY_LABELS,
    compare_pulses,
    daily_unique_attendance,
    eligible_for_denominator,
    family_pulse_from_bundle,
    load_period_bundle,
)
from .charting import weekly_comparison_png
from .repository import DomainError, Repository
from .timeutil import dt_to_discord_timestamp, local_date, parse_iso

WEEKDAY_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def pct(v: float | None) -> str:
    return "—" if v is None else f"{v:.0%}"


async def weekly_report_payload(
    repo: Repository,
    guild_id: int,
    start: datetime,
    end: datetime,
    previous_start: datetime,
    previous_end: datetime,
) -> tuple[discord.Embed, discord.File, dict, list[str]]:
    current_bundle = await load_period_bundle(repo, guild_id, start, end)
    previous_bundle = await load_period_bundle(repo, guild_id, previous_start, previous_end)
    current = family_pulse_from_bundle(current_bundle, start, end)
    previous = family_pulse_from_bundle(previous_bundle, previous_start, previous_end)
    changes = compare_pulses(current, previous)

    category_lines = [
        f"{CATEGORY_LABELS.get(key, key)} — **{current.category_counts.get(key, 0)}**"
        for key in ("training", "family", "faction")
    ]
    segment_names = {
        "morning": "🌅 Утро",
        "day": "☀️ День",
        "evening": "🌆 Вечер",
        "night": "🌙 Ночь",
    }
    segment_lines = [
        f"{segment_names[key]} — **{current.time_segment_counts.get(key, 0)}**"
        for key in ("morning", "day", "evening", "night")
    ]

    current_score_text = f"{current.published_score}/100" if current.published_score is not None else "—/100"
    previous_score_text = f"{previous.published_score}/100" if previous.published_score is not None else "—/100"
    delta_text = (
        f"{current.published_score - previous.published_score:+d}"
        if current.published_score is not None and previous.published_score is not None
        else "—"
    )
    embed = discord.Embed(
        title="📊 Недельный отчёт семьи · статистика v3",
        description=(
            f"Период: **{local_date(start, current_bundle.settings.timezone)} — "
            f"{local_date(end - timedelta(seconds=1), current_bundle.settings.timezone)}**\n\n"
            f"## 💓 {current_score_text} · {current.label}\n"
            f"Достоверность: **{current.confidence:.0%}**\n"
            f"Предыдущий сравнимый период: **{previous_score_text}** · изменение **{delta_text}**"
        ),
    )
    embed.add_field(
        name="👥 Состав и вовлечение",
        value=(
            f"В расчёте Pulse: **{current.pulse_members}**\n"
            f"Уникальных участников: **{current.unique_attendees}**\n"
            f"Охват состава: **{pct(current.reach)}**\n"
            f"Регулярность: **{pct(current.regularity)}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="🎯 Честность расписания",
        value=(
            f"Покрытие расписанием: **{pct(current.schedule_coverage)}**\n"
            f"Типичная посещаемость: **{pct(current.median_attendance)}**\n"
            f"Мало возможностей: **{current.insufficient_opportunity_members}**\n"
            f"Полнота профилей: **{pct(current.data_completeness)}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="🎮 Активности",
        value=(
            f"Всего аналитических: **{current.analytical_events}**\n"
            f"Плановых: **{current.planned_events}**\n"
            f"Спонтанных: **{current.spontaneous_events}**\n"
            f"Дней с активностями: **{current.event_days}**"
        ),
        inline=True,
    )
    embed.add_field(name="Категории", value="\n".join(category_lines), inline=True)
    embed.add_field(name="По времени", value="\n".join(segment_lines), inline=True)
    embed.add_field(name="Почему Pulse изменился", value="\n".join(changes), inline=False)
    if current.explanations:
        embed.add_field(name="Диагностика", value="\n".join(current.explanations[:6]), inline=False)

    current_daily_map = daily_unique_attendance(current_bundle, start, end)
    previous_daily_map = daily_unique_attendance(previous_bundle, previous_start, previous_end)
    current_vals = list(current_daily_map.values())
    previous_vals = list(previous_daily_map.values())
    length = max(len(current_vals), len(previous_vals))
    labels = WEEKDAY_RU[:length]
    current_vals += [0] * (length - len(current_vals))
    previous_vals += [0] * (length - len(previous_vals))
    graph = await asyncio.to_thread(
        weekly_comparison_png,
        labels,
        current_vals,
        previous_vals,
        subtitle=(
            f"{local_date(start, current_bundle.settings.timezone)} — "
            f"{local_date(end - timedelta(seconds=1), current_bundle.settings.timezone)} · "
            "уникальные участники"
        ),
    )
    file = discord.File(graph, filename="family_activity_week.png")
    embed.set_image(url="attachment://family_activity_week.png")

    metrics = {
        "statistics_version": 3,
        "score": current.score,
        "published_score": current.published_score,
        "confidence": current.confidence,
        "provisional": current.provisional,
        "schedule_coverage": current.schedule_coverage,
        "reach": current.reach,
        "regularity": current.regularity,
        "median_attendance": current.median_attendance,
        "rhythm": current.rhythm,
        "active_members": current.active_members,
        "pulse_members": current.pulse_members,
        "data_completeness": current.data_completeness,
        "evaluable_members": current.evaluable_members,
        "unique_attendees": current.unique_attendees,
        "analytical_events": current.analytical_events,
        "planned_events": current.planned_events,
        "spontaneous_events": current.spontaneous_events,
        "event_days": current.event_days,
        "regularity_base_members": current.regularity_base_members,
        "category_counts": current.category_counts,
        "time_segment_counts": current.time_segment_counts,
        "insufficient_opportunity_members": current.insufficient_opportunity_members,
    }
    return embed, file, metrics, changes + current.explanations


SOURCE_LABELS = {
    "primary_code": "Основной код",
    "late_code": "Поздний код",
    "control_code": "Контрольный код",
    "manual": "Вручную",
}


def _activity_audience_text(row) -> str:
    group = row["audience_group"] if "audience_group" in row.keys() else None
    if group == "academy":
        return "🎓 Только Academy"
    if group == "main":
        return "🏠 Только основной состав (Mein Rank)"
    kind = str(row["audience_type"])
    if kind == "all":
        return "Вся семья"
    if kind == "rank_range":
        return f"Ранги {row['min_rank']}–{row['max_rank']}"
    return "Выбранные участники"


def _activity_mode_text(row) -> str:
    if bool(row["is_spontaneous"]):
        return "⚡ Спонтанная"
    return "📅 Запланированная"


def _activity_csv(rows) -> discord.File:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow([
        "nickname",
        "static_id",
        "discord_user_id",
        "rank",
        "source",
        "first_seen_at",
        "control_confirmed",
    ])
    for row in rows:
        writer.writerow([
            row["nickname"],
            row["static_id"],
            row["discord_user_id"] or "",
            row["rank"],
            row["source"],
            row["first_seen_at"],
            "yes" if row["control_confirmed"] else "no",
        ])
    raw = buffer.getvalue().encode("utf-8-sig")
    return discord.File(io.BytesIO(raw), filename="participants.csv")


async def activity_report_payload(
    repo: Repository,
    guild_id: int,
    activity_id: int,
) -> tuple[discord.Embed, discord.File]:
    """Build the canonical confidential report for one completed activity."""
    row = await repo.get_activity(activity_id)
    if not row or int(row["guild_id"]) != int(guild_id):
        raise DomainError("Активность не найдена на этом сервере")
    if row["status"] not in {"closed", "finalized"}:
        raise DomainError("Отчёт доступен после завершения активности")

    attendance = await repo.attendance_for_activity(activity_id)
    evidence = await repo.list_activity_evidence(activity_id)
    source_counts = Counter(str(r["source"]) for r in attendance)
    control_count = sum(1 for r in attendance if bool(r["control_confirmed"]))

    scheduled = parse_iso(str(row["scheduled_for"]))
    started = parse_iso(str(row["started_at"])) if row["started_at"] else None
    ended = parse_iso(str(row["ended_at"])) if row["ended_at"] else None

    # Calculate a fair event-level denominator from the exact same engine as member stats.
    window_start = scheduled - timedelta(seconds=1)
    window_end = scheduled + timedelta(seconds=1)
    bundle = await load_period_bundle(repo, guild_id, window_start, window_end)
    event = next((e for e in bundle.events if e.id == activity_id), None)
    fair_eligible = None
    fair_attended = None
    if event is not None and event.analytical and not event.spontaneous:
        eligible_members = [m for m in bundle.members if eligible_for_denominator(m, event, bundle)[0]]
        fair_eligible = len(eligible_members)
        attendee_ids = {int(r["membership_id"]) for r in attendance}
        fair_attended = sum(1 for m in eligible_members if m.membership_id in attendee_ids)

    embed = discord.Embed(
        title=f"✅ Отчёт активности #{activity_id} · {row['title']}",
        description=row["description"] or "",
    )
    embed.add_field(name="Категория", value=CATEGORY_LABELS.get(str(row["category"]), str(row["category"])), inline=True)
    embed.add_field(name="Режим", value=_activity_mode_text(row), inline=True)
    embed.add_field(name="Статистика", value="📊 Аналитическая" if row["analytical"] else "📎 Только история", inline=True)
    embed.add_field(name="Аудитория", value=_activity_audience_text(row), inline=True)
    embed.add_field(
        name="Время",
        value=(
            f"План: {dt_to_discord_timestamp(scheduled, 'F')}\n"
            f"Старт: {dt_to_discord_timestamp(started, 'T') if started else '—'}\n"
            f"Финиш: {dt_to_discord_timestamp(ended, 'T') if ended else '—'}"
        ),
        inline=True,
    )
    organizer = (
        f"<@{row['organizer_discord_user_id']}>"
        if row["organizer_discord_user_id"]
        else str(row["organizer_nickname"])
    )
    embed.add_field(name="Организатор", value=organizer, inline=True)

    source_lines = [
        f"{SOURCE_LABELS.get(key, key)}: **{source_counts.get(key, 0)}**"
        for key in ("primary_code", "late_code", "manual")
    ]
    if control_count:
        source_lines.append(f"Контроль подтверждён: **{control_count}**")
    embed.add_field(
        name=f"👥 Участники · {len(attendance)}",
        value="\n".join(source_lines),
        inline=True,
    )

    if fair_eligible is None:
        fair_text = "—\nСпонтанная/неаналитическая активность не создаёт персональных пропусков."
    elif fair_eligible == 0:
        fair_text = "**0** честно доступных участников · процент не рассчитывается."
    else:
        rate_value = fair_attended / fair_eligible if fair_attended is not None else 0
        fair_text = f"**{fair_attended}/{fair_eligible}** · **{rate_value:.0%}**"
    embed.add_field(name="🎯 Честная посещаемость", value=fair_text, inline=True)

    if row["closing_note"]:
        embed.add_field(name="📝 Комментарий организатора", value=str(row["closing_note"])[:1024], inline=False)

    evidence_urls: list[tuple[str, str | None, str | None]] = []
    if row["evidence_url"]:
        evidence_urls.append((str(row["evidence_url"]), "Ссылка из формы завершения", None))
    for ev in evidence:
        shown_url = ev["mirrored_url"] if "mirrored_url" in ev.keys() and ev["mirrored_url"] else ev["url"]
        evidence_urls.append((str(shown_url), ev["filename"], ev["content_type"]))
    if evidence_urls:
        links = []
        for idx, (url, filename, _content_type) in enumerate(evidence_urls[:8], start=1):
            label = filename or f"Доказательство {idx}"
            links.append(f"[{label}]({url})")
        embed.add_field(name="📷 Доказательства", value="\n".join(links), inline=False)
        # Uploaded proof files are mirrored as replies *after* the report card.
        # Keeping them out of the main message attachments prevents Discord from
        # rendering a giant image above the embed (the old v1.2 behaviour).

    preview_lines = []
    for r in attendance[:15]:
        mention = f"<@{r['discord_user_id']}>" if r["discord_user_id"] else str(r["nickname"])
        control = " · 🔒" if r["control_confirmed"] else ""
        preview_lines.append(f"• **{r['nickname']}** `{r['static_id']}` · {mention}{control}")
    if preview_lines:
        if len(attendance) > 15:
            preview_lines.append(f"…и ещё **{len(attendance) - 15}** — полный список в CSV.")
        embed.add_field(name="Состав активности", value="\n".join(preview_lines), inline=False)
    else:
        embed.add_field(name="Состав активности", value="Никто не был подтверждён.", inline=False)

    embed.set_footer(text="Отчёт конфиденциальный. Полный список участников прикреплён CSV-файлом.")
    return embed, _activity_csv(attendance)
