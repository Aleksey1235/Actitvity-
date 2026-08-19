from __future__ import annotations

from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from ..helpers import get_settings, is_staff
from ..repository import DomainError, Repository
from ..timeutil import SEGMENT_LABELS, next_local_monday, parse_segments, utcnow


class AvailabilityCog(commands.Cog):
    availability = app_commands.Group(name="время", description="Обычное время участия в активностях")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.repo: Repository = getattr(bot, "repo")

    @availability.command(name="настроить", description="Настроить обычное время участия")
    @app_commands.describe(
        weekdays="Например: вечер, ночь или плавающий",
        weekends="Например: день, вечер или плавающий",
        member="Пусто = настроить себя; старший состав может выбрать другого участника",
    )
    @app_commands.rename(weekdays='будни', weekends='выходные', member='участник')
    async def set_availability(
        self,
        interaction: discord.Interaction,
        weekdays: str,
        weekends: str,
        member: discord.Member | None = None,
    ) -> None:
        settings = await get_settings(self.repo, interaction)
        if settings is None:
            return
        target = member or interaction.user
        if not isinstance(target, discord.Member):
            await interaction.response.send_message("Не удалось определить участника.", ephemeral=True)
            return
        if target.id != interaction.user.id:
            if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user, settings):
                await interaction.response.send_message("Чужой график может менять только старший состав.", ephemeral=True)
                return
        membership = await self.repo.get_active_membership_by_discord(interaction.guild_id, target.id)
        if not membership:
            await interaction.response.send_message("❌ Участника нет в активном составе.", ephemeral=True)
            return
        try:
            weekday_segments = parse_segments(weekdays)
            weekend_segments = parse_segments(weekends)
            has_history = await self.repo.has_any_availability(int(membership["id"]))
            effective_from = (
                next_local_monday(utcnow(), settings.timezone) if has_history else utcnow()
            )
            await self.repo.set_availability(
                membership_id=int(membership["id"]),
                weekday_segments=weekday_segments,
                weekend_segments=weekend_segments,
                effective_from=effective_from,
                actor_user_id=interaction.user.id,
            )
            week_text = ", ".join(SEGMENT_LABELS[s] for s in sorted(weekday_segments))
            weekend_text = ", ".join(SEGMENT_LABELS[s] for s in sorted(weekend_segments))
            when_text = "сейчас" if not has_history else "со следующего понедельника"
            await interaction.response.send_message(
                f"✅ График **{membership['nickname']}** сохранён и действует **{when_text}**.\n"
                f"Будни: {week_text}\nВыходные: {weekend_text}\n\n"
                "Изменение не пересчитывает прошлую статистику.",
                ephemeral=True,
            )
        except (ValueError, DomainError) as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)

    @availability.command(name="показать", description="Показать текущий график участия")
    @app_commands.rename(member='участник')
    async def show(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        settings = await get_settings(self.repo, interaction)
        if settings is None:
            return
        target = member or interaction.user
        if not isinstance(target, discord.Member):
            return
        if target.id != interaction.user.id:
            if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user, settings):
                await interaction.response.send_message("Чужой график доступен только старшему составу.", ephemeral=True)
                return
        membership = await self.repo.get_active_membership_by_discord(interaction.guild_id, target.id)
        if not membership:
            await interaction.response.send_message("❌ Участника нет в активном составе.", ephemeral=True)
            return
        rows = await self.repo.availability_at(int(membership["id"]), utcnow())
        if not rows:
            await interaction.response.send_message("⚪ График пока не настроен.", ephemeral=True)
            return
        grouped = {"weekday": [], "weekend": []}
        for row in rows:
            grouped[row["day_group"]].append(SEGMENT_LABELS[row["segment"]])
        await interaction.response.send_message(
            f"### 🕐 {membership['nickname']}\n"
            f"Будни: {', '.join(grouped['weekday']) or '—'}\n"
            f"Выходные: {', '.join(grouped['weekend']) or '—'}",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AvailabilityCog(bot))
