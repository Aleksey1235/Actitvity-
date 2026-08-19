from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..helpers import require_leader, require_staff
from ..presenters import build_public_dashboard_embed, build_staff_dashboard_embed
from ..repository import Repository
from ..ui import PublicDashboardView, StaffDashboardView


def _family_can_view(channel: discord.TextChannel, guild: discord.Guild, family_role_id: int | None) -> bool:
    # Protect against the two common leaks: @everyone and the normal family role.
    if channel.permissions_for(guild.default_role).view_channel:
        return True
    if family_role_id:
        role = guild.get_role(family_role_id)
        if role and channel.permissions_for(role).view_channel:
            return True
    return False


class PanelCog(commands.Cog):
    panel = app_commands.Group(name="панель", description="Постоянные панели Family Activity")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.repo: Repository = getattr(bot, "repo")

    async def _disable_old(self, interaction: discord.Interaction, channel_id: int | None, message_id: int | None) -> None:
        if not channel_id or not message_id or not interaction.guild:
            return
        channel = interaction.guild.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            try:
                msg = await channel.fetch_message(message_id)
                await msg.edit(view=None)
            except discord.HTTPException:
                pass

    @panel.command(name="участники", description="Создать безопасную панель для обычных участников")
    async def public_panel(self, interaction: discord.Interaction) -> None:
        settings = await require_leader(self.repo, interaction)
        if settings is None:
            return
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Панель можно создать только в текстовом канале.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self._disable_old(
            interaction,
            settings.public_dashboard_channel_id,
            settings.public_dashboard_message_id,
        )
        embed = await build_public_dashboard_embed(self.repo, interaction.guild_id)
        message = await interaction.channel.send(embed=embed, view=PublicDashboardView())
        await self.repo.update_guild_settings(
            interaction.guild_id,
            public_dashboard_channel_id=interaction.channel.id,
            public_dashboard_message_id=message.id,
        )
        await self.repo.audit(
            interaction.guild_id,
            interaction.user.id,
            "dashboard.public_created",
            "message",
            message.id,
            {"channel_id": interaction.channel.id},
        )
        await interaction.followup.send(
            f"✅ Панель участников создана: {message.jump_url}\n"
            "На ней нет Family Pulse, состава и внутренней аналитики; ответы кнопок приватные.",
            ephemeral=True,
        )

    @panel.command(name="руководство", description="Создать закрытую аналитическую панель руководства")
    async def staff_panel(self, interaction: discord.Interaction) -> None:
        settings = await require_leader(self.repo, interaction)
        if settings is None:
            return
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Панель можно создать только в текстовом канале.", ephemeral=True)
            return
        if _family_can_view(interaction.channel, interaction.guild, settings.family_role_id):
            await interaction.response.send_message(
                "❌ Этот канал виден обычным участникам/@everyone. Панель руководства содержит внутреннюю аналитику.\n"
                "Сначала закрой просмотр канала для @everyone и роли семьи, затем повтори команду.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        await self._disable_old(
            interaction,
            settings.staff_dashboard_channel_id,
            settings.staff_dashboard_message_id,
        )
        embed = await build_staff_dashboard_embed(self.repo, interaction.guild_id)
        message = await interaction.channel.send(embed=embed, view=StaffDashboardView())
        await self.repo.update_guild_settings(
            interaction.guild_id,
            staff_dashboard_channel_id=interaction.channel.id,
            staff_dashboard_message_id=message.id,
        )
        await self.repo.audit(
            interaction.guild_id,
            interaction.user.id,
            "dashboard.staff_created",
            "message",
            message.id,
            {"channel_id": interaction.channel.id},
        )
        await interaction.followup.send(
            f"✅ Закрытая панель руководства создана: {message.jump_url}", ephemeral=True
        )

    @panel.command(name="обновить", description="Принудительно обновить обе постоянные панели")
    async def refresh(self, interaction: discord.Interaction) -> None:
        settings = await require_staff(self.repo, interaction)
        if settings is None:
            return
        if not interaction.guild:
            return
        await interaction.response.defer(ephemeral=True)
        updated: list[str] = []

        if settings.public_dashboard_channel_id and settings.public_dashboard_message_id:
            channel = interaction.guild.get_channel(settings.public_dashboard_channel_id)
            if isinstance(channel, discord.TextChannel):
                try:
                    message = await channel.fetch_message(settings.public_dashboard_message_id)
                    embed = await build_public_dashboard_embed(self.repo, interaction.guild_id)
                    await message.edit(embed=embed, view=PublicDashboardView())
                    updated.append("участники")
                except discord.HTTPException:
                    pass

        if settings.staff_dashboard_channel_id and settings.staff_dashboard_message_id:
            channel = interaction.guild.get_channel(settings.staff_dashboard_channel_id)
            if isinstance(channel, discord.TextChannel):
                try:
                    message = await channel.fetch_message(settings.staff_dashboard_message_id)
                    embed = await build_staff_dashboard_embed(self.repo, interaction.guild_id)
                    await message.edit(embed=embed, view=StaffDashboardView())
                    updated.append("руководство")
                except discord.HTTPException:
                    pass

        if updated:
            await interaction.followup.send("✅ Обновлены панели: **" + ", ".join(updated) + "**.", ephemeral=True)
        else:
            await interaction.followup.send(
                "Панели ещё не созданы. Используй `/панель участники` и `/панель руководство`.",
                ephemeral=True,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PanelCog(bot))
