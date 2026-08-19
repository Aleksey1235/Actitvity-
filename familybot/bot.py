from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .config import Config
from .db import Database
from .helpers import is_confidential_channel, member_has_role, send_log
from .group_sync import GroupSyncResult, classify_group_state
from .presenters import build_public_dashboard_embed, build_staff_dashboard_embed
from .reporting import activity_report_payload, weekly_report_payload
from .repository import DomainError, Repository
from .schema import MIGRATIONS
from .timeutil import iso, previous_complete_week, utcnow
from .ui import ActivityControlView, PublicDashboardView, StaffDashboardView
from .vacation_sync import VacationSyncResult, partition_vacation_role

logger = logging.getLogger(__name__)

EXTENSIONS = [
    "familybot.cogs.setup",
    "familybot.cogs.members",
    "familybot.cogs.availability",
    "familybot.cogs.activities",
    "familybot.cogs.stats",
    "familybot.cogs.panel",
]


class FamilyBot(commands.Bot):
    def __init__(self, config: Config):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.config = config
        self.db = Database(config.database_path)
        self.repo = Repository(self.db)
        self.tree.on_error = self.on_tree_error
        self._last_backup_at = None

    async def setup_hook(self) -> None:
        # If an existing database needs a schema upgrade, preserve a coherent copy
        # before touching migrations. This makes host upgrades recoverable even if
        # the process is interrupted at the worst possible moment.
        current_version = await self.db.current_schema_version()
        target_version = max(version for version, _ in MIGRATIONS)
        if (
            current_version < target_version
            and Path(self.config.database_path).exists()
            and await self.db.file_size() > 0
        ):
            await self._create_database_backup(
                f"pre_migration_v{current_version}_to_v{target_version}"
            )
        await self.db.initialize()
        ok, detail = await self.db.integrity_check()
        if not ok:
            raise RuntimeError(f"Database integrity check failed: {detail}")
        logger.info("Database integrity check: %s", detail)
        await self._create_database_backup("startup")

        for ext in EXTENSIONS:
            await self.load_extension(ext)

        # Persistent views: all buttons have stable custom_id and timeout=None.
        self.add_view(PublicDashboardView())
        self.add_view(StaffDashboardView())
        for row in await self.repo.list_open_activities():
            if row["panel_message_id"]:
                self.add_view(
                    ActivityControlView(int(row["id"])),
                    message_id=int(row["panel_message_id"]),
                )

        if self.config.dev_guild_id:
            dev_guild = discord.Object(id=self.config.dev_guild_id)
            self.tree.copy_global_to(guild=dev_guild)
            await self.tree.sync(guild=dev_guild)
            logger.info("Synced commands to DEV_GUILD_ID=%s", self.config.dev_guild_id)
        else:
            await self.tree.sync()

        self.maintenance.start()

    async def close(self) -> None:
        if self.maintenance.is_running():
            self.maintenance.cancel()
        try:
            await self._create_database_backup("shutdown")
        except Exception:
            logger.exception("Could not create shutdown database backup")
        await super().close()

    async def _create_database_backup(self, reason: str = "auto") -> Path:
        self.config.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = utcnow().strftime("%Y%m%d_%H%M%S_%f")
        safe_reason = "".join(ch for ch in reason.lower() if ch.isalnum() or ch in "-_") or "backup"
        target = self.config.backup_dir / f"family_activity_{stamp}_{safe_reason}.db"
        await self.db.backup_to(target)
        self._last_backup_at = utcnow()
        await self._prune_database_backups()
        logger.info("Database backup created: %s", target)
        return target

    async def _prune_database_backups(self) -> None:
        cutoff = utcnow() - timedelta(days=self.config.backup_retention_days)
        if not self.config.backup_dir.exists():
            return
        for path in self.config.backup_dir.glob("family_activity_*.db"):
            try:
                modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                if modified_at < cutoff:
                    path.unlink(missing_ok=True)
                    logger.info("Old database backup removed: %s", path)
            except OSError:
                logger.exception("Could not prune database backup %s", path)

    async def database_status(self) -> dict[str, object]:
        ok, detail = await self.db.integrity_check()
        backups = []
        if self.config.backup_dir.exists():
            backups = sorted(
                self.config.backup_dir.glob("family_activity_*.db"),
                key=lambda x: x.stat().st_mtime,
                reverse=True,
            )
        return {
            "ok": ok,
            "integrity": detail,
            "size": await self.db.file_size(),
            "path": str(self.config.database_path),
            "backup_dir": str(self.config.backup_dir),
            "backup_count": len(backups),
            "latest_backup": str(backups[0]) if backups else None,
        }

    async def create_manual_database_backup(self) -> Path:
        return await self._create_database_backup("manual")

    async def _upsert_report_data_message(
        self,
        channel: discord.TextChannel,
        report_message: discord.Message,
        activity_id: int,
        csv_file: discord.File,
        existing_message_id: int | None,
    ) -> int | None:
        content = "📎 **Данные отчёта** · полный список участников в CSV."
        if existing_message_id:
            try:
                data_message = await channel.fetch_message(int(existing_message_id))
                await data_message.edit(content=content, attachments=[csv_file])
                return data_message.id
            except discord.NotFound:
                pass
        try:
            data_message = await report_message.reply(
                content=content,
                file=csv_file,
                mention_author=False,
            )
            await self.repo.mark_activity_report_data_message(activity_id, data_message.id)
            return data_message.id
        except discord.HTTPException:
            logger.exception("Could not publish activity report CSV #%s", activity_id)
            return None

    async def post_activity_report(
        self,
        guild: discord.Guild,
        activity_id: int,
        *,
        refresh: bool = False,
        extra_files: list[discord.File] | None = None,
    ) -> bool:
        # extra_files is kept for backwards compatibility with v1.2 callers, but
        # proof files are no longer attached to the report card itself. Discord renders
        # raw image attachments above embeds, which made reports visually confusing.
        settings = await self.repo.get_guild_settings(guild.id)
        if not settings or not settings.activity_report_channel_id:
            return False
        channel = guild.get_channel(settings.activity_report_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return False
        if not is_confidential_channel(channel, guild, settings):
            await send_log(
                self.repo,
                guild,
                "⚠️ Отчёт активности не опубликован: канал отчётов активностей доступен обычной роли семьи.",
            )
            return False
        row = await self.repo.get_activity(activity_id)
        if not row or int(row["guild_id"]) != guild.id or row["status"] not in {"closed", "finalized"}:
            return False
        try:
            embed, csv_file = await activity_report_payload(self.repo, guild.id, activity_id)
            existing_message_id = row["report_message_id"]
            existing_channel_id = row["report_channel_id"]
            report_message: discord.Message | None = None
            if existing_message_id and existing_channel_id == channel.id:
                try:
                    report_message = await channel.fetch_message(int(existing_message_id))
                    if refresh:
                        # Clear legacy v1.2 raw attachments from the main card. Evidence
                        # now lives in replies below the report and CSV in a data reply.
                        await report_message.edit(embed=embed, attachments=[])
                except discord.NotFound:
                    report_message = None
            if report_message is None:
                report_message = await channel.send(embed=embed)
                await self.repo.mark_activity_report_posted(activity_id, channel.id, report_message.id)
                row = await self.repo.get_activity(activity_id)

            await self._upsert_report_data_message(
                channel,
                report_message,
                activity_id,
                csv_file,
                int(row["report_data_message_id"]) if row and row["report_data_message_id"] else None,
            )
            return True
        except (discord.HTTPException, DomainError):
            logger.exception("Failed to publish activity report #%s in guild %s", activity_id, guild.id)
            return False

    async def mirror_activity_evidence(
        self,
        guild: discord.Guild,
        activity_id: int,
        evidence_id: int,
        proof_file: discord.File,
    ) -> bool:
        """Persist an uploaded proof *below* the canonical report card.

        The main report message contains only the embed. The proof is a reply, so
        Discord can never place a giant raw image above the report. The mirrored CDN
        URL is stored in SQLite and linked from the report embed.
        """
        if not await self.post_activity_report(guild, activity_id, refresh=True):
            return False
        row = await self.repo.get_activity(activity_id)
        if not row or not row["report_channel_id"] or not row["report_message_id"]:
            return False
        channel = guild.get_channel(int(row["report_channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            return False
        try:
            report_message = await channel.fetch_message(int(row["report_message_id"]))
            # Keep the CSV as the final child message: remove/recreate it after proof.
            if row["report_data_message_id"]:
                try:
                    old_data = await channel.fetch_message(int(row["report_data_message_id"]))
                    await old_data.delete()
                except discord.HTTPException:
                    pass
                await self.repo.mark_activity_report_data_message(activity_id, None)

            proof_message = await report_message.reply(
                content=f"📷 **Доказательство активности #{activity_id}**",
                file=proof_file,
                mention_author=False,
            )
            if not proof_message.attachments:
                return False
            mirrored_url = proof_message.attachments[0].url
            await self.repo.mark_activity_evidence_mirrored(
                evidence_id,
                mirrored_url=mirrored_url,
                channel_id=channel.id,
                message_id=proof_message.id,
            )
            # Refresh links in the main embed, then recreate CSV after all proofs.
            await self.post_activity_report(guild, activity_id, refresh=True)
            return True
        except discord.HTTPException:
            logger.exception("Could not mirror evidence #%s for activity #%s", evidence_id, activity_id)
            return False

    async def _ensure_activity_reports(self, guild: discord.Guild) -> None:
        settings = await self.repo.get_guild_settings(guild.id)
        if not settings or not settings.activity_report_channel_id:
            return
        rows = await self.repo.list_unposted_activity_reports(guild.id, limit=20)
        for row in rows:
            await self.post_activity_report(guild, int(row["id"]))

    async def on_ready(self) -> None:
        logger.info(
            "Family Activity Bot logged in as %s (%s)",
            self.user,
            self.user.id if self.user else "?",
        )
        # Reconcile external vacation role after every gateway reconnect. Operations are idempotent.
        for guild in self.guilds:
            try:
                await self._retire_legacy_dashboard(guild)
                settings = await self.repo.get_guild_settings(guild.id)
                if settings and settings.vacation_role_id and not settings.vacation_role_cutover_at:
                    cutover = iso(utcnow())
                    await self.repo.update_guild_settings(guild.id, vacation_role_cutover_at=cutover)
                    await send_log(
                        self.repo, guild,
                        "💤 Включён строгий режим отпусков: с этого момента текущий отпуск определяется только Discord-ролью внешнего бота. Старые записи остаются только историей до момента перехода."
                    )
                await self.sync_vacation_roles_for_guild(guild, source="startup_sync")
                settings = await self.repo.get_guild_settings(guild.id)
                if settings and settings.academy_role_id and settings.main_role_id:
                    if not settings.group_role_cutover_at:
                        cutover = iso(utcnow())
                        await self.repo.update_guild_settings(guild.id, group_role_cutover_at=cutover)
                        await send_log(
                            self.repo, guild,
                            "👥 Включена историческая синхронизация Academy/Main. "
                            "Группы участников отслеживаются по Discord-ролям с этого момента."
                        )
                    await self.sync_group_roles_for_guild(guild, source="startup_sync")
            except Exception:
                logger.exception("Startup reconciliation failed for guild %s", guild.id)

    async def on_tree_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        logger.error(
            "Application command error",
            exc_info=(type(error), error, error.__traceback__),
        )
        message = "❌ Произошла внутренняя ошибка. Она записана в лог бота."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        settings = await self.repo.get_guild_settings(after.guild.id)
        if not settings:
            return

        # Family role is advisory: never destroy membership history from a Discord role change.
        if settings.family_role_id:
            had_family = member_has_role(before, settings.family_role_id)
            has_family = member_has_role(after, settings.family_role_id)
            if had_family != has_family:
                active = await self.repo.get_active_membership_by_discord(after.guild.id, after.id)
                if had_family and not has_family and active:
                    await send_log(
                        self.repo,
                        after.guild,
                        f"⚠️ {after.mention} потерял роль семьи, но остаётся в активном составе базы. "
                        "Если человек действительно ушёл — используй `/состав выход`.",
                    )
                elif not had_family and has_family and not active:
                    await send_log(
                        self.repo,
                        after.guild,
                        f"⚠️ {after.mention} получил роль семьи, но его нет в базе состава. "
                        "Используй `/состав добавить` или `/состав вернуть`.",
                    )

        # Vacation role is authoritative for vacation state.
        if settings.vacation_role_id:
            had_vacation = member_has_role(before, settings.vacation_role_id)
            has_vacation = member_has_role(after, settings.vacation_role_id)
            if had_vacation != has_vacation:
                await self._sync_vacation_role_for_member(after, source="role_event")

        # Academy/Main roles are authoritative for the current subgroup. History is
        # interval-based, so future role changes never rewrite old activity statistics.
        if settings.academy_role_id and settings.main_role_id:
            watched = {settings.academy_role_id, settings.main_role_id}
            before_ids = {role.id for role in before.roles if role.id in watched}
            after_ids = {role.id for role in after.roles if role.id in watched}
            if before_ids != after_ids:
                await self._sync_group_roles_for_member(after, source="role_event")

    async def on_member_remove(self, member: discord.Member) -> None:
        active = await self.repo.get_active_membership_by_discord(member.guild.id, member.id)
        if active:
            await send_log(
                self.repo,
                member.guild,
                f"⚠️ **{active['nickname']}** (`{active['static_id']}`) вышел с Discord-сервера, "
                "но членство в семье не закрыто автоматически. Проверь причину и при необходимости "
                "используй `/состав выход_статик`.",
            )

    async def _sync_vacation_role_for_member(
        self,
        member: discord.Member,
        *,
        source: str,
    ) -> tuple[int, int]:
        settings = await self.repo.get_guild_settings(member.guild.id)
        if not settings or not settings.vacation_role_id:
            return 0, 0
        active = await self.repo.get_active_membership_by_discord(member.guild.id, member.id)
        has_role = member_has_role(member, settings.vacation_role_id)
        detected_at = utcnow()

        if not active:
            if has_role and source == "role_event":
                await send_log(
                    self.repo,
                    member.guild,
                    f"⚠️ {member.mention} получил роль отпуска, но его нет в активной базе состава. "
                    "Отпуск не записан в аналитику.",
                )
            return 0, 0

        membership_id = int(active["id"])
        if has_role:
            _, created = await self.repo.open_role_vacation(
                membership_id=membership_id,
                role_id=settings.vacation_role_id,
                starts_at=detected_at,
                source=source,
                actor_user_id=None,
            )
            if created:
                uncertainty = (
                    " Точное время выдачи роли неизвестно из-за сверки после простоя; "
                    "для статистики отпуск начинается с момента обнаружения."
                    if source != "role_event"
                    else ""
                )
                await send_log(
                    self.repo,
                    member.guild,
                    f"💤 **{active['nickname']}** (`{active['static_id']}`): отпуск автоматически открыт "
                    f"по роли <@&{settings.vacation_role_id}>.{uncertainty}",
                )
                return 1, 0
            return 0, 0

        closed = await self.repo.close_role_vacation(
            membership_id=membership_id,
            ends_at=detected_at,
            source=source,
            actor_user_id=None,
        )
        if closed:
            await send_log(
                self.repo,
                member.guild,
                f"🟢 **{active['nickname']}** (`{active['static_id']}`): роль отпуска снята, "
                "период отпуска автоматически закрыт.",
            )
            return 0, 1
        return 0, 0

    async def sync_vacation_roles_for_guild(
        self,
        guild: discord.Guild,
        *,
        source: str = "manual_sync",
    ) -> VacationSyncResult:
        """Reconcile the external Discord vacation role with active bot memberships.

        Important distinction: Discord can show users on the vacation role who are
        not yet present in Family Activity's active roster. We do not silently create
        memberships for them because Static ID / join history would be unknown.
        Instead the sync returns them as *unlinked* so staff sees the exact reason a
        Discord role count can differ from the analytics vacation count.
        """
        result = VacationSyncResult()
        settings = await self.repo.get_guild_settings(guild.id)
        if not settings or not settings.vacation_role_id:
            return result
        role = guild.get_role(settings.vacation_role_id)
        if role is None:
            logger.warning("Vacation role %s not found in guild %s", settings.vacation_role_id, guild.id)
            return result

        roster = await self.repo.list_active_members(guild.id)
        roster_ids = {
            int(row["discord_user_id"])
            for row in roster
            if row["discord_user_id"] is not None
        }

        # Role.members and Guild.members are cache-backed. With Intents.members enabled
        # they are normally complete; for a manual/startup reconciliation we also use
        # fetch_member as a safe fallback for a roster member missing from cache.
        role_holder_ids = {int(member.id) for member in role.members}
        resolved_members: dict[int, discord.Member] = {}

        for row in roster:
            discord_id = row["discord_user_id"]
            if discord_id is None:
                result.missing_discord_profiles += 1
                continue
            discord_id = int(discord_id)
            member = guild.get_member(discord_id)
            if member is None and source in {"manual_sync", "startup_sync"}:
                try:
                    member = await guild.fetch_member(discord_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    member = None
            if member is None:
                result.missing_discord_profiles += 1
                continue
            resolved_members[discord_id] = member
            # This also protects us from a temporarily stale Role.members cache.
            if member_has_role(member, settings.vacation_role_id):
                role_holder_ids.add(discord_id)

        partition = partition_vacation_role(role_holder_ids, roster_ids)
        result.discord_role_holders = len(partition.role_holder_ids)
        result.linked_role_holders = len(partition.linked_ids)
        result.unlinked_role_member_ids = tuple(sorted(partition.unlinked_role_ids))

        # Only active bot memberships are allowed into analytics. For them the role is
        # authoritative and opens/closes exact vacation intervals idempotently.
        for member in resolved_members.values():
            o, c = await self._sync_vacation_role_for_member(member, source=source)
            result.opened += o
            result.closed += c

        return result

    async def _sync_group_roles_for_member(
        self,
        member: discord.Member,
        *,
        source: str,
    ) -> bool:
        settings = await self.repo.get_guild_settings(member.guild.id)
        if not settings or not settings.academy_role_id or not settings.main_role_id:
            return False
        active = await self.repo.get_active_membership_by_discord(member.guild.id, member.id)
        if not active:
            if source == "role_event" and (
                member_has_role(member, settings.academy_role_id)
                or member_has_role(member, settings.main_role_id)
            ):
                await send_log(
                    self.repo,
                    member.guild,
                    f"⚠️ {member.mention} получил роль Academy/Main, но его нет в активной базе состава.",
                )
            return False

        state = classify_group_state(
            has_academy=member_has_role(member, settings.academy_role_id),
            has_main=member_has_role(member, settings.main_role_id),
        )
        changed = await self.repo.set_membership_group_state(
            membership_id=int(active["id"]),
            group_name=state,
            starts_at=utcnow(),
            source=source,
            actor_user_id=None,
        )
        if changed and state == "conflict":
            await send_log(
                self.repo,
                member.guild,
                f"⚠️ **{active['nickname']}** (`{active['static_id']}`) одновременно имеет роли "
                f"<@&{settings.academy_role_id}> и <@&{settings.main_role_id}>. Исправь конфликт ролей.",
            )
        elif changed and state == "unclassified":
            await send_log(
                self.repo,
                member.guild,
                f"⚪ **{active['nickname']}** (`{active['static_id']}`) не имеет ни Academy, ни Mein Rank. "
                "Он остаётся в базе семьи, но не входит в основной Family Pulse до исправления группы.",
            )
        return changed

    async def sync_group_roles_for_guild(
        self,
        guild: discord.Guild,
        *,
        source: str = "manual_sync",
    ) -> GroupSyncResult:
        result = GroupSyncResult()
        settings = await self.repo.get_guild_settings(guild.id)
        if not settings or not settings.academy_role_id or not settings.main_role_id:
            return result
        academy_role = guild.get_role(settings.academy_role_id)
        main_role = guild.get_role(settings.main_role_id)
        if academy_role is None or main_role is None:
            logger.warning("Academy/Main roles not found in guild %s", guild.id)
            return result

        roster = await self.repo.list_active_members(guild.id)
        roster_ids = {
            int(row["discord_user_id"])
            for row in roster
            if row["discord_user_id"] is not None
        }
        academy_holder_ids = {int(m.id) for m in academy_role.members if not m.bot}
        main_holder_ids = {int(m.id) for m in main_role.members if not m.bot}
        result.academy_role_holders = len(academy_holder_ids)
        result.main_role_holders = len(main_holder_ids)
        result.unlinked_academy_ids = tuple(sorted(academy_holder_ids - roster_ids))
        result.unlinked_main_ids = tuple(sorted(main_holder_ids - roster_ids))

        for row in roster:
            discord_id = row["discord_user_id"]
            if discord_id is None:
                result.missing_discord_profiles += 1
                continue
            discord_id = int(discord_id)
            member = guild.get_member(discord_id)
            if member is None and source in {"manual_sync", "startup_sync", "setup_sync"}:
                try:
                    member = await guild.fetch_member(discord_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    member = None
            if member is None:
                result.missing_discord_profiles += 1
                continue

            state = classify_group_state(
                has_academy=member_has_role(member, settings.academy_role_id),
                has_main=member_has_role(member, settings.main_role_id),
            )
            if state == "academy":
                result.academy_members += 1
            elif state == "main":
                result.main_members += 1
            elif state == "conflict":
                result.conflict_members += 1
            else:
                result.unclassified_members += 1
            if await self.repo.set_membership_group_state(
                membership_id=int(row["id"]),
                group_name=state,
                starts_at=utcnow(),
                source=source,
                actor_user_id=None,
            ):
                result.changed += 1
        return result

    async def _retire_legacy_dashboard(self, guild: discord.Guild) -> None:
        """Remove the v0.4 mixed dashboard so staff information is not left in a public channel."""
        settings = await self.repo.get_guild_settings(guild.id)
        if not settings or not settings.dashboard_channel_id or not settings.dashboard_message_id:
            return
        channel = guild.get_channel(settings.dashboard_channel_id)
        if isinstance(channel, discord.TextChannel):
            try:
                msg = await channel.fetch_message(settings.dashboard_message_id)
                embed = discord.Embed(
                    title="🔒 Старая панель отключена",
                    description=(
                        "Эта панель v0.4 смешивала пользовательские функции и внутреннюю аналитику. "
                        "В v1.0 она отключена. Создай отдельные `/панель участники` и `/панель руководство`."
                    ),
                )
                await msg.edit(embed=embed, view=None)
            except discord.HTTPException:
                pass
        await self.repo.update_guild_settings(
            guild.id,
            dashboard_channel_id=None,
            dashboard_message_id=None,
        )

    async def _refresh_dashboards_for_guild(self, guild: discord.Guild) -> None:
        settings = await self.repo.get_guild_settings(guild.id)
        if not settings:
            return

        if settings.public_dashboard_channel_id and settings.public_dashboard_message_id:
            channel = guild.get_channel(settings.public_dashboard_channel_id)
            if isinstance(channel, discord.TextChannel):
                try:
                    msg = await channel.fetch_message(settings.public_dashboard_message_id)
                    embed = await build_public_dashboard_embed(self.repo, guild.id)
                    await msg.edit(embed=embed, view=PublicDashboardView())
                except discord.HTTPException:
                    logger.warning("Could not refresh public dashboard for guild %s", guild.id)

        if settings.staff_dashboard_channel_id and settings.staff_dashboard_message_id:
            channel = guild.get_channel(settings.staff_dashboard_channel_id)
            if isinstance(channel, discord.TextChannel):
                # Security watchdog: if channel permissions are later opened to normal family members,
                # redact and disable the staff panel instead of continuing to expose analytics.
                if not is_confidential_channel(channel, guild, settings):
                    try:
                        msg = await channel.fetch_message(settings.staff_dashboard_message_id)
                        await msg.edit(
                            embed=discord.Embed(
                                title="🔒 Панель руководства отключена",
                                description="Канал стал доступен обычным участникам. Закрой права просмотра и создай `/панель руководство` заново.",
                            ),
                            view=None,
                        )
                    except discord.HTTPException:
                        pass
                    await self.repo.update_guild_settings(
                        guild.id,
                        staff_dashboard_channel_id=None,
                        staff_dashboard_message_id=None,
                    )
                    logger.warning("Staff dashboard auto-disabled after privacy change in guild %s", guild.id)
                else:
                    try:
                        msg = await channel.fetch_message(settings.staff_dashboard_message_id)
                        embed = await build_staff_dashboard_embed(self.repo, guild.id)
                        await msg.edit(embed=embed, view=StaffDashboardView())
                    except discord.HTTPException:
                        logger.warning("Could not refresh staff dashboard for guild %s", guild.id)

    async def _ensure_last_week_report(self, guild: discord.Guild) -> None:
        settings = await self.repo.get_guild_settings(guild.id)
        if not settings or not settings.report_channel_id:
            return
        now = utcnow()
        start, end = previous_complete_week(now, settings.timezone)
        previous_start, previous_end = start - timedelta(days=7), end - timedelta(days=7)
        existing = await self.repo.get_weekly_report(guild.id, start, end)
        if existing and existing["posted_message_id"]:
            return
        embed, file, metrics, explanations = await weekly_report_payload(
            self.repo, guild.id, start, end, previous_start, previous_end
        )
        report_id = await self.repo.save_weekly_report(
            guild_id=guild.id,
            week_start=start,
            week_end=end,
            pulse_score=metrics["score"],
            metrics=metrics,
            explanations=explanations,
        )
        channel = guild.get_channel(settings.report_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        if not is_confidential_channel(channel, guild, settings):
            logger.warning("Weekly report suppressed because report channel is not private in guild %s", guild.id)
            await send_log(
                self.repo, guild,
                "⚠️ Недельный отчёт не опубликован: настроенный канал отчётов стал доступен обычной роли семьи. Закрой права канала и повтори `/семья настройка`."
            )
            return
        try:
            message = await channel.send(embed=embed, file=file)
            await self.repo.mark_weekly_report_posted(report_id, channel.id, message.id)
        except discord.HTTPException:
            logger.exception("Failed to post weekly report for guild %s", guild.id)

    @tasks.loop(minutes=10)
    async def maintenance(self) -> None:
        try:
            await self.repo.finalize_old_activities(utcnow() - timedelta(hours=24))
            if (
                self._last_backup_at is None
                or utcnow() - self._last_backup_at >= timedelta(hours=self.config.backup_interval_hours)
            ):
                await self._create_database_backup("auto")
            for guild in self.guilds:
                try:
                    await self.sync_vacation_roles_for_guild(guild, source="periodic_sync")
                    await self.sync_group_roles_for_guild(guild, source="periodic_sync")
                    await self._refresh_dashboards_for_guild(guild)
                    await self._ensure_activity_reports(guild)
                    await self._ensure_last_week_report(guild)
                except Exception:
                    logger.exception("Maintenance failed for guild %s", guild.id)
        except Exception:
            logger.exception("Maintenance loop failed")

    @maintenance.before_loop
    async def before_maintenance(self) -> None:
        await self.wait_until_ready()
