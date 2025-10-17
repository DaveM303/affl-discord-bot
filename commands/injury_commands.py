import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from config import DB_PATH, ADMIN_ROLE_ID

class InjuryCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Check if user has admin permissions for admin commands"""
        # Public commands
        if interaction.command.name in ['injurylist']:
            return True

        # Admin check for other commands
        if interaction.guild.owner_id == interaction.user.id:
            return True

        if ADMIN_ROLE_ID:
            member = interaction.guild.get_member(interaction.user.id) or interaction.user
            if member:
                admin_role_id = int(ADMIN_ROLE_ID) if isinstance(ADMIN_ROLE_ID, str) else ADMIN_ROLE_ID
                if any(role.id == admin_role_id for role in member.roles):
                    return True

            await interaction.response.send_message(
                "❌ You need the admin role to use this command.",
                ephemeral=True
            )
            return False

        try:
            if interaction.user.guild_permissions.administrator:
                return True
        except:
            pass

        member = interaction.guild.get_member(interaction.user.id)
        if member:
            for role in member.roles:
                if role.permissions.administrator:
                    return True

        await interaction.response.send_message(
            "❌ You need Administrator permissions to use this command.",
            ephemeral=True
        )
        return False

    async def get_current_round(self, db):
        """Get the current round number from active season"""
        cursor = await db.execute(
            "SELECT current_round FROM seasons WHERE status = 'active' LIMIT 1"
        )
        result = await cursor.fetchone()
        return result[0] if result else 0

    async def notify_team_channel(self, team_id, message):
        """Send notification to team channel"""
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT channel_id FROM teams WHERE team_id = ?",
                (team_id,)
            )
            result = await cursor.fetchone()

            if result and result[0]:
                channel = self.bot.get_channel(int(result[0]))
                if channel:
                    await channel.send(message)

    @app_commands.command(name="addinjury", description="[ADMIN] Add an injury to a player")
    @app_commands.describe(
        player_name="Player name",
        injury_type="Type of injury",
        recovery_rounds="Number of rounds until recovery"
    )
    async def add_injury(
        self,
        interaction: discord.Interaction,
        player_name: str,
        injury_type: str,
        recovery_rounds: int
    ):
        async with aiosqlite.connect(DB_PATH) as db:
            # Find the player
            cursor = await db.execute(
                """SELECT p.player_id, p.name, p.team_id, t.team_name
                   FROM players p
                   LEFT JOIN teams t ON p.team_id = t.team_id
                   WHERE p.name LIKE ?""",
                (f"%{player_name}%",)
            )
            player = await cursor.fetchone()

            if not player:
                await interaction.response.send_message(
                    f"❌ No player found matching '{player_name}'",
                    ephemeral=True
                )
                return

            player_id, p_name, team_id, team_name = player

            # Check if player is already injured
            cursor = await db.execute(
                """SELECT injury_id FROM injuries
                   WHERE player_id = ? AND status = 'injured'""",
                (player_id,)
            )
            existing = await cursor.fetchone()

            if existing:
                await interaction.response.send_message(
                    f"❌ **{p_name}** is already injured! Use `/editinjury` to modify it.",
                    ephemeral=True
                )
                return

            # Get current round
            current_round = await self.get_current_round(db)

            if current_round == 0:
                await interaction.response.send_message(
                    "❌ No active season! Start a season first.",
                    ephemeral=True
                )
                return

            # Calculate return round
            return_round = current_round + recovery_rounds

            # Add injury
            await db.execute(
                """INSERT INTO injuries (player_id, injury_type, injury_round, recovery_rounds, return_round, status)
                   VALUES (?, ?, ?, ?, ?, 'injured')""",
                (player_id, injury_type, current_round, recovery_rounds, return_round)
            )
            await db.commit()

            # Send response
            await interaction.response.send_message(
                f"🚑 **{p_name}** has been injured!\n"
                f"• Injury: {injury_type}\n"
                f"• Recovery: {recovery_rounds} round{'s' if recovery_rounds != 1 else ''}\n"
                f"• Expected return: Round {return_round}"
            )

            # Notify team channel
            if team_id:
                team_display = team_name if team_name else "Free Agent"
                round_text = "round" if recovery_rounds == 1 else "rounds"
                await self.notify_team_channel(
                    team_id,
                    f"🚑 **Injury Update**\n"
                    f"**{p_name}** has suffered a **{injury_type} injury** and will miss **{recovery_rounds} {round_text}**.\n"
                    f"Expected return: Round {return_round}"
                )

    @app_commands.command(name="injurylist", description="View all current injuries")
    async def injury_list(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            # Get current round
            current_round = await self.get_current_round(db)

            # Get all active injuries
            cursor = await db.execute(
                """SELECT p.name, i.injury_type, i.return_round, t.team_name, t.emoji_id
                   FROM injuries i
                   JOIN players p ON i.player_id = p.player_id
                   LEFT JOIN teams t ON p.team_id = t.team_id
                   WHERE i.status = 'injured'
                   ORDER BY i.return_round ASC, p.name ASC"""
            )
            injuries = await cursor.fetchall()

            if not injuries:
                await interaction.response.send_message("No active injuries!")
                return

            # Build injury list
            injury_list = []
            for name, injury_type, return_round, team_name, emoji_id in injuries:
                # Calculate rounds remaining
                rounds_left = return_round - current_round

                # Get team emoji
                team_display = ""
                if team_name:
                    try:
                        if emoji_id:
                            emoji = self.bot.get_emoji(int(emoji_id))
                            if emoji:
                                team_display = f"{emoji} "
                    except:
                        pass

                if rounds_left <= 0:
                    status = "✅ Ready to return"
                else:
                    status = f"⏳ {rounds_left} round(s) remaining"

                injury_list.append(
                    f"{team_display}**{name}** - {injury_type} ({status})"
                )

            embed = discord.Embed(
                title=f"Injury List - Round {current_round}",
                description="\n".join(injury_list),
                color=discord.Color.red()
            )
            embed.set_footer(text=f"{len(injuries)} active injury(ies)")

            await interaction.response.send_message(embed=embed)

    @app_commands.command(name="editinjury", description="[ADMIN] Edit a player's injury")
    @app_commands.describe(
        player_name="Player name",
        new_injury_type="New injury type (optional)",
        new_recovery_rounds="New recovery rounds (optional)"
    )
    async def edit_injury(
        self,
        interaction: discord.Interaction,
        player_name: str,
        new_injury_type: str = None,
        new_recovery_rounds: int = None
    ):
        async with aiosqlite.connect(DB_PATH) as db:
            # Find the player
            cursor = await db.execute(
                """SELECT p.player_id, p.name
                   FROM players p
                   WHERE p.name LIKE ?""",
                (f"%{player_name}%",)
            )
            player = await cursor.fetchone()

            if not player:
                await interaction.response.send_message(
                    f"❌ No player found matching '{player_name}'",
                    ephemeral=True
                )
                return

            player_id, p_name = player

            # Find active injury
            cursor = await db.execute(
                """SELECT injury_id, injury_type, injury_round, recovery_rounds
                   FROM injuries
                   WHERE player_id = ? AND status = 'injured'""",
                (player_id,)
            )
            injury = await cursor.fetchone()

            if not injury:
                await interaction.response.send_message(
                    f"❌ **{p_name}** has no active injury!",
                    ephemeral=True
                )
                return

            injury_id, old_injury_type, injury_round, old_recovery = injury

            # Update fields
            updates = []
            values = []
            changes = []

            if new_injury_type:
                updates.append("injury_type = ?")
                values.append(new_injury_type)
                changes.append(f"Injury: {old_injury_type} → {new_injury_type}")

            if new_recovery_rounds:
                new_return_round = injury_round + new_recovery_rounds
                updates.append("recovery_rounds = ?, return_round = ?")
                values.extend([new_recovery_rounds, new_return_round])
                changes.append(f"Recovery: {old_recovery} → {new_recovery_rounds} rounds")

            if not updates:
                await interaction.response.send_message(
                    "❌ No updates specified!",
                    ephemeral=True
                )
                return

            # Perform update
            values.append(injury_id)
            query = f"UPDATE injuries SET {', '.join(updates)} WHERE injury_id = ?"

            await db.execute(query, values)
            await db.commit()

            response = f"✅ Updated injury for **{p_name}**\n\n"
            response += "\n".join(changes)

            await interaction.response.send_message(response)

    @app_commands.command(name="removeinjury", description="[ADMIN] Remove a player's injury")
    @app_commands.describe(player_name="Player name")
    async def remove_injury(self, interaction: discord.Interaction, player_name: str):
        async with aiosqlite.connect(DB_PATH) as db:
            # Find the player
            cursor = await db.execute(
                """SELECT p.player_id, p.name, p.team_id, t.team_name
                   FROM players p
                   LEFT JOIN teams t ON p.team_id = t.team_id
                   WHERE p.name LIKE ?""",
                (f"%{player_name}%",)
            )
            player = await cursor.fetchone()

            if not player:
                await interaction.response.send_message(
                    f"❌ No player found matching '{player_name}'",
                    ephemeral=True
                )
                return

            player_id, p_name, team_id, team_name = player

            # Find and remove active injury
            cursor = await db.execute(
                """SELECT injury_id FROM injuries
                   WHERE player_id = ? AND status = 'injured'""",
                (player_id,)
            )
            injury = await cursor.fetchone()

            if not injury:
                await interaction.response.send_message(
                    f"❌ **{p_name}** has no active injury!",
                    ephemeral=True
                )
                return

            # Mark as recovered
            await db.execute(
                "UPDATE injuries SET status = 'recovered' WHERE injury_id = ?",
                (injury[0],)
            )
            await db.commit()

            await interaction.response.send_message(
                f"✅ **{p_name}** has recovered from injury!"
            )

            # Notify team channel
            if team_id:
                team_display = team_name if team_name else "Free Agent"
                await self.notify_team_channel(
                    team_id,
                    f"✅ **Recovery Update**\n"
                    f"**{p_name}** has recovered from injury and is available for selection!"
                )


async def setup(bot):
    await bot.add_cog(InjuryCommands(bot))
