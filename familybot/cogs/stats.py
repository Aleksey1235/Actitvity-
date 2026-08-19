from __future__ import annotations

from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from ..helpers import get_settings, is_staff, require_staff
from ..chart_service import build_chart
from ..presenters import (
    build_academy_overview_embed,
    build_attention_embed,
    build_family_pulse_embed,
    build_member_stats_embed,
)
from ..reporting import weekly_report_payload
from ..repository import DomainError, Repository
from ..timeutil import current_week_bounds, previous_complete_week, utcnow


GRAPH_CHOICES = [
    app_commands.Choice(name="Неделя · эта vs прошлая", value="week"),
    app_commands.Choice(name="Family Pulse · история", value="pulse"),
    app_commands.Choice(name="Категории контента", value="categories"),
    app_commands.Choice(name="Основной состав vs Academy", value="groups"),
    app_commands.Choice(name="Расписание активностей", value="schedule"),
]


class StatsCog(commands.Cog):
    stats = app_commands.Group(name="статистика", description="Статистика активности семьи")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.repo: Repository = getattr(bot, "repo")

    @stats.command(name="моя", description="Моя личная статистика без баллов")
    async def me(self, interaction: discord.Interaction) -> None:
        settings = await get_settings(self.repo, interaction)
        if settings is None:
            return
        membership = await self.repo.get_active_membership_by_discord(interaction.guild_id, interaction.user.id)
        if not membership:
            await interaction.response.send_message("❌ Тебя нет в активном составе.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        embed = await build_member_stats_embed(self.repo, interaction.guild_id, int(membership["id"]), staff_view=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @stats.command(name="участник", description="Оценка и статистика участника для руководства")
    @app_commands.rename(member='участник')
    async def member(self, interaction: discord.Interaction, member: discord.Member) -> None:
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

    @stats.command(name="семья", description="Текущий Family Pulse с объяснением")
    async def family(self, interaction: discord.Interaction) -> None:
        settings = await require_staff(self.repo, interaction)
        if settings is None:
            return
        await interaction.response.defer(ephemeral=True)
        embed = await build_family_pulse_embed(self.repo, interaction.guild_id)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @stats.command(name="академия", description="Отдельная статистика Academy для руководства")
    async def academy(self, interaction: discord.Interaction) -> None:
        settings = await require_staff(self.repo, interaction)
        if settings is None:
            return
        await interaction.response.defer(ephemeral=True)
        embed = await build_academy_overview_embed(self.repo, interaction.guild_id)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @stats.command(name="внимание", description="Кого и что руководству стоит проверить")
    async def attention(self, interaction: discord.Interaction) -> None:
        settings = await require_staff(self.repo, interaction)
        if settings is None:
            return
        await interaction.response.defer(ephemeral=True)
        embed = await build_attention_embed(self.repo, interaction.guild_id)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @stats.command(name="график", description="Построить аккуратный график для руководства")
    @app_commands.choices(chart_type=GRAPH_CHOICES)
    @app_commands.rename(chart_type="тип", days="дней")
    @app_commands.describe(
        chart_type="Какой график построить",
        days="Период для категорий, Academy/Main и расписания (7–90 дней)",
    )
    async def chart(
        self,
        interaction: discord.Interaction,
        chart_type: app_commands.Choice[str],
        days: app_commands.Range[int, 7, 90] = 28,
    ) -> None:
        settings = await require_staff(self.repo, interaction)
        if settings is None:
            return
        await interaction.response.defer(ephemeral=True)
        try:
            payload = await build_chart(
                self.repo, interaction.guild_id, chart_type.value, days=int(days)
            )
        except DomainError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        file = discord.File(payload.image, filename=payload.filename)
        embed = discord.Embed(title=payload.title, description=payload.description)
        embed.set_image(url=f"attachment://{payload.filename}")
        embed.set_footer(text="График строится заново из SQLite; PNG не является источником данных.")
        await interaction.followup.send(embed=embed, file=file, ephemeral=True)

    @stats.command(name="неделя", description="Отчёт текущей недели vs те же дни прошлой")
    async def week(self, interaction: discord.Interaction) -> None:
        settings = await require_staff(self.repo, interaction)
        if settings is None:
            return
        await interaction.response.defer(ephemeral=True)
        now = utcnow()
        start, week_end = current_week_bounds(now, settings.timezone)
        end = min(now, week_end)
        previous_start = start - timedelta(days=7)
        previous_end = previous_start + (end - start)
        embed, file, _, _ = await weekly_report_payload(
            self.repo, interaction.guild_id, start, end, previous_start, previous_end
        )
        await interaction.followup.send(embed=embed, file=file, ephemeral=True)

    @stats.command(name="прошлая_неделя", description="Полный отчёт за завершённую прошлую неделю")
    async def last_week(self, interaction: discord.Interaction) -> None:
        settings = await require_staff(self.repo, interaction)
        if settings is None:
            return
        await interaction.response.defer(ephemeral=True)
        now = utcnow()
        start, end = previous_complete_week(now, settings.timezone)
        previous_start, previous_end = start - timedelta(days=7), end - timedelta(days=7)
        embed, file, metrics, explanations = await weekly_report_payload(
            self.repo, interaction.guild_id, start, end, previous_start, previous_end
        )
        await self.repo.save_weekly_report(
            guild_id=interaction.guild_id,
            week_start=start,
            week_end=end,
            pulse_score=metrics["score"],
            metrics=metrics,
            explanations=explanations,
        )
        await interaction.followup.send(embed=embed, file=file, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StatsCog(bot))
