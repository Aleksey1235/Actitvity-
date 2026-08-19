from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..helpers import activity_embed, require_staff
from ..reporting import activity_report_payload
from ..repository import DomainError, Repository
from ..timeutil import parse_local_datetime
from ..ui import ActivityControlView, CHECKIN_LIMITER

CATEGORY_CHOICES = [
    app_commands.Choice(name="Тренировка", value="training"),
    app_commands.Choice(name="Семейный контент", value="family"),
    app_commands.Choice(name="Фракционный контент", value="faction"),
]
MODE_CHOICES = [
    app_commands.Choice(name="Авто — бот решит по времени объявления", value="auto"),
    app_commands.Choice(name="Запланированная — только если объявлена заранее", value="planned"),
    app_commands.Choice(name="Спонтанная — отсутствие никому не считается", value="spontaneous"),
]
AUDIENCE_CHOICES = [
    app_commands.Choice(name="Вся семья", value="all"),
    app_commands.Choice(name="Только основной состав (Mein Rank)", value="main"),
    app_commands.Choice(name="Только Academy", value="academy"),
    app_commands.Choice(name="Диапазон рангов", value="rank_range"),
    app_commands.Choice(name="Выбранные участники", value="custom"),
]


class ActivitiesCog(commands.Cog):
    activity = app_commands.Group(name="активность", description="Семейные активности")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.repo: Repository = getattr(bot, "repo")


    @app_commands.command(name="отметиться", description="Отметиться на текущей активности по коду")
    @app_commands.rename(code='код')
    async def checkin(self, interaction: discord.Interaction, code: str) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message("Только на сервере.", ephemeral=True)
            return
        if not await CHECKIN_LIMITER.allow(interaction.user.id):
            await interaction.response.send_message(
                "Слишком много попыток. Подожди немного и проверь код у организатора.",
                ephemeral=True,
            )
            return
        try:
            activity_id, kind, created = await self.repo.checkin_with_code(
                guild_id=interaction.guild_id,
                discord_user_id=interaction.user.id,
                code=code,
            )
            activity = await self.repo.get_activity(activity_id)
            kind_label = {"primary": "основная", "late": "поздняя", "control": "контрольная"}[kind]
            suffix = "" if created or kind == "control" else "\nПовторная отметка не увеличивает статистику."
            await interaction.response.send_message(
                f"✅ **{activity['title']}** · отметка: **{kind_label}**.{suffix}",
                ephemeral=True,
            )
        except DomainError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)

    @activity.command(name="создать", description="Создать активность")
    @app_commands.choices(category=CATEGORY_CHOICES, mode=MODE_CHOICES, audience=AUDIENCE_CHOICES)
    @app_commands.describe(
        when="Локальное время семьи: YYYY-MM-DD HH:MM",
        mode="Авто/запланированная/спонтанная. Короткое объявление нельзя выдать за запланированное.",
        analytical="Учитывать в статистике и Family Pulse",
        first_member="Первый участник для custom-аудитории; остальных можно добавить отдельно",
    )
    @app_commands.rename(category='категория', title='название', when='время', mode='режим', analytical='аналитическая', audience='аудитория', min_rank='мин_ранг', max_rank='макс_ранг', first_member='первый_участник', description='описание')
    async def create(
        self,
        interaction: discord.Interaction,
        category: app_commands.Choice[str],
        title: str,
        when: str,
        mode: app_commands.Choice[str] | None = None,
        analytical: bool = True,
        audience: app_commands.Choice[str] | None = None,
        min_rank: int | None = None,
        max_rank: int | None = None,
        first_member: discord.Member | None = None,
        description: str | None = None,
    ) -> None:
        settings = await require_staff(self.repo, interaction)
        if settings is None:
            return
        organizer = await self.repo.get_active_membership_by_discord(interaction.guild_id, interaction.user.id)
        if not organizer:
            await interaction.response.send_message(
                "❌ Организатор должен быть добавлен в состав семьи.", ephemeral=True
            )
            return
        audience_choice = audience.value if audience else "all"
        audience_group: str | None = None
        if audience_choice in {"academy", "main"}:
            if not settings.academy_role_id or not settings.main_role_id:
                await interaction.response.send_message(
                    "❌ Academy/Main ещё не настроены. Выполни `/семья настройка` и укажи обе роли.",
                    ephemeral=True,
                )
                return
            audience_group = audience_choice
            audience_value = "all"
        else:
            audience_value = audience_choice

        custom_ids: list[int] = []
        if audience_value == "custom":
            if first_member is None:
                await interaction.response.send_message(
                    "❌ Для выбранной аудитории укажи хотя бы `first_member`.", ephemeral=True
                )
                return
            member_row = await self.repo.get_active_membership_by_discord(interaction.guild_id, first_member.id)
            if not member_row:
                await interaction.response.send_message("❌ first_member не найден в составе.", ephemeral=True)
                return
            custom_ids.append(int(member_row["id"]))
        try:
            scheduled_for = parse_local_datetime(when, settings.timezone)
            activity_id = await self.repo.create_activity(
                guild_id=interaction.guild_id,
                category=category.value,
                title=title,
                description=description,
                analytical=analytical,
                audience_type=audience_value,
                audience_group=audience_group,
                min_rank=min_rank,
                max_rank=max_rank,
                scheduled_for=scheduled_for,
                organizer_membership_id=int(organizer["id"]),
                notice_threshold_minutes=settings.notice_minutes,
                actor_user_id=interaction.user.id,
                classification_mode=mode.value if mode else "auto",
                custom_membership_ids=custom_ids,
            )
            row = await self.repo.get_activity(activity_id)
            view = ActivityControlView(activity_id)
            await interaction.response.send_message(embed=activity_embed(row, 0), view=view)
            message = await interaction.original_response()
            await self.repo.set_activity_panel(activity_id, message.channel.id, message.id)
        except (ValueError, DomainError) as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)

    @activity.command(name="аудитория_добавить", description="Добавить участника в выбранную аудиторию до старта")
    @app_commands.rename(activity_id='активность', member='участник')
    async def audience_add(
        self,
        interaction: discord.Interaction,
        activity_id: int,
        member: discord.Member,
    ) -> None:
        settings = await require_staff(self.repo, interaction)
        if settings is None:
            return
        membership = await self.repo.get_active_membership_by_discord(interaction.guild_id, member.id)
        if not membership:
            await interaction.response.send_message("❌ Участника нет в составе.", ephemeral=True)
            return
        try:
            await self.repo.add_custom_audience_member(activity_id, int(membership["id"]), interaction.user.id)
            await interaction.response.send_message(
                f"✅ **{membership['nickname']}** добавлен в аудиторию активности `#{activity_id}`.",
                ephemeral=True,
            )
        except DomainError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)

    @activity.command(name="добавить", description="Вручную подтвердить посещение с обязательной причиной")
    @app_commands.rename(activity_id='активность', member='участник', reason='причина')
    async def manual_add(
        self,
        interaction: discord.Interaction,
        activity_id: int,
        member: discord.Member,
        reason: str,
    ) -> None:
        settings = await require_staff(self.repo, interaction)
        if settings is None:
            return
        membership = await self.repo.get_active_membership_by_discord(interaction.guild_id, member.id)
        if not membership:
            await interaction.response.send_message("❌ Участника нет в активном составе.", ephemeral=True)
            return
        try:
            await self.repo.manual_add_attendance(
                activity_id=activity_id,
                membership_id=int(membership["id"]),
                actor_user_id=interaction.user.id,
                reason=reason,
            )
            await interaction.response.send_message(
                f"✅ **{membership['nickname']}** добавлен в `#{activity_id}` вручную. Действие записано в журнал.",
                ephemeral=True,
            )
        except DomainError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)

    @activity.command(name="убрать", description="Убрать ошибочную отметку с обязательной причиной")
    @app_commands.rename(activity_id='активность', member='участник', reason='причина')
    async def manual_remove(
        self,
        interaction: discord.Interaction,
        activity_id: int,
        member: discord.Member,
        reason: str,
    ) -> None:
        settings = await require_staff(self.repo, interaction)
        if settings is None:
            return
        membership = await self.repo.get_active_membership_by_discord(interaction.guild_id, member.id)
        if not membership:
            await interaction.response.send_message("❌ Участника нет в активном составе.", ephemeral=True)
            return
        try:
            await self.repo.remove_attendance(
                activity_id=activity_id,
                membership_id=int(membership["id"]),
                actor_user_id=interaction.user.id,
                reason=reason,
            )
            await interaction.response.send_message(
                f"✅ Отметка **{membership['nickname']}** удалена из `#{activity_id}`. Причина сохранена.",
                ephemeral=True,
            )
        except DomainError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)

    @activity.command(name="доказательство", description="Прикрепить фото/файл к отчёту активности")
    @app_commands.rename(activity_id="активность", attachment="файл")
    async def evidence(
        self,
        interaction: discord.Interaction,
        activity_id: int,
        attachment: discord.Attachment,
    ) -> None:
        settings = await require_staff(self.repo, interaction)
        if settings is None:
            return
        row = await self.repo.get_activity(activity_id)
        if not row or int(row["guild_id"]) != int(interaction.guild_id or 0):
            await interaction.response.send_message("❌ Активность не найдена на этом сервере.", ephemeral=True)
            return
        try:
            evidence_id = await self.repo.add_activity_evidence(
                activity_id=activity_id,
                url=attachment.url,
                filename=attachment.filename,
                content_type=attachment.content_type,
                actor_user_id=interaction.user.id,
            )
            refreshed = False
            if row["status"] == "closed" and interaction.guild:
                proof_file = await attachment.to_file()
                refreshed = await self.bot.mirror_activity_evidence(  # type: ignore[attr-defined]
                    interaction.guild, activity_id, evidence_id, proof_file
                )
            await interaction.response.send_message(
                f"✅ `{attachment.filename}` добавлен к активности `#{activity_id}`."
                + (" Отчёт в закрытом канале обновлён." if refreshed else ""),
                ephemeral=True,
            )
        except DomainError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)

    @activity.command(name="отчёт", description="Посмотреть полный отчёт завершённой активности")
    @app_commands.rename(activity_id="активность")
    async def report(self, interaction: discord.Interaction, activity_id: int) -> None:
        settings = await require_staff(self.repo, interaction)
        if settings is None:
            return
        await interaction.response.defer(ephemeral=True)
        try:
            embed, file = await activity_report_payload(self.repo, interaction.guild_id, activity_id)
            await interaction.followup.send(embed=embed, file=file, ephemeral=True)
        except DomainError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)

    @activity.command(name="архив", description="Последние завершённые активности и их отчёты")
    async def archive(self, interaction: discord.Interaction) -> None:
        settings = await require_staff(self.repo, interaction)
        if settings is None:
            return
        rows = await self.repo.list_recent_closed_activities(interaction.guild_id, limit=20)
        lines = []
        for row in rows:
            if row["report_message_id"] and row["report_channel_id"]:
                report_state = (
                    f"[📨 отчёт](https://discord.com/channels/{interaction.guild_id}/"
                    f"{row['report_channel_id']}/{row['report_message_id']})"
                )
            else:
                report_state = "⚠️ не опубликован"
            lines.append(
                f"• `#{row['id']}` **{row['title']}** · {report_state} · <t:{int(__import__('datetime').datetime.fromisoformat(row['ended_at']).timestamp())}:R>"
            )
        await interaction.response.send_message(
            "### 🗂 Архив активностей\n" + ("\n".join(lines) if lines else "Архив пока пуст.")
            + "\n\nДля полного просмотра: `/активность отчёт`.",
            ephemeral=True,
        )

    @activity.command(name="отменить", description="Отменить незакрытую активность")
    @app_commands.rename(activity_id='активность', reason='причина')
    async def cancel(self, interaction: discord.Interaction, activity_id: int, reason: str) -> None:
        settings = await require_staff(self.repo, interaction)
        if settings is None:
            return
        try:
            row = await self.repo.get_activity(activity_id)
            if not row or int(row["guild_id"]) != int(interaction.guild_id or 0):
                raise DomainError("Activity not found on this server")
            await self.repo.cancel_activity(activity_id, interaction.user.id, reason)
            row = await self.repo.get_activity(activity_id)
            await interaction.response.send_message(f"✅ Активность `#{activity_id}` отменена.", ephemeral=True)
            if row and row["panel_channel_id"] and row["panel_message_id"] and interaction.guild:
                channel = interaction.guild.get_channel(int(row["panel_channel_id"]))
                if isinstance(channel, discord.TextChannel):
                    try:
                        msg = await channel.fetch_message(int(row["panel_message_id"]))
                        await msg.edit(embed=activity_embed(row, 0), view=None)
                    except discord.HTTPException:
                        pass
        except DomainError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)

    @activity.command(name="текущие", description="Показать текущие и ближайшие активности")
    async def active(self, interaction: discord.Interaction) -> None:
        settings = await require_staff(self.repo, interaction)
        if settings is None:
            return
        rows = await self.repo.list_open_activities(interaction.guild_id)
        lines = []
        for row in rows[:20]:
            lines.append(
                f"• `#{row['id']}` **{row['title']}** · {row['status']} · <t:{int(__import__('datetime').datetime.fromisoformat(row['scheduled_for']).timestamp())}:R>"
            )
        await interaction.response.send_message(
            "### 🎮 Открытые активности\n" + ("\n".join(lines) if lines else "Нет открытых активностей."),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ActivitiesCog(bot))
