from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from datetime import timedelta

import discord

from .helpers import activity_embed, get_settings, is_staff, require_staff
from .chart_service import build_chart
from .presenters import (
    build_academy_overview_embed,
    build_attention_embed,
    build_family_pulse_embed,
    build_member_stats_embed,
    build_my_activities_embed,
    build_my_profile_embed,
    build_roster_embed,
)
from .reporting import weekly_report_payload
from .repository import DomainError, Repository
from .timeutil import current_week_bounds, dt_to_discord_timestamp, parse_iso, utcnow


class AttemptLimiter:
    def __init__(self, max_attempts: int = 5, period_seconds: int = 120):
        self.max_attempts = max_attempts
        self.period_seconds = period_seconds
        self._data: dict[int, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def allow(self, user_id: int) -> bool:
        now = time.monotonic()
        async with self._lock:
            q = self._data[user_id]
            while q and q[0] < now - self.period_seconds:
                q.popleft()
            if len(q) >= self.max_attempts:
                return False
            q.append(now)
            return True


CHECKIN_LIMITER = AttemptLimiter()


def repo_from(interaction: discord.Interaction) -> Repository:
    return getattr(interaction.client, "repo")


class CheckinModal(discord.ui.Modal, title="Отметиться на активности"):
    code = discord.ui.TextInput(
        label="Код активности",
        placeholder="Например K7M4Q2",
        min_length=4,
        max_length=12,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message("Работает только на сервере.", ephemeral=True)
            return
        if not await CHECKIN_LIMITER.allow(interaction.user.id):
            await interaction.response.send_message(
                "Слишком много попыток. Подожди немного и проверь код у организатора.",
                ephemeral=True,
            )
            return
        repo = repo_from(interaction)
        try:
            activity_id, kind, created = await repo.checkin_with_code(
                guild_id=interaction.guild_id,
                discord_user_id=interaction.user.id,
                code=str(self.code),
            )
            activity = await repo.get_activity(activity_id)
            kind_label = {"primary": "основная", "late": "поздняя", "control": "контрольная"}[kind]
            text = (
                f"✅ Посещение **#{activity_id} · {activity['title']}** подтверждено.\n"
                f"Отметка: **{kind_label}**."
            )
            if not created and kind != "control":
                text += "\nТы уже был в списке участников; повторная отметка не увеличивает статистику."
            await interaction.response.send_message(text, ephemeral=True)
        except DomainError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)


class CloseActivityModal(discord.ui.Modal, title="Завершить активность"):
    evidence = discord.ui.TextInput(
        label="Ссылка на скрин (необязательно)",
        required=False,
        max_length=500,
    )
    note = discord.ui.TextInput(
        label="Комментарий (необязательно)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1000,
    )

    def __init__(self, activity_id: int):
        super().__init__()
        self.activity_id = activity_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        repo = repo_from(interaction)
        settings = await require_staff(repo, interaction)
        if settings is None:
            return
        activity = await repo.get_activity(self.activity_id)
        if not activity or int(activity["guild_id"]) != int(interaction.guild_id or 0):
            await interaction.response.send_message("❌ Активность не найдена на этом сервере.", ephemeral=True)
            return
        try:
            await repo.close_activity(
                activity_id=self.activity_id,
                actor_user_id=interaction.user.id,
                evidence_url=str(self.evidence).strip() or None,
                closing_note=str(self.note).strip() or None,
            )
            row = await repo.get_activity(self.activity_id)
            attendance = await repo.attendance_for_activity(self.activity_id)
            report_posted = False
            if interaction.guild and hasattr(interaction.client, "post_activity_report"):
                report_posted = await interaction.client.post_activity_report(  # type: ignore[attr-defined]
                    interaction.guild, self.activity_id
                )
            report_text = (
                " Отчёт опубликован в закрытом канале активностей."
                if report_posted
                else " Отчёт сохранён в базе; если канал отчётов не настроен/недоступен, его можно открыть через `/активность отчёт`."
            )
            await interaction.response.send_message(
                f"✅ Активность **#{self.activity_id}** закрыта. Участников: **{len(attendance)}**.{report_text}",
                ephemeral=True,
            )
            if row and row["panel_channel_id"] and row["panel_message_id"] and interaction.guild:
                channel = interaction.guild.get_channel(int(row["panel_channel_id"]))
                if isinstance(channel, discord.TextChannel):
                    try:
                        message = await channel.fetch_message(int(row["panel_message_id"]))
                        await message.edit(embed=activity_embed(row, len(attendance)), view=None)
                    except discord.HTTPException:
                        pass
        except DomainError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)


class ActivityControlView(discord.ui.View):
    def __init__(self, activity_id: int):
        super().__init__(timeout=None)
        self.activity_id = activity_id

        buttons = [
            discord.ui.Button(label="Старт", emoji="▶️", style=discord.ButtonStyle.success, custom_id=f"act:start:{activity_id}", row=0),
            discord.ui.Button(label="Новый код", emoji="🔐", style=discord.ButtonStyle.primary, custom_id=f"act:code:{activity_id}", row=0),
            discord.ui.Button(label="Поздний код", emoji="🕐", style=discord.ButtonStyle.secondary, custom_id=f"act:late:{activity_id}", row=0),
            discord.ui.Button(label="Контроль", emoji="✅", style=discord.ButtonStyle.secondary, custom_id=f"act:control:{activity_id}", row=0),
            discord.ui.Button(label="Завершить", emoji="🏁", style=discord.ButtonStyle.danger, custom_id=f"act:finish:{activity_id}", row=0),
            discord.ui.Button(label="Участники", emoji="👥", style=discord.ButtonStyle.secondary, custom_id=f"act:members:{activity_id}", row=1),
        ]
        callbacks = [
            self.start,
            self.primary_code,
            self.late_code,
            self.control_code,
            self.finish,
            self.members,
        ]
        for button, callback in zip(buttons, callbacks, strict=True):
            button.callback = callback
            self.add_item(button)

    async def _staff(self, interaction: discord.Interaction) -> Repository | None:
        repo = repo_from(interaction)
        settings = await get_settings(repo, interaction)
        if settings is None:
            return None
        if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user, settings):
            await interaction.response.send_message("Только старший состав может управлять активностью.", ephemeral=True)
            return None
        activity = await repo.get_activity(self.activity_id)
        if not activity or int(activity["guild_id"]) != int(interaction.guild_id or 0):
            await interaction.response.send_message("Активность не найдена на этом сервере.", ephemeral=True)
            return None
        return repo

    async def _show_code(self, interaction: discord.Interaction, kind: str) -> None:
        repo = await self._staff(interaction)
        if repo is None:
            return
        try:
            registration = await repo.open_registration(
                activity_id=self.activity_id,
                kind=kind,
                actor_user_id=interaction.user.id,
            )
            label = {"primary": "основной", "late": "поздний", "control": "контрольный"}[kind]
            await interaction.response.send_message(
                f"🔐 **{label.capitalize()} код:** `{registration.code}`\n"
                f"Действует до {dt_to_discord_timestamp(registration.expires_at, 'T')}.\n"
                "Передай код людям непосредственно на активности. Не публикуй его заранее в общем канале.",
                ephemeral=True,
            )
        except DomainError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)

    async def start(self, interaction: discord.Interaction) -> None:
        repo = await self._staff(interaction)
        if repo is None:
            return
        try:
            row = await repo.get_activity(self.activity_id)
            if not row:
                raise DomainError("Активность не найдена")
            if row["status"] == "scheduled":
                await repo.start_activity(self.activity_id, interaction.user.id)
            registration = await repo.open_registration(
                activity_id=self.activity_id,
                kind="primary",
                actor_user_id=interaction.user.id,
            )
            updated = await repo.get_activity(self.activity_id)
            attendance = await repo.attendance_for_activity(self.activity_id)
            await interaction.response.edit_message(
                embed=activity_embed(updated, len(attendance)),
                view=self,
            )
            await interaction.followup.send(
                f"▶️ Активность начата. Основной код: `{registration.code}`\n"
                f"Действует до {dt_to_discord_timestamp(registration.expires_at, 'T')}.",
                ephemeral=True,
            )
        except DomainError as exc:
            if interaction.response.is_done():
                await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ {exc}", ephemeral=True)

    async def primary_code(self, interaction: discord.Interaction) -> None:
        await self._show_code(interaction, "primary")

    async def late_code(self, interaction: discord.Interaction) -> None:
        await self._show_code(interaction, "late")

    async def control_code(self, interaction: discord.Interaction) -> None:
        await self._show_code(interaction, "control")

    async def finish(self, interaction: discord.Interaction) -> None:
        repo = await self._staff(interaction)
        if repo is None:
            return
        await interaction.response.send_modal(CloseActivityModal(self.activity_id))

    async def members(self, interaction: discord.Interaction) -> None:
        repo = await self._staff(interaction)
        if repo is None:
            return
        rows = await repo.attendance_for_activity(self.activity_id)
        lines = [
            (
                f"• {'🔒' if r['control_confirmed'] else '✅'} **{r['nickname']}** · "
                f"`{r['static_id']}` · <@{r['discord_user_id']}> · `{r['source']}`"
            )
            for r in rows[:35]
        ]
        if len(rows) > 35:
            lines.append(f"…и ещё **{len(rows) - 35}**")
        await interaction.response.send_message(
            f"### 👥 Участники #{self.activity_id} · {len(rows)}\n" + ("\n".join(lines) if lines else "Пока никто не отмечен."),
            ephemeral=True,
        )



class PublicDashboardView(discord.ui.View):
    """Safe member-facing panel. Every response is ephemeral and user-scoped."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _membership(self, interaction: discord.Interaction):
        repo = repo_from(interaction)
        if not interaction.guild_id:
            await interaction.response.send_message("Только на сервере.", ephemeral=True)
            return repo, None
        membership = await repo.get_active_membership_by_discord(interaction.guild_id, interaction.user.id)
        if not membership:
            await interaction.response.send_message("Тебя нет в активном составе семьи.", ephemeral=True)
            return repo, None
        return repo, membership

    @discord.ui.button(label="Отметиться", emoji="✅", style=discord.ButtonStyle.success, custom_id="public:checkin", row=0)
    async def checkin(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message("Только на сервере.", ephemeral=True)
            return
        repo = repo_from(interaction)
        membership = await repo.get_active_membership_by_discord(interaction.guild_id, interaction.user.id)
        if not membership:
            await interaction.response.send_message("Тебя нет в активном составе семьи.", ephemeral=True)
            return
        await interaction.response.send_modal(CheckinModal())

    @discord.ui.button(label="Моя статистика", emoji="📊", style=discord.ButtonStyle.primary, custom_id="public:my_stats", row=0)
    async def my_stats(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        repo, membership = await self._membership(interaction)
        if not membership:
            return
        await interaction.response.defer(ephemeral=True)
        embed = await build_member_stats_embed(repo, interaction.guild_id, int(membership["id"]), staff_view=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="Мои активности", emoji="🎮", style=discord.ButtonStyle.secondary, custom_id="public:my_activities", row=0)
    async def my_activities(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        repo, membership = await self._membership(interaction)
        if not membership:
            return
        embed = await build_my_activities_embed(repo, interaction.guild_id, int(membership["id"]))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Мой профиль", emoji="👤", style=discord.ButtonStyle.secondary, custom_id="public:my_profile", row=0)
    async def my_profile(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        repo, membership = await self._membership(interaction)
        if not membership:
            return
        embed = await build_my_profile_embed(repo, interaction.guild_id, int(membership["id"]))
        await interaction.response.send_message(embed=embed, ephemeral=True)


class StaffChartSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Неделя · эта vs прошлая", value="week", emoji="📈", description="Уникальные участники по дням"),
            discord.SelectOption(label="Family Pulse · история", value="pulse", emoji="💓", description="Недельная динамика Pulse"),
            discord.SelectOption(label="Категории контента", value="categories", emoji="🎮", description="Тренировки / семейный / фракционный"),
            discord.SelectOption(label="Основной состав vs Academy", value="groups", emoji="👥", description="Сравнение охвата групп"),
            discord.SelectOption(label="Расписание активностей", value="schedule", emoji="🕐", description="Тепловая карта дней и времени"),
        ]
        super().__init__(placeholder="Выбери график…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        repo = repo_from(interaction)
        settings = await require_staff(repo, interaction)
        if settings is None:
            return
        await interaction.response.defer(ephemeral=True)
        try:
            payload = await build_chart(repo, interaction.guild_id, self.values[0], days=28)
        except DomainError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        file = discord.File(payload.image, filename=payload.filename)
        embed = discord.Embed(title=payload.title, description=payload.description)
        embed.set_image(url=f"attachment://{payload.filename}")
        embed.set_footer(text="По кнопке используется период 28 дней там, где он нужен. Для другого периода: /статистика график")
        await interaction.followup.send(embed=embed, file=file, ephemeral=True)


class StaffChartSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(StaffChartSelect())


class StaffDashboardView(discord.ui.View):
    """Management panel. Every callback re-checks the staff role."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _staff_repo(self, interaction: discord.Interaction) -> Repository | None:
        repo = repo_from(interaction)
        settings = await require_staff(repo, interaction)
        if settings is None:
            return None
        return repo

    @discord.ui.button(label="Family Pulse", emoji="💓", style=discord.ButtonStyle.primary, custom_id="staff:pulse", row=0)
    async def pulse(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        repo = await self._staff_repo(interaction)
        if repo is None:
            return
        await interaction.response.defer(ephemeral=True)
        embed = await build_family_pulse_embed(repo, interaction.guild_id)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="Состав", emoji="👥", style=discord.ButtonStyle.secondary, custom_id="staff:roster", row=0)
    async def roster(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        repo = await self._staff_repo(interaction)
        if repo is None:
            return
        embed = await build_roster_embed(repo, interaction.guild_id)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Academy", emoji="🎓", style=discord.ButtonStyle.secondary, custom_id="staff:academy", row=0)
    async def academy(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        repo = await self._staff_repo(interaction)
        if repo is None:
            return
        await interaction.response.defer(ephemeral=True)
        embed = await build_academy_overview_embed(repo, interaction.guild_id)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="Требуют внимания", emoji="👀", style=discord.ButtonStyle.secondary, custom_id="staff:attention", row=0)
    async def attention(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        repo = await self._staff_repo(interaction)
        if repo is None:
            return
        await interaction.response.defer(ephemeral=True)
        embed = await build_attention_embed(repo, interaction.guild_id)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="Активности", emoji="🎮", style=discord.ButtonStyle.secondary, custom_id="staff:activities", row=1)
    async def activities(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        repo = await self._staff_repo(interaction)
        if repo is None:
            return
        rows = await repo.list_open_activities(interaction.guild_id)
        lines = [f"• `#{row['id']}` **{row['title']}** · `{row['status']}`" for row in rows[:15]]
        await interaction.response.send_message(
            "### 🎮 Открытые активности\n" + ("\n".join(lines) if lines else "Сейчас нет открытых активностей."),
            ephemeral=True,
        )

    @discord.ui.button(label="Отпуска", emoji="💤", style=discord.ButtonStyle.secondary, custom_id="staff:vacations", row=1)
    async def vacations(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        repo = await self._staff_repo(interaction)
        if repo is None or not interaction.guild:
            return
        await interaction.response.defer(ephemeral=True)
        bot = interaction.client
        sync = await bot.sync_vacation_roles_for_guild(  # type: ignore[attr-defined]
            interaction.guild, source="manual_sync"
        )
        rows = await repo.list_open_role_vacations(interaction.guild_id)
        lines = []
        for r in rows[:25]:
            if r["starts_at"]:
                started = dt_to_discord_timestamp(parse_iso(r["starts_at"]), "f")
            else:
                started = f"`{r['starts_on']}`"
            uncertain = " · ⚠️ время начала обнаружено сверкой" if r["sync_uncertain"] else ""
            lines.append(
                f"💤 **{r['nickname']}** · `{r['static_id']}` · с {started}{uncertain}"
            )
        if len(rows) > 25:
            lines.append(f"…и ещё **{len(rows) - 25}**")

        header = (
            "### 💤 Отпуска — живая сверка Discord ↔ база\n"
            f"На Discord-роли отпуска: **{sync.discord_role_holders}**\n"
            f"Привязано к активному составу: **{sync.linked_role_holders}**\n"
            f"С ролью, но вне базы состава: **{sync.unlinked_role_holders}**\n"
            f"Активных отпусков в аналитике: **{len(rows)}**\n\n"
        )
        if sync.unlinked_role_holders:
            missing_mentions = []
            for user_id in sync.unlinked_role_member_ids[:10]:
                member = interaction.guild.get_member(user_id)
                missing_mentions.append(member.mention if member else f"<@{user_id}>")
            header += (
                "⚠️ **Не привязаны к составу бота:** "
                + ", ".join(missing_mentions)
                + (f" и ещё **{sync.unlinked_role_holders - 10}**" if sync.unlinked_role_holders > 10 else "")
                + "\nЭти люди не учитываются в аналитике, пока их нет в `/состав список`.\n\n"
            )

        await interaction.followup.send(
            header + ("\n".join(lines) if lines else "Среди привязанных участников сейчас нет активных отпусков."),
            ephemeral=True,
        )

    @discord.ui.button(label="Графики", emoji="📊", style=discord.ButtonStyle.secondary, custom_id="staff:charts", row=1)
    async def charts(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        repo = await self._staff_repo(interaction)
        if repo is None:
            return
        await interaction.response.send_message(
            "### 📊 Графики руководства\nВыбери нужный график. Данные всегда пересчитываются из базы.",
            view=StaffChartSelectView(),
            ephemeral=True,
        )

    @discord.ui.button(label="Отчёт недели", emoji="📈", style=discord.ButtonStyle.secondary, custom_id="staff:week", row=1)
    async def week(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        repo = await self._staff_repo(interaction)
        if repo is None:
            return
        settings = await repo.ensure_guild_settings(interaction.guild_id)
        await interaction.response.defer(ephemeral=True)
        now = utcnow()
        start, week_end = current_week_bounds(now, settings.timezone)
        end = min(now, week_end)
        previous_start = start - timedelta(days=7)
        previous_end = previous_start + (end - start)
        embed, file, _, _ = await weekly_report_payload(
            repo, interaction.guild_id, start, end, previous_start, previous_end
        )
        await interaction.followup.send(embed=embed, file=file, ephemeral=True)
