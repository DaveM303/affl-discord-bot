import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from config import DB_PATH, ADMIN_ROLE_ID
from commands.season_commands import get_round_name

class InjuryCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def player_name_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for player names with format: Team Name (POS, age, OVR)"""
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                """SELECT p.player_id, p.name, p.position, p.age, p.overall_rating, t.team_name
                   FROM players p
                   LEFT JOIN teams t ON p.team_id = t.team_id
                   ORDER BY p.name"""
            )
            players = await cursor.fetchall()

        # Filter players based on what the user has typed
        choices = []
        for player_id, name, position, age, rating, team_name in players:
            # Check if current input matches player name
            if current.lower() in name.lower():
                # Format: Team Name (POS, age yo, OVR)
                team_prefix = team_name if team_name else "Free Agent"
                display_name = f"{name} ({team_prefix}, {position}, {age}yo, {rating} OVR)"

                # Value is player_id so we can query by ID later
                choices.append(app_commands.Choice(name=display_name, value=str(player_id)))

        # Return up to 25 choices (Discord limit)
        return choices[:25]

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
    @app_commands.autocomplete(player_name=player_name_autocomplete)
    async def add_injury(
        self,
        interaction: discord.Interaction,
        player_name: str,
        injury_type: str,
        recovery_rounds: int
    ):
        async with aiosqlite.connect(DB_PATH) as db:
            # Get player by ID (player_name is actually player_id from autocomplete)
            try:
                player_id = int(player_name)
            except ValueError:
                await interaction.response.send_message(
                    f"❌ Invalid player selection. Please use the autocomplete suggestions.",
                    ephemeral=True
                )
                return

            cursor = await db.execute(
                """SELECT p.player_id, p.name, p.team_id, t.team_name
                   FROM players p
                   LEFT JOIN teams t ON p.team_id = t.team_id
                   WHERE p.player_id = ?""",
                (player_id,)
            )
            player = await cursor.fetchone()

            if not player:
                await interaction.response.send_message(
                    f"❌ Player not found. Please select from the autocomplete suggestions.",
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

            # Get total rounds and regular_rounds to check if season-ending
            cursor = await db.execute(
                "SELECT total_rounds, regular_rounds FROM seasons WHERE status = 'active' LIMIT 1"
            )
            season_info = await cursor.fetchone()
            total_rounds = season_info[0] if season_info else 0
            regular_rounds = season_info[1] if season_info else 24

            # Format expected return
            if return_round > total_rounds:
                expected_return = "SEASON"
            else:
                expected_return = get_round_name(return_round, regular_rounds)

            # Send response
            await interaction.response.send_message(
                f"🚑 **{p_name}** has been injured!\n"
                f"• Injury: {injury_type}\n"
                f"• Recovery: {recovery_rounds} round{'s' if recovery_rounds != 1 else ''}\n"
                f"• Expected return: {expected_return}",
                ephemeral=True
            )

            # Notify team channel
            if team_id:
                team_display = team_name if team_name else "Free Agent"
                week_text = "week" if recovery_rounds == 1 else "weeks"

                # Use "an" for vowels, "a" for consonants
                article = "an" if injury_type[0].lower() in 'aeiou' else "a"

                # Format expected return for channel notification
                if return_round > total_rounds:
                    return_text = "SEASON"
                else:
                    return_text = get_round_name(return_round, regular_rounds)

                await self.notify_team_channel(
                    team_id,
                    f"🚑 **Injury Update**\n"
                    f"**{p_name}** has suffered {article} **{injury_type} injury** and will miss **{recovery_rounds} {week_text}**.\n"
                    f"Expected return: {return_text}"
                )

    @app_commands.command(name="injurylist", description="View current injuries and suspensions")
    @app_commands.describe(team_name="Team name (leave empty for your team, use 'all' for all teams)")
    async def injury_list(self, interaction: discord.Interaction, team_name: str = None):
        async with aiosqlite.connect(DB_PATH) as db:
            # Get current round, total rounds, and regular_rounds
            cursor = await db.execute(
                "SELECT current_round, total_rounds, regular_rounds FROM seasons WHERE status = 'active' LIMIT 1"
            )
            season_info = await cursor.fetchone()
            current_round = season_info[0] if season_info else 0
            total_rounds = season_info[1] if season_info else 0
            regular_rounds = season_info[2] if season_info else 24

            # Determine which team to show
            filter_team_id = None
            title_suffix = ""

            if team_name and team_name.lower() == 'all':
                # Show all teams - no suffix
                filter_team_id = None
                title_suffix = ""
            elif team_name:
                # Show specific team (exact match due to autocomplete)
                cursor = await db.execute(
                    "SELECT team_id, team_name FROM teams WHERE team_name = ?",
                    (team_name,)
                )
                team = await cursor.fetchone()
                if not team:
                    await interaction.response.send_message(
                        f"❌ Team '{team_name}' not found. Please select from the autocomplete suggestions.",
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

            # Get active injuries (filtered by team if specified)
            if filter_team_id:
                cursor = await db.execute(
                    """SELECT p.name, i.injury_type, i.return_round, t.team_name, t.emoji_id
                       FROM injuries i
                       JOIN players p ON i.player_id = p.player_id
                       LEFT JOIN teams t ON p.team_id = t.team_id
                       WHERE i.status = 'injured' AND p.team_id = ?
                       ORDER BY i.return_round ASC, p.name ASC""",
                    (filter_team_id,)
                )
            else:
                cursor = await db.execute(
                    """SELECT p.name, i.injury_type, i.return_round, t.team_name, t.emoji_id
                       FROM injuries i
                       JOIN players p ON i.player_id = p.player_id
                       LEFT JOIN teams t ON p.team_id = t.team_id
                       WHERE i.status = 'injured'
                       ORDER BY i.return_round ASC, p.name ASC"""
                )
            injuries = await cursor.fetchall()

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

            if not injuries and not suspensions:
                await interaction.response.send_message("No active injuries or suspensions!")
                return

            # Build combined list
            combined_list = []

            # Add injuries
            if injuries:
                combined_list.append("**🚑 Injuries:**")
                for name, injury_type, return_round, team_name, emoji_id in injuries:
                    # Calculate weeks remaining
                    weeks_left = return_round - current_round

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

                    if weeks_left <= 0:
                        status = "✅ Ready to return"
                    else:
                        week_text = "week" if weeks_left == 1 else "weeks"
                        # Check if injury extends beyond season
                        season_indicator = " (SEASON)" if return_round > total_rounds else ""
                        status = f"- {weeks_left} {week_text}{season_indicator}"

                    combined_list.append(
                        f"{team_display}**{name}** - {injury_type} {status}"
                    )

            # Add suspensions
            if suspensions:
                if injuries:
                    combined_list.append("")  # Empty line separator
                combined_list.append("**🚫 Suspensions:**")
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

                    combined_list.append(
                        f"{team_display}**{name}** - {suspension_reason} {status}"
                    )

            # Get the round name
            round_display = get_round_name(current_round, regular_rounds) if current_round > 0 else "Offseason"

            embed = discord.Embed(
                title=f"Injury & Suspension List - {round_display}{title_suffix}",
                description="\n".join(combined_list),
                color=discord.Color.red()
            )

            await interaction.response.send_message(embed=embed)

    @app_commands.command(name="editinjury", description="[ADMIN] Edit a player's injury")
    @app_commands.describe(
        player_name="Player name",
        new_injury_type="New injury type (optional)",
        new_recovery_rounds="New recovery rounds (optional)"
    )
    @app_commands.autocomplete(player_name=player_name_autocomplete)
    async def edit_injury(
        self,
        interaction: discord.Interaction,
        player_name: str,
        new_injury_type: str = None,
        new_recovery_rounds: int = None
    ):
        async with aiosqlite.connect(DB_PATH) as db:
            # Get player by ID (player_name is actually player_id from autocomplete)
            try:
                player_id = int(player_name)
            except ValueError:
                await interaction.response.send_message(
                    f"❌ Invalid player selection. Please use the autocomplete suggestions.",
                    ephemeral=True
                )
                return

            cursor = await db.execute(
                """SELECT p.player_id, p.name
                   FROM players p
                   WHERE p.player_id = ?""",
                (player_id,)
            )
            player = await cursor.fetchone()

            if not player:
                await interaction.response.send_message(
                    f"❌ Player not found. Please select from the autocomplete suggestions.",
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

            await interaction.response.send_message(response, ephemeral=True)

    @app_commands.command(name="removeinjury", description="[ADMIN] Remove a player's injury")
    @app_commands.describe(player_name="Player name")
    @app_commands.autocomplete(player_name=player_name_autocomplete)
    async def remove_injury(self, interaction: discord.Interaction, player_name: str):
        async with aiosqlite.connect(DB_PATH) as db:
            # Get player by ID (player_name is actually player_id from autocomplete)
            try:
                player_id = int(player_name)
            except ValueError:
                await interaction.response.send_message(
                    f"❌ Invalid player selection. Please use the autocomplete suggestions.",
                    ephemeral=True
                )
                return

            cursor = await db.execute(
                """SELECT p.player_id, p.name, p.team_id, t.team_name
                   FROM players p
                   LEFT JOIN teams t ON p.team_id = t.team_id
                   WHERE p.player_id = ?""",
                (player_id,)
            )
            player = await cursor.fetchone()

            if not player:
                await interaction.response.send_message(
                    f"❌ Player not found. Please select from the autocomplete suggestions.",
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
                f"✅ **{p_name}** has recovered from injury!",
                ephemeral=True
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
