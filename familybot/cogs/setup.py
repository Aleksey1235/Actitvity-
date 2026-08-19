from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..helpers import require_leader
from ..repository import Repository
from ..timeutil import iso, utcnow, validate_timezone


def _channel_visible_to_family(channel: discord.TextChannel, family_role: discord.Role) -> bool:
    guild = channel.guild
    return (
        channel.permissions_for(guild.default_role).view_channel
        or channel.permissions_for(family_role).view_channel
    )


class SetupCog(commands.Cog):
    family = app_commands.Group(name="семья", description="Настройки Family Activity")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.repo: Repository = getattr(bot, "repo")

    @family.command(name="настройка", description="Первичная настройка ролей, закрытых каналов и часового пояса")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        family_role="Роль всех участников семьи",
        staff_role="Роль старшего состава",
        leader_role="Роль лидера/главных администраторов бота",
        vacation_role="Роль отпуска, которую выдаёт ваш внешний бот отпусков",
        log_channel="Закрытый канал журнала действий",
        report_channel="Закрытый канал недельных отчётов",
        activity_report_channel="Закрытый канал отчётов по каждой завершённой активности",
        academy_role="Роль Academy. Укажи вместе с ролью основного состава",
        main_role="Роль Mein Rank / основного состава. Укажи вместе с Academy",
        timezone="IANA timezone, например Europe/Moscow",
    )
    @app_commands.rename(
        family_role="роль_семьи",
        staff_role="роль_руководства",
        leader_role="роль_лидера",
        vacation_role="роль_отпуска",
        log_channel="канал_логов",
        report_channel="недельные_отчёты",
        activity_report_channel="отчёты_активностей",
        academy_role="роль_academy",
        main_role="роль_основного_состава",
        timezone="часовой_пояс",
    )
    async def setup_command(
        self,
        interaction: discord.Interaction,
        family_role: discord.Role,
        staff_role: discord.Role,
        leader_role: discord.Role,
        vacation_role: discord.Role,
        log_channel: discord.TextChannel,
        report_channel: discord.TextChannel,
        activity_report_channel: discord.TextChannel,
        academy_role: discord.Role | None = None,
        main_role: discord.Role | None = None,
        timezone: str = "Europe/Moscow",
    ) -> None:
        if not interaction.guild_id or not interaction.guild:
            await interaction.response.send_message("Только на сервере.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Нужны права администратора Discord.", ephemeral=True)
            return
        try:
            validate_timezone(timezone)
        except ValueError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return

        if family_role.id in {staff_role.id, leader_role.id, vacation_role.id}:
            await interaction.response.send_message(
                "❌ Роль семьи, руководства, лидера и отпуска должны быть разными ролями.",
                ephemeral=True,
            )
            return
        if (academy_role is None) != (main_role is None):
            await interaction.response.send_message(
                "❌ Academy и Mein Rank настраиваются только вместе: укажи обе роли или не указывай ни одну.",
                ephemeral=True,
            )
            return
        if academy_role and main_role:
            role_ids = {
                family_role.id, staff_role.id, leader_role.id, vacation_role.id,
                academy_role.id, main_role.id,
            }
            if len(role_ids) != 6:
                await interaction.response.send_message(
                    "❌ Family, Staff, Leader, отпуск, Academy и Mein Rank должны быть разными Discord-ролями.",
                    ephemeral=True,
                )
                return
        if _channel_visible_to_family(log_channel, family_role):
            await interaction.response.send_message(
                f"❌ {log_channel.mention} виден обычной роли семьи. Канал логов должен быть закрытым.",
                ephemeral=True,
            )
            return
        if _channel_visible_to_family(report_channel, family_role):
            await interaction.response.send_message(
                f"❌ {report_channel.mention} виден обычной роли семьи. Недельные отчёты содержат внутреннюю аналитику и должны быть закрытыми.",
                ephemeral=True,
            )
            return
        if _channel_visible_to_family(activity_report_channel, family_role):
            await interaction.response.send_message(
                f"❌ {activity_report_channel.mention} виден обычной роли семьи. Отчёты активностей содержат внутренний состав и должны быть закрытыми.",
                ephemeral=True,
            )
            return

        current = await self.repo.ensure_guild_settings(interaction.guild_id)
        cutover_dt = utcnow()
        cutover_at = current.vacation_role_cutover_at
        role_changed = current.vacation_role_id is not None and current.vacation_role_id != vacation_role.id
        if role_changed:
            # End old-role vacations exactly at policy cutover before synchronizing
            # the replacement role. This keeps historical analytics unambiguous.
            await self.repo.close_all_open_role_vacations_for_guild(
                interaction.guild_id,
                ends_at=cutover_dt,
                source="manual_sync",
                actor_user_id=interaction.user.id,
            )
        if current.vacation_role_id != vacation_role.id or not cutover_at:
            cutover_at = iso(cutover_dt)

        academy_role_id = academy_role.id if academy_role else current.academy_role_id
        main_role_id = main_role.id if main_role else current.main_role_id
        group_cutover_at = current.group_role_cutover_at
        group_roles_changed = (
            academy_role is not None and main_role is not None
            and (current.academy_role_id != academy_role.id or current.main_role_id != main_role.id)
        )
        if group_roles_changed:
            await self.repo.close_all_open_group_periods_for_guild(
                interaction.guild_id, ends_at=cutover_dt, actor_user_id=interaction.user.id
            )
            group_cutover_at = iso(cutover_dt)
        elif academy_role_id and main_role_id and not group_cutover_at:
            group_cutover_at = iso(cutover_dt)

        await self.repo.update_guild_settings(
            interaction.guild_id,
            family_role_id=family_role.id,
            staff_role_id=staff_role.id,
            leader_role_id=leader_role.id,
            vacation_role_id=vacation_role.id,
            vacation_role_cutover_at=cutover_at,
            academy_role_id=academy_role_id,
            main_role_id=main_role_id,
            group_role_cutover_at=group_cutover_at,
            log_channel_id=log_channel.id,
            report_channel_id=report_channel.id,
            activity_report_channel_id=activity_report_channel.id,
            timezone=timezone,
        )
        await self.repo.audit(
            interaction.guild_id,
            interaction.user.id,
            "guild.configured",
            "guild",
            interaction.guild_id,
            {
                "family_role_id": family_role.id,
                "staff_role_id": staff_role.id,
                "leader_role_id": leader_role.id,
                "vacation_role_id": vacation_role.id,
                "academy_role_id": academy_role_id,
                "main_role_id": main_role_id,
                "group_role_cutover_at": group_cutover_at,
                "log_channel_id": log_channel.id,
                "report_channel_id": report_channel.id,
                "activity_report_channel_id": activity_report_channel.id,
                "vacation_role_cutover_at": cutover_at,
                "timezone": timezone,
            },
        )

        # Immediately mirror the existing external vacation-role state.
        vacation_sync = await self.bot.sync_vacation_roles_for_guild(  # type: ignore[attr-defined]
            interaction.guild,
            source="manual_sync",
        )
        group_sync = await self.bot.sync_group_roles_for_guild(  # type: ignore[attr-defined]
            interaction.guild,
            source="setup_sync",
        )
        await interaction.response.send_message(
            "✅ **Family Activity настроен.**\n"
            f"Состав: {family_role.mention}\n"
            f"Старший состав: {staff_role.mention}\n"
            f"Лидер: {leader_role.mention}\n"
            f"Внешняя роль отпуска: {vacation_role.mention}\n"
            + (f"Academy: <@&{academy_role_id}>\nОсновной состав: <@&{main_role_id}>\n" if academy_role_id and main_role_id else "Academy/Main: не настроены\n")
            + f"Логи: {log_channel.mention}\n"
            f"Недельные отчёты: {report_channel.mention}\n"
            f"Отчёты активностей: {activity_report_channel.mention}\n"
            f"Часовой пояс: `{timezone}`\n"
            f"Источник отпусков: роль {vacation_role.mention} с момента `{cutover_at}`\n\n"
            f"Синхронизация отпусков: открыто **{vacation_sync.opened}**, закрыто **{vacation_sync.closed}**.\n"
            f"На роли отпуска в Discord: **{vacation_sync.discord_role_holders}**; "
            f"привязано к активному составу: **{vacation_sync.linked_role_holders}**; "
            f"с ролью, но вне базы состава: **{vacation_sync.unlinked_role_holders}**.\n"
            f"Участников базы без доступного Discord-профиля: **{vacation_sync.missing_discord_profiles}**.\n\n"
            + (
                f"Синхронизация групп: основной состав **{group_sync.main_members}**, Academy **{group_sync.academy_members}**, "
                f"без группы **{group_sync.unclassified_members}**, конфликт ролей **{group_sync.conflict_members}**.\n\n"
                if academy_role_id and main_role_id else ""
            )
            + "Дальше: добавь/импортируй состав, создай публичную `/панель участники`, "
            "а в закрытом staff-канале — `/панель руководство`.",
            ephemeral=True,
        )

    @family.command(name="синхронизация_групп", description="Сверить Academy/Mein Rank с базой состава")
    async def sync_groups(self, interaction: discord.Interaction) -> None:
        settings = await require_leader(self.repo, interaction)
        if settings is None:
            return
        if not interaction.guild:
            return
        if not settings.academy_role_id or not settings.main_role_id:
            await interaction.response.send_message(
                "❌ Сначала настрой роли Academy и основного состава через `/семья настройка`.",
                ephemeral=True,
            )
            return
        result = await self.bot.sync_group_roles_for_guild(  # type: ignore[attr-defined]
            interaction.guild, source="manual_sync"
        )
        unlinked_a = ", ".join(f"<@{uid}>" for uid in result.unlinked_academy_ids[:15]) or "—"
        unlinked_m = ", ".join(f"<@{uid}>" for uid in result.unlinked_main_ids[:15]) or "—"
        await interaction.response.send_message(
            "✅ **Синхронизация Academy/Main завершена.**\n"
            f"🏠 Основной состав: **{result.main_members}**\n"
            f"🎓 Academy: **{result.academy_members}**\n"
            f"⚪ Без группы: **{result.unclassified_members}**\n"
            f"⚠️ Конфликт Academy + Mein Rank: **{result.conflict_members}**\n"
            f"Изменений записано: **{result.changed}**\n"
            f"Нет Discord-профиля: **{result.missing_discord_profiles}**\n\n"
            f"Academy-роль вне базы: {unlinked_a}\n"
            f"Mein Rank вне базы: {unlinked_m}",
            ephemeral=True,
        )

    @family.command(name="правила", description="Настроить правила аналитики")
    @app_commands.describe(
        notice_minutes="Сколько минут заранее нужно объявить активность, чтобы отсутствие могло учитываться",
        newcomer_days="Сколько дней новичок собирает данные без оценки",
        member_eval_days="Период персональной оценки",
        min_member_opportunities="Минимум подходящих активностей для персональной оценки",
        weekly_min_opportunities="Минимум подходящих активностей в неделю для покрытия расписанием",
    )
    @app_commands.rename(
        notice_minutes="предупреждение_мин",
        newcomer_days="дней_новичка",
        member_eval_days="период_оценки",
        min_member_opportunities="минимум_возможностей",
        weekly_min_opportunities="минимум_за_неделю",
    )
    async def rules(
        self,
        interaction: discord.Interaction,
        notice_minutes: app_commands.Range[int, 0, 1440],
        newcomer_days: app_commands.Range[int, 0, 30],
        member_eval_days: app_commands.Range[int, 7, 90],
        min_member_opportunities: app_commands.Range[int, 1, 50],
        weekly_min_opportunities: app_commands.Range[int, 1, 14],
    ) -> None:
        settings = await require_leader(self.repo, interaction)
        if settings is None:
            return
        await self.repo.update_guild_settings(
            interaction.guild_id,
            notice_minutes=int(notice_minutes),
            newcomer_days=int(newcomer_days),
            member_eval_days=int(member_eval_days),
            min_member_opportunities=int(min_member_opportunities),
            weekly_min_opportunities=int(weekly_min_opportunities),
        )
        await self.repo.audit(
            interaction.guild_id,
            interaction.user.id,
            "guild.analytics_rules_changed",
            "guild",
            interaction.guild_id,
            {
                "notice_minutes": notice_minutes,
                "newcomer_days": newcomer_days,
                "member_eval_days": member_eval_days,
                "min_member_opportunities": min_member_opportunities,
                "weekly_min_opportunities": weekly_min_opportunities,
            },
        )
        await interaction.response.send_message("✅ Правила аналитики обновлены.", ephemeral=True)

    @family.command(name="синхронизация_отпусков", description="Принудительно сверить внешнюю роль отпуска с базой")
    async def sync_vacations(self, interaction: discord.Interaction) -> None:
        settings = await require_leader(self.repo, interaction)
        if settings is None or not interaction.guild:
            return
        if not settings.vacation_role_id:
            await interaction.response.send_message("❌ Роль отпуска ещё не настроена.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        result = await self.bot.sync_vacation_roles_for_guild(  # type: ignore[attr-defined]
            interaction.guild,
            source="manual_sync",
        )
        active = await self.repo.list_open_role_vacations(interaction.guild_id)

        unlinked_lines = []
        for user_id in result.unlinked_role_member_ids[:15]:
            member = interaction.guild.get_member(user_id)
            unlinked_lines.append(member.mention if member else f"<@{user_id}>")
        unlinked_text = ""
        if unlinked_lines:
            unlinked_text = (
                "\n\n⚠️ **Роль отпуска есть, но человека нет в активной базе Family Activity:**\n"
                + ", ".join(unlinked_lines)
            )
            if result.unlinked_role_holders > 15:
                unlinked_text += f" и ещё **{result.unlinked_role_holders - 15}**"
            unlinked_text += (
                "\nТакие люди не попадают в статистику отпусков, пока не будут добавлены/возвращены "
                "в состав бота. Это сделано специально, чтобы бот не создавал участников без Static ID."
            )

        await interaction.followup.send(
            "✅ **Синхронизация отпусков завершена.**\n"
            f"На роли отпуска в Discord: **{result.discord_role_holders}**\n"
            f"Из них есть в активной базе состава: **{result.linked_role_holders}**\n"
            f"С ролью, но вне активной базы: **{result.unlinked_role_holders}**\n"
            f"Новых периодов отпуска открыто: **{result.opened}**\n"
            f"Периодов закрыто: **{result.closed}**\n"
            f"Участников базы без доступного Discord-профиля: **{result.missing_discord_profiles}**\n"
            f"Активных отпусков в аналитической базе сейчас: **{len(active)}**"
            + unlinked_text,
            ephemeral=True,
        )

    @family.command(name="база", description="Проверить состояние базы данных и резервных копий")
    async def database_status(self, interaction: discord.Interaction) -> None:
        settings = await require_leader(self.repo, interaction)
        if settings is None:
            return
        status = await self.bot.database_status()  # type: ignore[attr-defined]
        size = int(status["size"])
        if size < 1024:
            size_text = f"{size} Б"
        elif size < 1024 * 1024:
            size_text = f"{size / 1024:.1f} КБ"
        else:
            size_text = f"{size / (1024 * 1024):.1f} МБ"
        latest = status["latest_backup"] or "ещё нет"
        await interaction.response.send_message(
            "### 💾 Хранилище Family Activity\n"
            f"Целостность SQLite: **{'✅ OK' if status['ok'] else '❌ ОШИБКА'}**\n"
            f"Размер базы: **{size_text}**\n"
            f"Файл базы: `{status['path']}`\n"
            f"Папка копий: `{status['backup_dir']}`\n"
            f"Резервных копий: **{status['backup_count']}**\n"
            f"Последняя копия: `{latest}`\n\n"
            "На хостинге каталог `data` должен находиться на постоянном диске/volume.",
            ephemeral=True,
        )

    @family.command(name="резервная_копия", description="Создать резервную копию базы данных прямо сейчас")
    async def database_backup(self, interaction: discord.Interaction) -> None:
        settings = await require_leader(self.repo, interaction)
        if settings is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            path = await self.bot.create_manual_database_backup()  # type: ignore[attr-defined]
            await interaction.followup.send(
                "✅ Резервная копия базы создана и проверена.\n"
                f"`{path}`",
                ephemeral=True,
            )
        except Exception:
            await interaction.followup.send(
                "❌ Не удалось создать резервную копию. Подробности записаны в лог бота.",
                ephemeral=True,
            )
            raise


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SetupCog(bot))
