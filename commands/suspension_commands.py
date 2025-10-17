import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from config import DB_PATH, ADMIN_ROLE_ID

class SuspensionCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Check if user has admin permissions for admin commands"""
        # Public commands
        if interaction.command.name in ['suspensionlist']:
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

    @app_commands.command(name="addsuspension", description="[ADMIN] Add a suspension to a player")
    @app_commands.describe(
        player_name="Player name",
        suspension_reason="Reason for suspension",
        games_missed="Number of games to miss"
    )
    async def add_suspension(
        self,
        interaction: discord.Interaction,
        player_name: str,
        suspension_reason: str,
        games_missed: int
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

            # Check if player is already suspended
            cursor = await db.execute(
                """SELECT suspension_id FROM suspensions
                   WHERE player_id = ? AND status = 'suspended'""",
                (player_id,)
            )
            existing = await cursor.fetchone()

            if existing:
                await interaction.response.send_message(
                    f"❌ **{p_name}** is already suspended! Use `/editsuspension` to modify it.",
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
            return_round = current_round + games_missed

            # Add suspension
            await db.execute(
                """INSERT INTO suspensions (player_id, suspension_reason, suspension_round, games_missed, return_round, status)
                   VALUES (?, ?, ?, ?, ?, 'suspended')""",
                (player_id, suspension_reason, current_round, games_missed, return_round)
            )
            await db.commit()

            # Get total rounds to check if season-ending
            cursor = await db.execute(
                "SELECT total_rounds FROM seasons WHERE status = 'active' LIMIT 1"
            )
            season_info = await cursor.fetchone()
            total_rounds = season_info[0] if season_info else 0

            # Format expected return
            if return_round > total_rounds:
                expected_return = "SEASON"
            else:
                expected_return = f"Round {return_round}"

            # Send response
            await interaction.response.send_message(
                f"🚫 **{p_name}** has been suspended!\n"
                f"• Reason: {suspension_reason}\n"
                f"• Games missed: {games_missed} game{'s' if games_missed != 1 else ''}\n"
                f"• Expected return: {expected_return}",
                ephemeral=True
            )

            # Notify team channel
            if team_id:
                team_display = team_name if team_name else "Free Agent"
                game_text = "game" if games_missed == 1 else "games"

                # Format expected return for channel notification
                if return_round > total_rounds:
                    return_text = "SEASON"
                else:
                    return_text = f"Round {return_round}"

                await self.notify_team_channel(
                    team_id,
                    f"🚫 **Suspension Update**\n"
                    f"**{p_name}** has been suspended for **{suspension_reason}** and will miss **{games_missed} {game_text}**.\n"
                    f"Expected return: {return_text}"
                )

    @app_commands.command(name="suspensionlist", description="View current suspensions")
    @app_commands.describe(team_name="Team name (leave empty for your team, use 'all' for all teams)")
    async def suspension_list(self, interaction: discord.Interaction, team_name: str = None):
        async with aiosqlite.connect(DB_PATH) as db:
            # Get current round and total rounds
            cursor = await db.execute(
                "SELECT current_round, total_rounds FROM seasons WHERE status = 'active' LIMIT 1"
            )
            season_info = await cursor.fetchone()
            current_round = season_info[0] if season_info else 0
            total_rounds = season_info[1] if season_info else 0

            # Determine which team to show
            filter_team_id = None
            title_suffix = ""

            if team_name and team_name.lower() == 'all':
                # Show all teams - no suffix
                filter_team_id = None
                title_suffix = ""
            elif team_name:
                # Show specific team
                cursor = await db.execute(
                    "SELECT team_id, team_name FROM teams WHERE team_name LIKE ?",
                    (f"%{team_name}%",)
                )
                team = await cursor.fetchone()
                if not team:
                    await interaction.response.send_message(
                        f"❌ No team found matching '{team_name}'",
                        ephemeral=True
                    )
                    return
                filter_team_id = team[0]
                title_suffix = f" - {team[1]}"
            else:
                # Default to user's team
                cursor = await db.execute(
                    "SELECT team_name, role_id FROM teams WHERE role_id IS NOT NULL"
                )
                teams = await cursor.fetchall()

                user_team_id = None
                user_team_name = None
                for t_name, role_id in teams:
                    role = interaction.guild.get_role(int(role_id))
                    if role and role in interaction.user.roles:
                        cursor = await db.execute(
                            "SELECT team_id FROM teams WHERE team_name = ?",
                            (t_name,)
                        )
                        result = await cursor.fetchone()
                        if result:
                            user_team_id = result[0]
                            user_team_name = t_name
                            break

                if user_team_id:
                    filter_team_id = user_team_id
                    title_suffix = f" - {user_team_name}"
                else:
                    # User has no team, show all
                    filter_team_id = None
                    title_suffix = ""

            # Get active suspensions (filtered by team if specified)
            if filter_team_id:
                cursor = await db.execute(
                    """SELECT p.name, s.suspension_reason, s.return_round, t.team_name, t.emoji_id
                       FROM suspensions s
                       JOIN players p ON s.player_id = p.player_id
                       LEFT JOIN teams t ON p.team_id = t.team_id
                       WHERE s.status = 'suspended' AND p.team_id = ?
                       ORDER BY s.return_round ASC, p.name ASC""",
                    (filter_team_id,)
                )
            else:
                cursor = await db.execute(
                    """SELECT p.name, s.suspension_reason, s.return_round, t.team_name, t.emoji_id
                       FROM suspensions s
                       JOIN players p ON s.player_id = p.player_id
                       LEFT JOIN teams t ON p.team_id = t.team_id
                       WHERE s.status = 'suspended'
                       ORDER BY s.return_round ASC, p.name ASC"""
                )
            suspensions = await cursor.fetchall()

            if not suspensions:
                await interaction.response.send_message("No active suspensions!")
                return

            # Build suspension list
            suspension_list = []
            for name, suspension_reason, return_round, team_name, emoji_id in suspensions:
                # Calculate games remaining
                games_left = return_round - current_round

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

                if games_left <= 0:
                    status = "✅ Ready to return"
                else:
                    game_text = "game" if games_left == 1 else "games"
                    # Check if suspension extends beyond season
                    season_indicator = " (SEASON)" if return_round > total_rounds else ""
                    status = f"- {games_left} {game_text}{season_indicator}"

                suspension_list.append(
                    f"{team_display}**{name}** - {suspension_reason} {status}"
                )

            embed = discord.Embed(
                title=f"Suspension List - Round {current_round}{title_suffix}",
                description="\n".join(suspension_list),
                color=discord.Color.orange()
            )

            await interaction.response.send_message(embed=embed)

    @app_commands.command(name="editsuspension", description="[ADMIN] Edit a player's suspension")
    @app_commands.describe(
        player_name="Player name",
        new_suspension_reason="New suspension reason (optional)",
        new_games_missed="New games missed (optional)"
    )
    async def edit_suspension(
        self,
        interaction: discord.Interaction,
        player_name: str,
        new_suspension_reason: str = None,
        new_games_missed: int = None
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

            # Find active suspension
            cursor = await db.execute(
                """SELECT suspension_id, suspension_reason, suspension_round, games_missed
                   FROM suspensions
                   WHERE player_id = ? AND status = 'suspended'""",
                (player_id,)
            )
            suspension = await cursor.fetchone()

            if not suspension:
                await interaction.response.send_message(
                    f"❌ **{p_name}** has no active suspension!",
                    ephemeral=True
                )
                return

            suspension_id, old_suspension_reason, suspension_round, old_games_missed = suspension

            # Update fields
            updates = []
            values = []
            changes = []

            if new_suspension_reason:
                updates.append("suspension_reason = ?")
                values.append(new_suspension_reason)
                changes.append(f"Reason: {old_suspension_reason} → {new_suspension_reason}")

            if new_games_missed:
                new_return_round = suspension_round + new_games_missed
                updates.append("games_missed = ?, return_round = ?")
                values.extend([new_games_missed, new_return_round])
                changes.append(f"Games missed: {old_games_missed} → {new_games_missed} games")

            if not updates:
                await interaction.response.send_message(
                    "❌ No updates specified!",
                    ephemeral=True
                )
                return

            # Perform update
            values.append(suspension_id)
            query = f"UPDATE suspensions SET {', '.join(updates)} WHERE suspension_id = ?"

            await db.execute(query, values)
            await db.commit()

            response = f"✅ Updated suspension for **{p_name}**\n\n"
            response += "\n".join(changes)

            await interaction.response.send_message(response, ephemeral=True)

    @app_commands.command(name="removesuspension", description="[ADMIN] Remove a player's suspension")
    @app_commands.describe(player_name="Player name")
    async def remove_suspension(self, interaction: discord.Interaction, player_name: str):
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

            # Find and remove active suspension
            cursor = await db.execute(
                """SELECT suspension_id FROM suspensions
                   WHERE player_id = ? AND status = 'suspended'""",
                (player_id,)
            )
            suspension = await cursor.fetchone()

            if not suspension:
                await interaction.response.send_message(
                    f"❌ **{p_name}** has no active suspension!",
                    ephemeral=True
                )
                return

            # Mark as completed
            await db.execute(
                "UPDATE suspensions SET status = 'completed' WHERE suspension_id = ?",
                (suspension[0],)
            )
            await db.commit()

            await interaction.response.send_message(
                f"✅ **{p_name}**'s suspension has been lifted!",
                ephemeral=True
            )

            # Notify team channel
            if team_id:
                team_display = team_name if team_name else "Free Agent"
                await self.notify_team_channel(
                    team_id,
                    f"✅ **Suspension Update**\n"
                    f"**{p_name}**'s suspension has been lifted and they are available for selection!"
                )


async def setup(bot):
    await bot.add_cog(SuspensionCommands(bot))
