from __future__ import annotations

import json
from datetime import datetime, time
from zoneinfo import ZoneInfo

import discord

from .repository import GuildSettings, Repository
from .timeutil import UTC, dt_to_discord_timestamp

CATEGORY_LABELS = {
    "training": "🏋️ Тренировка",
    "family": "🏠 Семейный контент",
    "faction": "🏛 Фракционный контент",
}

STATUS_LABELS = {
    "scheduled": "📅 Запланирована",
    "running": "🟢 Идёт",
    "closed": "✅ Закрыта",
    "finalized": "🔒 Архив",
    "cancelled": "❌ Отменена",
}

EXIT_LABELS = {
    "voluntary": "Ушёл сам",
    "kicked": "Исключён",
    "other": "Другое",
}


def member_has_role(member: discord.Member, role_id: int | None) -> bool:
    return role_id is not None and any(role.id == role_id for role in member.roles)


async def get_settings(repo: Repository, interaction: discord.Interaction) -> GuildSettings | None:
    if not interaction.guild_id:
        if not interaction.response.is_done():
            await interaction.response.send_message("Команда работает только на сервере.", ephemeral=True)
        return None
    settings = await repo.get_guild_settings(interaction.guild_id)
    if not settings or not settings.family_role_id or not settings.staff_role_id or not settings.leader_role_id:
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Бот ещё не настроен. Администратор должен выполнить `/семья настройка`.",
                ephemeral=True,
            )
        return None
    return settings


def is_staff(member: discord.Member, settings: GuildSettings) -> bool:
    return (
        member.guild_permissions.administrator
        or member_has_role(member, settings.staff_role_id)
        or member_has_role(member, settings.leader_role_id)
    )


def is_leader(member: discord.Member, settings: GuildSettings) -> bool:
    return member.guild_permissions.administrator or member_has_role(member, settings.leader_role_id)


def is_confidential_channel(
    channel: discord.TextChannel,
    guild: discord.Guild,
    settings: GuildSettings,
) -> bool:
    """True when ordinary family members cannot view a staff-only channel."""
    if channel.permissions_for(guild.default_role).view_channel:
        return False
    if settings.family_role_id:
        family_role = guild.get_role(settings.family_role_id)
        if family_role and channel.permissions_for(family_role).view_channel:
            return False
    return True


async def require_staff(repo: Repository, interaction: discord.Interaction) -> GuildSettings | None:
    settings = await get_settings(repo, interaction)
    if settings is None:
        return None
    if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user, settings):
        if not interaction.response.is_done():
            await interaction.response.send_message("Эта функция доступна старшему составу.", ephemeral=True)
        return None
    return settings


async def require_leader(repo: Repository, interaction: discord.Interaction) -> GuildSettings | None:
    settings = await get_settings(repo, interaction)
    if settings is None:
        return None
    if not isinstance(interaction.user, discord.Member) or not is_leader(interaction.user, settings):
        if not interaction.response.is_done():
            await interaction.response.send_message("Эта функция доступна лидеру/администратору.", ephemeral=True)
        return None
    return settings


async def send_log(repo: Repository, guild: discord.Guild, text: str) -> None:
    settings = await repo.get_guild_settings(guild.id)
    if not settings or not settings.log_channel_id:
        return
    channel = guild.get_channel(settings.log_channel_id)
    if isinstance(channel, discord.TextChannel):
        # Never leak audit data if someone later makes the configured log channel public.
        if not is_confidential_channel(channel, guild, settings):
            return
        try:
            await channel.send(text)
        except discord.HTTPException:
            pass


def activity_embed(row, participant_count: int | None = None) -> discord.Embed:
    scheduled = datetime.fromisoformat(row["scheduled_for"])
    embed = discord.Embed(
        title=f"{CATEGORY_LABELS.get(row['category'], '🎮')} · {row['title']}",
        description=row["description"] or "",
    )
    embed.add_field(name="Статус", value=STATUS_LABELS.get(row["status"], row["status"]), inline=True)
    embed.add_field(name="Начало", value=dt_to_discord_timestamp(scheduled, "F"), inline=True)
    mode = "⚡ Спонтанная" if row["is_spontaneous"] else "📅 Запланированная"
    mode_source = {
        "auto": "авто",
        "planned": "выбрано организатором",
        "spontaneous": "выбрано организатором",
    }.get(str(row["classification_mode"]) if "classification_mode" in row.keys() else "auto", "авто")
    analytical = "📊 учитывается" if row["analytical"] else "📎 только история"
    embed.add_field(
        name="Режим",
        value=f"{mode} · {mode_source}\nОбъявлено за **{row['notice_minutes']} мин**\n{analytical}",
        inline=True,
    )
    audience = row["audience_type"]
    audience_group = row["audience_group"] if "audience_group" in row.keys() else None
    if audience_group == "academy":
        audience_text = "🎓 Только Academy"
    elif audience_group == "main":
        audience_text = "🏠 Только основной состав (Mein Rank)"
    elif audience == "all":
        audience_text = "Вся семья"
    elif audience == "rank_range":
        audience_text = f"Ранги {row['min_rank']}–{row['max_rank']}"
    else:
        audience_text = "Выбранная группа"
    embed.add_field(name="Аудитория", value=audience_text, inline=True)
    if participant_count is not None:
        embed.add_field(name="Участников", value=str(participant_count), inline=True)
    embed.add_field(name="ID", value=f"`{row['id']}`", inline=True)
    embed.set_footer(text="Спонтанная активность никогда не создаёт пропуск в персональной статистике.")
    return embed


def local_midnight_utc(date_value: str, tz_name: str) -> datetime:
    d = datetime.strptime(date_value, "%Y-%m-%d").date()
    return datetime.combine(d, time.min, tzinfo=ZoneInfo(tz_name)).astimezone(UTC)


def truncate(text: str, limit: int = 1000) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
