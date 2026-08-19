from __future__ import annotations

from datetime import timedelta

import discord

from .analytics import (
    CATEGORY_LABELS,
    compare_member_stats,
    compare_pulses,
    family_pulse,
    group_overview,
    load_period_bundle,
    member_stats_from_bundle,
)
from .repository import Repository
from .timeutil import current_week_bounds, dt_to_discord_timestamp, parse_iso, utcnow


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def _pulse_score_line(score: int | None, label: str, confidence: float) -> str:
    shown = f"{score}/100" if score is not None else "—/100"
    return f"### 💓 Family Pulse: **{shown}**\n{label}\nДостоверность оценки: **{confidence:.0%}**"


async def build_public_dashboard_embed(repo: Repository, guild_id: int) -> discord.Embed:
    settings = await repo.ensure_guild_settings(guild_id)
    embed = discord.Embed(
        title="🏠 FAMILY ACTIVITY",
        description=(
            "Панель участника семьи. Здесь можно подтвердить участие по коду и посмотреть "
            "только свои данные.\n\n"
            "🔒 **Общая аналитика семьи, Family Pulse, список состава и служебные данные здесь не показываются.**"
        ),
    )
    embed.add_field(
        name="Как пользоваться",
        value=(
            "**1.** На активности получи код у организатора.\n"
            "**2.** Нажми **Отметиться** и введи код.\n"
            "**3.** Своя статистика и история открываются приватно только тебе."
        ),
        inline=False,
    )
    if settings.vacation_role_id:
        embed.add_field(
            name="💤 Отпуск",
            value="Отпуска ведёт другой бот. Family Activity автоматически учитывает роль отпуска в статистике.",
            inline=False,
        )
    embed.set_footer(text="Все ответы кнопок этой панели отправляются приватно.")
    return embed


async def build_my_profile_embed(repo: Repository, guild_id: int, membership_id: int) -> discord.Embed:
    row = await repo.get_membership(membership_id)
    if not row or int(row["guild_id"]) != int(guild_id):
        raise ValueError("Membership not found")
    open_vacation = await repo.get_open_role_vacation(membership_id)
    availability = await repo.availability_at(membership_id, utcnow())
    labels = {
        "morning": "🌅 Утро", "day": "☀️ День", "evening": "🌆 Вечер",
        "night": "🌙 Ночь", "floating": "🔄 Плавающий",
    }
    grouped: dict[str, list[str]] = {"weekday": [], "weekend": []}
    for item in availability:
        grouped.setdefault(str(item["day_group"]), []).append(labels.get(str(item["segment"]), str(item["segment"])))
    joined = parse_iso(row["joined_at"])
    embed = discord.Embed(
        title=f"👤 {row['nickname']} | {row['static_id']}",
        description="Твой профиль в системе Family Activity.",
    )
    embed.add_field(name="Ранг", value=f"R{row['rank']}", inline=True)
    embed.add_field(name="В семье с", value=dt_to_discord_timestamp(joined, "D"), inline=True)
    embed.add_field(name="Статус", value="💤 В отпуске" if open_vacation else "🟢 В составе", inline=True)
    group_row = await repo.get_open_group_period(membership_id)
    group_label = {
        "academy": "🎓 Academy",
        "main": "🏠 Основной состав",
        "unclassified": "⚪ Группа не определена",
        "conflict": "⚠️ Конфликт Academy/Mein Rank",
    }.get(str(group_row["group_name"]) if group_row else "", "—")
    embed.add_field(name="Группа", value=group_label, inline=True)
    schedule = (
        f"Будни: {', '.join(grouped.get('weekday', [])) or 'не настроено'}\n"
        f"Выходные: {', '.join(grouped.get('weekend', [])) or 'не настроено'}"
    )
    embed.add_field(name="🕐 Обычное время участия", value=schedule, inline=False)
    embed.set_footer(text="Изменение игрового времени влияет только на будущую аналитику и не переписывает прошлое.")
    return embed


async def build_my_activities_embed(repo: Repository, guild_id: int, membership_id: int, *, limit: int = 15) -> discord.Embed:
    membership = await repo.get_membership(membership_id)
    if not membership or int(membership["guild_id"]) != int(guild_id):
        raise ValueError("Membership not found")
    rows = await repo.db.fetchall(
        """
        SELECT a.id, a.title, a.category, a.scheduled_for, a.is_spontaneous, at.source,
               EXISTS(SELECT 1 FROM attendance_checks ac WHERE ac.attendance_id=at.id AND ac.kind='control') AS control_confirmed
        FROM attendance at
        JOIN activities a ON a.id=at.activity_id
        WHERE at.membership_id=? AND at.removed_at IS NULL
          AND a.status IN ('closed','finalized','running')
        ORDER BY a.scheduled_for DESC
        LIMIT ?
        """,
        (membership_id, int(limit)),
    )
    lines = []
    for r in rows:
        when = parse_iso(r["scheduled_for"])
        category = CATEGORY_LABELS.get(str(r["category"]), str(r["category"]))
        spontaneous = " · ⚡ спонтанная" if r["is_spontaneous"] else ""
        control = " · 🔒 контроль" if r["control_confirmed"] else ""
        lines.append(
            f"• `#{r['id']}` · {dt_to_discord_timestamp(when, 'd')} · {category} · **{r['title']}**{spontaneous}{control}"
        )
    embed = discord.Embed(
        title=f"🎮 Мои активности · {membership['nickname']}",
        description="\n".join(lines) if lines else "Участия пока не зафиксированы.",
    )
    embed.set_footer(text="Любое подтверждённое посещение сохраняется. Спонтанные активности не создают пропусков.")
    return embed


async def build_staff_dashboard_embed(repo: Repository, guild_id: int) -> discord.Embed:
    settings = await repo.ensure_guild_settings(guild_id)
    now = utcnow()
    week_start, week_end = current_week_bounds(now, settings.timezone)
    current_end = min(now, week_end)
    prev_start = week_start - timedelta(days=7)
    prev_end = prev_start + (current_end - week_start)

    current = await family_pulse(repo, guild_id, week_start, current_end)
    previous = await family_pulse(repo, guild_id, prev_start, prev_end)
    active_members = await repo.list_active_members(guild_id)
    open_vacations = await repo.list_open_role_vacations(guild_id)
    vacation_ids = {int(v["membership_id"]) for v in open_vacations}
    newcomer_count = sum(
        1
        for r in active_members
        if now < parse_iso(r["joined_at"]) + timedelta(days=settings.newcomer_days)
    )

    if current.published_score is not None and previous.published_score is not None:
        delta = current.published_score - previous.published_score
        delta_text = f"{delta:+d}" if delta else "±0"
    else:
        delta_text = "— (пока несопоставимо)"
    embed = discord.Embed(
        title="🛡️ FAMILY CONTROL · РУКОВОДСТВО",
        description=(
            f"{_pulse_score_line(current.published_score, current.label, current.confidence)}\n"
            f"К сравнимому периоду: **{delta_text}**\n\n"
            + ("Family Pulse относится к **основному составу (Mein Rank)**. Academy анализируется отдельно." if current.group_mode_enabled else "Оценка относится к семье целиком. Личных баллов у участников нет.")
        ),
    )
    composition = (
        f"В семье: **{len(active_members)}**\n"
        f"🏠 Основной: **{current.main_members}**\n"
        f"🎓 Academy: **{current.academy_members}**\n"
        f"⚠️ Конфликт ролей: **{current.conflict_members}**\n"
        f"⚪ Без группы: **{current.unclassified_members}**\n"
        f"🆕 Новички: **{newcomer_count}** · 💤 Отпуск: **{len(vacation_ids)}**"
        if current.group_mode_enabled
        else (
            f"В семье: **{len(active_members)}**\n"
            f"В расчёте Pulse: **{current.pulse_members}**\n"
            f"🆕 Новички: **{newcomer_count}**\n"
            f"💤 Сейчас в отпуске: **{len(vacation_ids)}**"
        )
    )
    embed.add_field(name="👥 Состав", value=composition, inline=True)
    embed.add_field(
        name="🎮 Эта неделя",
        value=(
            f"Аналитических: **{current.analytical_events}**\n"
            f"Плановых: **{current.planned_events}**\n"
            f"Спонтанных: **{current.spontaneous_events}**\n"
            f"Уникальных участников: **{current.unique_attendees}**\n"
            f"Дней с активностями: **{current.event_days}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="📊 Качество статистики",
        value=(
            f"Охват состава: **{pct(current.reach)}**\n"
            f"Регулярность участников: **{pct(current.regularity)}**\n"
            f"Покрытие расписанием: **{pct(current.schedule_coverage)}**\n"
            f"Типичная посещаемость: **{pct(current.median_attendance)}**\n"
            f"Полнота профилей: **{pct(current.data_completeness)}**\n"
            f"Достоверность Pulse: **{current.confidence:.0%}**"
        ),
        inline=False,
    )
    if current.explanations:
        embed.add_field(name="🧠 Что важно сейчас", value="\n".join(current.explanations[:5]), inline=False)
    embed.set_footer(text="Текущая неделя сравнивается только с теми же днями прошлой недели. Низкая достоверность всегда помечается явно.")
    return embed


async def build_family_pulse_embed(repo: Repository, guild_id: int) -> discord.Embed:
    settings = await repo.ensure_guild_settings(guild_id)
    now = utcnow()
    week_start, week_end = current_week_bounds(now, settings.timezone)
    current_end = min(now, week_end)
    prev_start = week_start - timedelta(days=7)
    prev_end = prev_start + (current_end - week_start)
    current = await family_pulse(repo, guild_id, week_start, current_end)
    previous = await family_pulse(repo, guild_id, prev_start, prev_end)
    if current.published_score is not None and previous.published_score is not None:
        delta_text = f"{current.published_score - previous.published_score:+d}"
    else:
        delta_text = "—"
    score_text = f"{current.published_score}/100" if current.published_score is not None else "—/100"

    embed = discord.Embed(
        title="💓 Family Pulse · статистика v3",
        description=(
            f"## **{score_text}** · {current.label}\n"
            f"Изменение к сравнимому периоду: **{delta_text}**\n"
            f"Достоверность: **{current.confidence:.0%}**"
            + ("\nКонтур оценки: **основной состав (Mein Rank)**" if current.group_mode_enabled else "")
        ),
    )
    embed.add_field(name="Охват состава · 30%", value=pct(current.reach), inline=True)
    embed.add_field(name="Регулярность · 20%", value=pct(current.regularity), inline=True)
    embed.add_field(name="Покрытие расписанием · 20%", value=pct(current.schedule_coverage), inline=True)
    embed.add_field(name="Посещаемость событий · 25%", value=pct(current.median_attendance), inline=True)
    embed.add_field(name="Ритм · 5%", value=pct(current.rhythm), inline=True)
    embed.add_field(
        name="Качество исходных данных",
        value=(
            f"В расчёте состава: **{current.pulse_members}**\n"
            f"С настроенным временем: **{current.evaluable_members}**\n"
            f"Без графика: **{current.schedule_profile_missing}**\n"
            f"Мало возможностей: **{current.insufficient_opportunity_members}**\n"
            f"База регулярности: **{current.regularity_base_members}**\n"
            f"Полнота профилей: **{pct(current.data_completeness)}**"
            + (f"\nКлассификация Academy/Main: **{pct(current.group_data_completeness)}**" if current.group_mode_enabled else "")
        ),
        inline=True,
    )
    reasons = compare_pulses(current, previous)
    embed.add_field(name="Почему оценка изменилась", value="\n".join(reasons), inline=False)
    if current.explanations:
        embed.add_field(name="Диагностика", value="\n".join(current.explanations[:6]), inline=False)
    embed.set_footer(text="Если показателю не хватает применимых данных, бот показывает «—», а не подставляет ложный 0%.")
    return embed


async def build_member_stats_embed(
    repo: Repository,
    guild_id: int,
    membership_id: int,
    *,
    staff_view: bool,
) -> discord.Embed:
    settings = await repo.ensure_guild_settings(guild_id)
    end = utcnow()
    start = end - timedelta(days=settings.member_eval_days)
    bundle = await load_period_bundle(repo, guild_id, start, end)
    member = next((m for m in bundle.members if m.membership_id == membership_id), None)
    if member is None:
        row = await repo.get_membership(membership_id)
        if row is None:
            raise ValueError("Membership not found")
        return discord.Embed(
            title=f"👤 {row['nickname']} | {row['static_id']}",
            description="За выбранный период данных нет.",
        )
    stat = member_stats_from_bundle(bundle, member, start, end)

    # Independent 7-day trend. This compares facts with facts, not an incomplete
    # 28-day rating against a complete previous month.
    recent_start = end - timedelta(days=7)
    previous_start = end - timedelta(days=14)
    recent_bundle = await load_period_bundle(repo, guild_id, recent_start, end)
    previous_bundle = await load_period_bundle(repo, guild_id, previous_start, recent_start)
    recent_member = next((m for m in recent_bundle.members if m.membership_id == membership_id), None)
    previous_member = next((m for m in previous_bundle.members if m.membership_id == membership_id), None)
    trend_lines: list[str] = []
    if recent_member and previous_member:
        current7 = member_stats_from_bundle(recent_bundle, recent_member, recent_start, end)
        previous7 = member_stats_from_bundle(previous_bundle, previous_member, previous_start, recent_start)
        trend_lines = compare_member_stats(current7, previous7)

    embed = discord.Embed(
        title=f"👤 {stat.nickname} | {stat.static_id}",
        description=(
            f"Период: последние **{settings.member_eval_days} дней**\n"
            + (f"Оценка руководства: **{stat.rating_label}**" if staff_view else "Личная статистика: только факты участия, без баллов и публичного рейтинга.")
        ),
    )
    group_row = await repo.get_open_group_period(membership_id)
    if group_row:
        group_label = {
            "academy": "🎓 Academy", "main": "🏠 Основной состав",
            "unclassified": "⚪ Без группы", "conflict": "⚠️ Конфликт ролей",
        }.get(str(group_row["group_name"]), str(group_row["group_name"]))
        embed.add_field(name="Группа", value=group_label, inline=True)

    # Facts first. A user who attended a spontaneous event must immediately see 1,
    # not a confusing 0/0 denominator.
    embed.add_field(
        name="🎮 Фактическое участие",
        value=(
            f"Всего посещено: **{stat.total_attended_events}**\n"
            f"Плановых посещено: **{stat.planned_attended_events}**\n"
            f"Спонтанных посещено: **{stat.spontaneous_attended}**\n"
            f"Дней с участием: **{stat.active_days}**\n"
            f"Недель с участием: **{stat.active_weeks}**"
        ),
        inline=True,
    )
    fair_rate = pct(stat.attendance_rate)
    embed.add_field(
        name="📅 Честная посещаемость",
        value=(
            f"Доступных возможностей: **{stat.opportunities}**\n"
            f"Использовано: **{stat.attended_opportunities}**\n"
            f"Пропущено: **{stat.missed_opportunities}**\n"
            f"Посещаемость: **{fair_rate}**\n"
            f"Дней с возможностями: **{stat.opportunity_days}**\n"
            f"Недель с возможностями: **{stat.attended_opportunity_weeks}/{stat.opportunity_weeks}**"
        ),
        inline=True,
    )
    if stat.opportunities == 0:
        embed.add_field(
            name="ℹ️ Почему нет процента посещаемости",
            value=(
                "Процент считается только по **запланированным аналитическим активностям**, которые подходили человеку по времени, аудитории, периоду членства и отпуску. "
                "Спонтанные активности всегда сохраняются как посещение, но никогда не создают пропуск."
            ),
            inline=False,
        )

    last = dt_to_discord_timestamp(stat.last_activity_at) if stat.last_activity_at else "Нет подтверждённых посещений"
    embed.add_field(name="Последняя активность", value=last, inline=False)

    category_lines = []
    for key in ("training", "family", "faction"):
        cs = stat.category_stats.get(key)
        if not cs:
            continue
        category_lines.append(
            f"{CATEGORY_LABELS[key]} — посещено **{cs.attended_total}** · из честно доступных плановых **{cs.attended_eligible_events}/{cs.eligible_events}**"
        )
    embed.add_field(name="По категориям", value="\n".join(category_lines) or "Нет данных", inline=False)

    if trend_lines:
        embed.add_field(name="📈 Последние 7 дней vs предыдущие 7", value="\n".join(trend_lines), inline=False)
    if staff_view:
        reason_labels = {
            "not_member_at_time": "не был в составе",
            "newcomer": "период новичка",
            "vacation": "отпуск",
            "audience": "не входил в аудиторию",
            "availability_missing": "не настроено время",
            "time_mismatch": "не подходило по времени",
        }
        excluded_lines = [
            f"• {reason_labels.get(key, key)}: **{value}**"
            for key, value in sorted(stat.eligibility_exclusions.items())
            if value
        ]
        audit_text = (
            f"Плановых аналитических событий, прошедших фильтр: **{stat.eligible_planned_events}**\n"
            f"После объединения пересечений: **{stat.opportunities} возможностей**\n"
            f"Склеено пересекающихся событий: **{stat.overlap_merged_events}**"
        )
        if excluded_lines:
            audit_text += "\nИсключено из знаменателя:\n" + "\n".join(excluded_lines)
        embed.add_field(name="🧾 Аудит знаменателя", value=audit_text, inline=False)
        embed.add_field(name="Почему такая оценка", value="\n".join(f"• {r}" for r in stat.reasons), inline=False)
    if stat.extra_attended:
        embed.add_field(
            name="✨ Дополнительное участие",
            value=f"Посещено **{stat.extra_attended}** плановых активностей вне обычного честного знаменателя — например вне указанного времени. Это учитывается как факт участия, но не создаёт обязанность приходить туда.",
            inline=False,
        )
    if not stat.availability_configured:
        embed.add_field(
            name="⚠️ Временной профиль",
            value="Обычное время участия не настроено. Фактические посещения считаются, но процент честной посещаемости и персональная оценка недоступны.",
            inline=False,
        )
    return embed


async def build_roster_embed(repo: Repository, guild_id: int) -> discord.Embed:
    members = await repo.list_active_members(guild_id)
    settings = await repo.ensure_guild_settings(guild_id)
    group_rows = await repo.current_group_rows(guild_id) if settings.academy_role_id and settings.main_role_id else []
    group_map = {int(r["membership_id"]): str(r["group_name"]) for r in group_rows}

    if not settings.academy_role_id or not settings.main_role_id:
        lines = []
        for row in members[:30]:
            mention = f"<@{row['discord_user_id']}>" if row["discord_user_id"] else row["nickname"]
            lines.append(f"`R{row['rank']}` **{row['nickname']}** · `{row['static_id']}` · {mention}")
        if len(members) > 30:
            lines.append(f"…и ещё **{len(members) - 30}**. Для поиска используй `/состав профиль`.")
        return discord.Embed(
            title=f"👥 Состав семьи · {len(members)}",
            description="\n".join(lines) if lines else "Состав пока пуст.",
        )

    buckets = {"main": [], "academy": [], "unclassified": [], "conflict": []}
    for row in members:
        buckets.setdefault(group_map.get(int(row["id"]), "unclassified"), []).append(row)

    embed = discord.Embed(
        title=f"👥 Состав организации · {len(members)}",
        description=(
            f"🏠 Основной состав: **{len(buckets['main'])}** · "
            f"🎓 Academy: **{len(buckets['academy'])}** · "
            f"⚪ Без группы: **{len(buckets['unclassified'])}** · "
            f"⚠️ Конфликты: **{len(buckets['conflict'])}**"
        ),
    )
    labels = [
        ("main", "🏠 Основной состав"),
        ("academy", "🎓 Academy"),
        ("conflict", "⚠️ Конфликт ролей"),
        ("unclassified", "⚪ Без группы"),
    ]
    for key, label in labels:
        rows = buckets[key]
        if not rows:
            continue
        lines = []
        for row in rows[:15]:
            mention = f"<@{row['discord_user_id']}>" if row["discord_user_id"] else row["nickname"]
            lines.append(f"`R{row['rank']}` **{row['nickname']}** `{row['static_id']}` · {mention}")
        if len(rows) > 15:
            lines.append(f"…и ещё **{len(rows)-15}**")
        embed.add_field(name=f"{label} · {len(rows)}", value="\n".join(lines), inline=False)
    embed.set_footer(text="Academy/Main синхронизируются по Discord-ролям. История переходов сохраняется по времени.")
    return embed


async def build_attention_embed(repo: Repository, guild_id: int) -> discord.Embed:
    settings = await repo.ensure_guild_settings(guild_id)
    end = utcnow()
    start = end - timedelta(days=settings.member_eval_days)
    bundle = await load_period_bundle(repo, guild_id, start, end)
    group_rows = await repo.current_group_rows(guild_id) if settings.academy_role_id and settings.main_role_id else []
    group_map = {int(r["membership_id"]): str(r["group_name"]) for r in group_rows}
    items: list[tuple[int, str]] = []
    if settings.academy_role_id and settings.main_role_id:
        for member in bundle.members:
            state = group_map.get(member.membership_id, "unclassified")
            if state == "conflict":
                items.append((-2, f"⚠️ **{member.nickname}** `{member.static_id}` — одновременно Academy и Mein Rank."))
            elif state == "unclassified":
                items.append((-1, f"⚪ **{member.nickname}** `{member.static_id}` — нет ни Academy, ни Mein Rank."))
    for member in bundle.members:
        row = await repo.get_membership(member.membership_id)
        if not row or row["status"] != "active":
            continue
        if settings.academy_role_id and settings.main_role_id:
            state = group_map.get(member.membership_id, "unclassified")
            if state != "main":
                continue
        stat = member_stats_from_bundle(bundle, member, start, end)
        if stat.rating_key == "low":
            items.append((0, f"🔴 **{member.nickname}** `{member.static_id}` — почти не участвует: {stat.attended_opportunities}/{stat.opportunities} честных возможностей, всего посещений {stat.total_attended_events}"))
        elif stat.rating_key == "irregular":
            items.append((1, f"🟠 **{member.nickname}** `{member.static_id}` — нерегулярно: {stat.attended_opportunities}/{stat.opportunities}, всего посещений {stat.total_attended_events}"))
        elif not stat.availability_configured:
            items.append((2, f"⚪ **{member.nickname}** `{member.static_id}` — не настроено обычное время; фактических посещений {stat.total_attended_events}"))
        elif stat.rating_key == "insufficient":
            items.append((3, f"⚠️ **{member.nickname}** `{member.static_id}` — мало честных возможностей для оценки ({stat.opportunities}), посещений {stat.total_attended_events}"))
    items.sort(key=lambda x: (x[0], x[1]))
    lines = [text for _, text in items[:25]]
    if len(items) > 25:
        lines.append(f"…и ещё **{len(items) - 25}**")
    embed = discord.Embed(
        title="👀 Требуют внимания",
        description="\n".join(lines) if lines else "Сейчас нет явных проблем, требующих внимания.",
    )
    embed.set_footer(text="Список объясняет причину цифрами. Это аналитика для руководства, а не автоматическое наказание.")
    return embed


async def build_academy_overview_embed(repo: Repository, guild_id: int) -> discord.Embed:
    settings = await repo.ensure_guild_settings(guild_id)
    if not settings.academy_role_id or not settings.main_role_id:
        return discord.Embed(
            title="🎓 Academy",
            description="Роли Academy/Main ещё не настроены через `/семья настройка`.",
        )
    end = utcnow()
    start = end - timedelta(days=settings.member_eval_days)
    overview = await group_overview(repo, guild_id, start, end, "academy")
    group_rows = await repo.current_group_rows(guild_id)
    academy_rows = [r for r in group_rows if str(r["group_name"]) == "academy"]
    candidates = set(overview.candidate_membership_ids)

    embed = discord.Embed(
        title="🎓 Academy · обзор",
        description=(
            f"Период аналитики: **{settings.member_eval_days} дней**\n"
            "Это отдельная статистика обучения. Academy не занижает и не завышает Family Pulse основного состава."
        ),
    )
    embed.add_field(
        name="Состояние",
        value=(
            f"В Academy сейчас: **{overview.members}**\n"
            f"Участвовали за период: **{overview.unique_attendees}**\n"
            f"Охват: **{pct(overview.coverage)}**\n"
            f"Всего посещений: **{overview.total_attendances}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="Оценки руководства",
        value=(
            f"🔥 Очень высокая: **{overview.rating_counts.get('very_high', 0)}**\n"
            f"🟢 Высокая: **{overview.rating_counts.get('high', 0)}**\n"
            f"🟡 Стабильная: **{overview.rating_counts.get('stable', 0)}**\n"
            f"🟠/🔴 Требуют внимания: **{overview.rating_counts.get('irregular', 0) + overview.rating_counts.get('low', 0)}**\n"
            f"⚪ Мало данных/новички: **{overview.rating_counts.get('insufficient', 0) + overview.rating_counts.get('newcomer', 0)}**"
        ),
        inline=True,
    )
    candidate_lines = []
    for row in academy_rows:
        if int(row["membership_id"]) not in candidates:
            continue
        mention = f"<@{row['discord_user_id']}>" if row["discord_user_id"] else str(row["nickname"])
        candidate_lines.append(f"• **{row['nickname']}** `{row['static_id']}` · {mention}")
    embed.add_field(
        name="✅ Кандидаты на рассмотрение перевода",
        value="\n".join(candidate_lines[:20]) if candidate_lines else "Пока нет кандидатов по текущим данным.",
        inline=False,
    )
    embed.set_footer(text="Кандидат — только подсказка руководству. Бот никогда не переводит Academy в основной состав автоматически.")
    return embed


# Compatibility alias for old imports.
build_dashboard_embed = build_staff_dashboard_embed
