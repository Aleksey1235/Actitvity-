from __future__ import annotations

from datetime import datetime
import csv
import io
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

from ..helpers import EXIT_LABELS, get_settings, require_leader, require_staff
from ..presenters import build_member_stats_embed, build_roster_embed
from ..repository import DomainError, Repository
from ..timeutil import UTC, parse_iso, utcnow

EXIT_CHOICES = [
    app_commands.Choice(name="Ушёл сам", value="voluntary"),
    app_commands.Choice(name="Исключён", value="kicked"),
    app_commands.Choice(name="Другое", value="other"),
]


class MembersCog(commands.Cog):
    member = app_commands.Group(name="состав", description="Управление составом семьи")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.repo: Repository = getattr(bot, "repo")

    async def _joined_at(self, guild_id: int, joined_date: str | None) -> datetime:
        settings = await self.repo.ensure_guild_settings(guild_id)
        if not joined_date:
            return utcnow()
        try:
            local_date = datetime.strptime(joined_date.strip(), "%Y-%m-%d").date()
        except ValueError as exc:
            raise DomainError("Дата вступления: формат YYYY-MM-DD") from exc
        return datetime.combine(local_date, datetime.min.time(), tzinfo=ZoneInfo(settings.timezone)).astimezone(UTC)

    async def _sync_add_role(self, interaction: discord.Interaction, member: discord.Member, role_id: int | None) -> str | None:
        if not role_id or any(r.id == role_id for r in member.roles):
            return None
        role = interaction.guild.get_role(role_id) if interaction.guild else None
        if not role:
            return "Роль семьи не найдена на сервере."
        try:
            await member.add_roles(role, reason="Family Activity: добавлен в состав")
        except discord.HTTPException:
            return "Не удалось выдать роль семьи (проверь права и иерархию ролей)."
        return None

    async def _sync_remove_role(self, interaction: discord.Interaction, member: discord.Member, role_id: int | None) -> str | None:
        if not role_id:
            return None
        role = interaction.guild.get_role(role_id) if interaction.guild else None
        if not role or not any(r.id == role.id for r in member.roles):
            return None
        try:
            await member.remove_roles(role, reason="Family Activity: выход из состава")
        except discord.HTTPException:
            return "Не удалось снять роль семьи (проверь права и иерархию ролей)."
        return None

    @member.command(name="добавить", description="Добавить человека в текущий состав")
    @app_commands.describe(member="Discord участник", nickname="Игровой Nickname", static_id="Static ID", rank="Ранг семьи", joined_date="Дата вступления YYYY-MM-DD; пусто = сегодня")
    @app_commands.rename(member='участник', nickname='ник', static_id='статик', rank='ранг', joined_date='дата_вступления')
    async def add(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        nickname: str,
        static_id: str,
        rank: app_commands.Range[int, 0, 99],
        joined_date: str | None = None,
    ) -> None:
        settings = await require_staff(self.repo, interaction)
        if settings is None:
            return
        try:
            membership_id = await self.repo.add_member(
                guild_id=interaction.guild_id,
                discord_user_id=member.id,
                static_id=static_id,
                nickname=nickname,
                rank=int(rank),
                joined_at=await self._joined_at(interaction.guild_id, joined_date),
                actor_user_id=interaction.user.id,
            )
            role_warning = await self._sync_add_role(interaction, member, settings.family_role_id)
            if interaction.guild:
                await self.bot._sync_group_roles_for_member(member, source="manual_sync")  # type: ignore[attr-defined]
                await self.bot._sync_vacation_role_for_member(member, source="manual_sync")  # type: ignore[attr-defined]
            msg = f"✅ **{nickname}** (`{static_id}`) добавлен в состав. Membership ID: `{membership_id}`."
            if role_warning:
                msg += f"\n⚠️ {role_warning}"
            msg += "\nНастрой его обычное время участия через `/время настроить`."
            await interaction.response.send_message(msg, ephemeral=True)
        except DomainError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)

    @member.command(name="вернуть", description="Вернуть ранее ушедшего участника, сохранив старую историю")
    @app_commands.describe(member="Discord участник", static_id="Старый Static ID", nickname="Текущий игровой Nickname", rank="Новый ранг", joined_date="Дата возвращения YYYY-MM-DD")
    @app_commands.rename(member='участник', static_id='статик', nickname='ник', rank='ранг', joined_date='дата_возврата')
    async def rejoin(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        static_id: str,
        nickname: str,
        rank: app_commands.Range[int, 0, 99],
        joined_date: str | None = None,
    ) -> None:
        settings = await require_staff(self.repo, interaction)
        if settings is None:
            return
        person = await self.repo.get_person_by_static(interaction.guild_id, static_id)
        if not person:
            await interaction.response.send_message("❌ Такой Static ID раньше не был в базе. Используй `/состав добавить`.", ephemeral=True)
            return
        try:
            membership_id = await self.repo.rejoin_member(
                guild_id=interaction.guild_id,
                discord_user_id=member.id,
                static_id=static_id,
                nickname=nickname,
                rank=int(rank),
                joined_at=await self._joined_at(interaction.guild_id, joined_date),
                actor_user_id=interaction.user.id,
            )
            role_warning = await self._sync_add_role(interaction, member, settings.family_role_id)
            if interaction.guild:
                await self.bot._sync_group_roles_for_member(member, source="manual_sync")  # type: ignore[attr-defined]
                await self.bot._sync_vacation_role_for_member(member, source="manual_sync")  # type: ignore[attr-defined]
            history = await self.repo.list_membership_history_for_person(int(person["id"]))
            msg = (
                f"✅ **{nickname}** вернулся в семью. Создан новый период членства `{membership_id}`.\n"
                f"Предыдущая история сохранена; периодов членства теперь: **{len(history)}**."
            )
            if role_warning:
                msg += f"\n⚠️ {role_warning}"
            await interaction.response.send_message(msg, ephemeral=True)
        except DomainError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)

    @member.command(name="выход", description="Закрыть период членства: человек ушёл/был исключён")
    @app_commands.choices(exit_type=EXIT_CHOICES)
    @app_commands.rename(member='участник', exit_type='тип_выхода', reason='причина')
    async def leave(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        exit_type: app_commands.Choice[str],
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
            await self.repo.leave_member(
                membership_id=int(membership["id"]),
                exit_type=exit_type.value,
                reason=reason,
                actor_user_id=interaction.user.id,
            )
            warnings = []
            for role_id in (settings.family_role_id, settings.academy_role_id, settings.main_role_id):
                warning = await self._sync_remove_role(interaction, member, role_id)
                if warning:
                    warnings.append(warning)
            role_warning = "; ".join(warnings) if warnings else None
            msg = (
                f"✅ Период членства **{membership['nickname']}** закрыт.\n"
                f"Тип: **{EXIT_LABELS[exit_type.value]}**\nПричина: {reason}\n"
                "История активностей и старый период членства сохранены."
            )
            if role_warning:
                msg += f"\n⚠️ {role_warning}"
            await interaction.response.send_message(msg, ephemeral=True)
        except DomainError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)

    @member.command(name="выход_статик", description="Закрыть членство по Static ID, даже если человека уже нет в Discord")
    @app_commands.choices(exit_type=EXIT_CHOICES)
    @app_commands.rename(static_id='статик', exit_type='тип_выхода', reason='причина')
    async def leave_static(
        self,
        interaction: discord.Interaction,
        static_id: str,
        exit_type: app_commands.Choice[str],
        reason: str,
    ) -> None:
        settings = await require_staff(self.repo, interaction)
        if settings is None:
            return
        membership = await self.repo.get_active_membership_by_static(interaction.guild_id, static_id)
        if not membership:
            await interaction.response.send_message("❌ Активный участник с таким Static ID не найден.", ephemeral=True)
            return
        try:
            await self.repo.leave_member(
                membership_id=int(membership["id"]),
                exit_type=exit_type.value,
                reason=reason,
                actor_user_id=interaction.user.id,
            )
            await interaction.response.send_message(
                f"✅ **{membership['nickname']}** (`{static_id}`) перенесён в историю состава. Данные не удалены.",
                ephemeral=True,
            )
        except DomainError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)

    @member.command(name="ранг", description="Изменить ранг с сохранением истории")
    @app_commands.rename(member='участник', new_rank='новый_ранг', reason='причина')
    async def rank(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        new_rank: app_commands.Range[int, 0, 99],
        reason: str | None = None,
    ) -> None:
        settings = await require_staff(self.repo, interaction)
        if settings is None:
            return
        membership = await self.repo.get_active_membership_by_discord(interaction.guild_id, member.id)
        if not membership:
            await interaction.response.send_message("❌ Участника нет в активном составе.", ephemeral=True)
            return
        try:
            old = int(membership["rank"])
            await self.repo.change_rank(int(membership["id"]), int(new_rank), interaction.user.id, reason)
            await interaction.response.send_message(f"✅ Ранг **{membership['nickname']}**: `{old}` → `{new_rank}`.", ephemeral=True)
        except DomainError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)

    @member.command(name="профиль", description="Статистика активного участника")
    @app_commands.rename(member='участник')
    async def profile(self, interaction: discord.Interaction, member: discord.Member) -> None:
        settings = await require_staff(self.repo, interaction)
        if settings is None:
            return
        membership = await self.repo.get_active_membership_by_discord(interaction.guild_id, member.id)
        if not membership:
            await interaction.response.send_message("❌ Участника нет в активном составе.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        embed = await build_member_stats_embed(self.repo, interaction.guild_id, int(membership["id"]), staff_view=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @member.command(name="история", description="История вступлений/выходов человека по Static ID")
    @app_commands.rename(static_id='статик')
    async def history(self, interaction: discord.Interaction, static_id: str) -> None:
        settings = await require_staff(self.repo, interaction)
        if settings is None:
            return
        person = await self.repo.get_person_by_static(interaction.guild_id, static_id)
        if not person:
            await interaction.response.send_message("❌ Static ID не найден.", ephemeral=True)
            return
        periods = await self.repo.list_membership_history_for_person(int(person["id"]))
        lines = []
        for p in periods:
            joined = parse_iso(p["joined_at"])
            left = parse_iso(p["left_at"]) if p["left_at"] else None
            status = "🟢 сейчас в семье" if p["status"] == "active" else f"⚫ {EXIT_LABELS.get(p['exit_type'], 'Ушёл')}"
            line = f"• `{p['id']}` · {joined.date()} → {left.date() if left else 'сейчас'} · R{p['rank']} · {status}"
            if p["exit_reason"]:
                line += f" · {p['exit_reason']}"
            lines.append(line)
            groups = await self.repo.list_group_history(int(p["id"]))
            group_labels = {
                "academy": "🎓 Academy",
                "main": "🏠 Основной",
                "unclassified": "⚪ Без группы",
                "conflict": "⚠️ Конфликт",
            }
            for gp in groups:
                g_start = parse_iso(gp["starts_at"])
                g_end = parse_iso(gp["ends_at"]) if gp["ends_at"] else None
                lines.append(
                    f"  ↳ {group_labels.get(gp['group_name'], gp['group_name'])}: "
                    f"{g_start.date()} → {g_end.date() if g_end else 'сейчас'}"
                )
        await interaction.response.send_message(
            f"### 📜 {person['nickname']} · `{person['static_id']}`\n" + "\n".join(lines),
            ephemeral=True,
        )

    @member.command(name="импорт", description="Атомарно импортировать существующий состав из CSV")
    @app_commands.rename(file='файл')
    async def import_csv(self, interaction: discord.Interaction, file: discord.Attachment) -> None:
        settings = await require_leader(self.repo, interaction)
        if settings is None:
            return
        if file.size > 512_000:
            await interaction.response.send_message("❌ CSV слишком большой (лимит 500 KB).", ephemeral=True)
            return
        try:
            raw = await file.read()
            text = raw.decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text))
            required = {"discord_id", "nickname", "static_id", "rank"}
            if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
                raise DomainError(
                    "CSV columns: discord_id,nickname,static_id,rank,joined_date(optional)"
                )
            parsed = []
            missing_discord = []
            for line_no, row in enumerate(reader, start=2):
                try:
                    discord_id = int((row.get("discord_id") or "").strip())
                    rank = int((row.get("rank") or "").strip())
                except ValueError as exc:
                    raise DomainError(f"Строка {line_no}: discord_id и rank должны быть числами") from exc
                discord_member = interaction.guild.get_member(discord_id) if interaction.guild else None
                if discord_member is None:
                    missing_discord.append(f"строка {line_no}: {discord_id}")
                joined_raw = (row.get("joined_date") or "").strip() or None
                joined_at = await self._joined_at(interaction.guild_id, joined_raw)
                parsed.append(
                    {
                        "discord_user_id": discord_id,
                        "nickname": (row.get("nickname") or "").strip(),
                        "static_id": (row.get("static_id") or "").strip(),
                        "rank": rank,
                        "joined_at": joined_at,
                    }
                )
            if missing_discord:
                raise DomainError(
                    "Некоторые Discord ID не найдены на сервере: " + ", ".join(missing_discord[:10])
                )
            ids = await self.repo.bulk_add_members(
                guild_id=interaction.guild_id,
                members=parsed,
                actor_user_id=interaction.user.id,
            )
            await interaction.response.send_message(
                f"✅ Импорт завершён атомарно: **{len(ids)}** участников добавлено. "
                "Если бы хотя бы одна строка конфликтовала, не добавился бы никто.",
                ephemeral=True,
            )
        except (UnicodeDecodeError, DomainError, ValueError) as exc:
            await interaction.response.send_message(f"❌ Импорт отменён: {exc}", ephemeral=True)

    @member.command(name="сверка", description="Сверить Discord-роль семьи с базой состава")
    async def reconcile(self, interaction: discord.Interaction) -> None:
        settings = await require_staff(self.repo, interaction)
        if settings is None:
            return
        if not interaction.guild or not settings.family_role_id:
            return
        role = interaction.guild.get_role(settings.family_role_id)
        if role is None:
            await interaction.response.send_message("❌ Роль семьи не найдена.", ephemeral=True)
            return
        roster = await self.repo.list_active_members(interaction.guild_id)
        db_ids = {int(r["discord_user_id"]) for r in roster if r["discord_user_id"] is not None}
        role_ids = {m.id for m in role.members if not m.bot}
        role_only = sorted(role_ids - db_ids)
        db_only = sorted(db_ids - role_ids)
        lines = [
            f"Роль Discord: **{len(role_ids)}**",
            f"Активная база: **{len(db_ids)}**",
            "",
            f"⚠️ В роли, но не в базе: **{len(role_only)}**",
            ", ".join(f"<@{uid}>" for uid in role_only[:20]) or "—",
            "",
            f"⚠️ В базе, но без роли: **{len(db_only)}**",
            ", ".join(f"<@{uid}>" for uid in db_only[:20]) or "—",
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @member.command(name="найти", description="Найти участника по Nickname или Static ID")
    @app_commands.rename(query='поиск')
    async def find(self, interaction: discord.Interaction, query: str) -> None:
        settings = await require_staff(self.repo, interaction)
        if settings is None:
            return
        q = query.strip()
        rows = await self.repo.db.fetchall(
            """
            SELECT p.*, m.id AS membership_id, m.rank, m.status, m.joined_at, m.left_at, m.exit_type
            FROM people p
            LEFT JOIN memberships m ON m.person_id=p.id
            WHERE p.guild_id=? AND (p.static_id=? OR p.nickname LIKE ?)
            ORDER BY CASE WHEN m.status='active' THEN 0 ELSE 1 END, m.joined_at DESC
            LIMIT 20
            """,
            (interaction.guild_id, q, f"%{q}%"),
        )
        lines = []
        for r in rows:
            status = "🟢 в составе" if r['status'] == 'active' else "⚫ история"
            lines.append(
                f"• **{r['nickname']}** `{r['static_id']}` · {status}"
                + (f" · R{r['rank']}" if r['rank'] is not None else "")
                + (f" · membership `{r['membership_id']}`" if r['membership_id'] is not None else "")
            )
        await interaction.response.send_message(
            "### 🔎 Результаты поиска\n" + ("\n".join(lines) if lines else "Ничего не найдено."),
            ephemeral=True,
        )

    @member.command(name="список", description="Показать текущий состав")
    async def list_members(self, interaction: discord.Interaction) -> None:
        settings = await require_staff(self.repo, interaction)
        if settings is None:
            return
        embed = await build_roster_embed(self.repo, interaction.guild_id)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @member.command(name="вышедшие", description="Последние выходы из семьи")
    async def departed(self, interaction: discord.Interaction) -> None:
        settings = await require_staff(self.repo, interaction)
        if settings is None:
            return
        rows = await self.repo.db.fetchall(
            """
            SELECT m.*, p.nickname, p.static_id, p.discord_user_id
            FROM memberships m
            JOIN people p ON p.id=m.person_id
            WHERE m.guild_id=? AND m.status='departed'
            ORDER BY m.left_at DESC
            LIMIT 30
            """,
            (interaction.guild_id,),
        )
        lines = []
        for r in rows:
            kind = EXIT_LABELS.get(r['exit_type'], 'Ушёл')
            lines.append(
                f"• **{r['nickname']}** `{r['static_id']}` · {kind} · {parse_iso(r['left_at']).date() if r['left_at'] else '—'}"
                + (f" · {r['exit_reason']}" if r['exit_reason'] else "")
            )
        await interaction.response.send_message(
            "### 🚪 История последних выходов\n" + ("\n".join(lines) if lines else "История выходов пока пустая."),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MembersCog(bot))
