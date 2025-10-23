import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
import json
from config import DB_PATH
from commands.season_commands import get_round_name

# AFL lineup structure with 22 positions + 1 sub
AFL_POSITIONS = [
    # Back 6
    "LBP", "FB", "RBP",
    "LHB", "CHB", "RHB",
    # Mid 3
    "LW", "C", "RW",
    # Forward 6
    "LHF", "CHF", "RHF",
    "LFP", "FF", "RFP",
    # Followers 3
    "R", "RR", "RO",
    # Interchange (4)
    "INT1", "INT2", "INT3", "INT4",
    # Sub (1)
    "SUB"
]

class LineupCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def get_user_team(self, user_id: int, guild) -> tuple:
        """Get the team for a Discord user. Returns (team_id, team_name) or (None, None)"""
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT team_id, team_name, role_id FROM teams WHERE role_id IS NOT NULL")
            teams = await cursor.fetchall()
            
            for team_id, team_name, role_id in teams:
                role = guild.get_role(int(role_id))
                if role:
                    member = guild.get_member(user_id)
                    if member and role in member.roles:
                        return team_id, team_name
            
            return None, None

    async def is_admin(self, interaction: discord.Interaction) -> bool:
        """Check if user is admin (owner or has admin role/permissions)"""
        from config import ADMIN_ROLE_ID

        if interaction.guild.owner_id == interaction.user.id:
            return True

        if ADMIN_ROLE_ID:
            admin_role_id = int(ADMIN_ROLE_ID) if isinstance(ADMIN_ROLE_ID, str) else ADMIN_ROLE_ID
            if any(role.id == admin_role_id for role in interaction.user.roles):
                return True

        try:
            if interaction.user.guild_permissions.administrator:
                return True
        except:
            pass

        return False

    async def team_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for team names"""
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT team_name FROM teams ORDER BY team_name")
            teams = await cursor.fetchall()

        # Filter teams based on what the user has typed
        choices = []
        for (team_name,) in teams:
            if current.lower() in team_name.lower():
                choices.append(app_commands.Choice(name=team_name, value=team_name))

        # Return up to 25 choices (Discord limit)
        return choices[:25]

    @app_commands.command(name="teamlineup", description="Open the lineup management menu")
    @app_commands.describe(team_name="[ADMIN ONLY] Team name to manage lineup for")
    @app_commands.autocomplete(team_name=team_autocomplete)
    async def team_lineup(self, interaction: discord.Interaction, team_name: str = None):
        # If team_name specified, check if user is admin
        if team_name:
            if not await self.is_admin(interaction):
                await interaction.response.send_message(
                    "❌ Only admins can manage other teams' lineups!",
                    ephemeral=True
                )
                return

            # Look up specified team (exact match due to autocomplete)
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "SELECT team_id, team_name FROM teams WHERE team_name = ?",
                    (team_name,)
                )
                result = await cursor.fetchone()
                if not result:
                    await interaction.response.send_message(
                        f"❌ Team '{team_name}' not found. Please select from the autocomplete suggestions.",
                        ephemeral=True
                    )
                    return
                team_id, team_name = result
        else:
            # Get user's team
            team_id, team_name = await self.get_user_team(interaction.user.id, interaction.guild)

            if not team_id:
                await interaction.response.send_message(
                    "❌ You don't manage a team!",
                    ephemeral=True
                )
                return

        # Get team data
        async with aiosqlite.connect(DB_PATH) as db:
            # Get current lineup
            cursor = await db.execute(
                """SELECT l.position_name, p.name, p.position, p.overall_rating, p.player_id
                   FROM lineups l
                   JOIN players p ON l.player_id = p.player_id
                   WHERE l.team_id = ?
                   ORDER BY l.slot_number""",
                (team_id,)
            )
            lineup_data = await cursor.fetchall()

            # Get roster
            cursor = await db.execute(
                """SELECT player_id, name, position, overall_rating
                   FROM players
                   WHERE team_id = ?
                   ORDER BY overall_rating DESC""",
                (team_id,)
            )
            roster = await cursor.fetchall()

            # Get team emoji
            cursor = await db.execute(
                "SELECT emoji_id FROM teams WHERE team_id = ?",
                (team_id,)
            )
            result = await cursor.fetchone()
            emoji_id = result[0] if result else None

            # Check if starting lineup exists
            cursor = await db.execute(
                "SELECT 1 FROM starting_lineups WHERE team_id = ?",
                (team_id,)
            )
            has_starting_lineup = await cursor.fetchone() is not None

        # Build lineup dict
        lineup = {}
        for pos_name, name, pos, rating, player_id in lineup_data:
            lineup[pos_name] = {'name': name, 'pos': pos, 'rating': rating, 'player_id': player_id}

        # Create menu view
        view = TeamLineupMenu(team_id, team_name, lineup, roster, self.bot, emoji_id, has_starting_lineup)
        embed = await view.create_menu_embed()

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="setlineup", description="Set your team's 22-player lineup")
    @app_commands.describe(team_name="[ADMIN ONLY] Team name to manage lineup for")
    @app_commands.autocomplete(team_name=team_autocomplete)
    async def set_lineup(self, interaction: discord.Interaction, team_name: str = None):
        # If team_name specified, check if user is admin
        if team_name:
            if not await self.is_admin(interaction):
                await interaction.response.send_message(
                    "❌ Only admins can manage other team's lineups!",
                    ephemeral=True
                )
                return

            # Look up specified team (exact match due to autocomplete)
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "SELECT team_id, team_name FROM teams WHERE team_name = ?",
                    (team_name,)
                )
                result = await cursor.fetchone()
                if not result:
                    await interaction.response.send_message(
                        f"❌ Team '{team_name}' not found. Please select from the autocomplete suggestions.",
                        ephemeral=True
                    )
                    return
                team_id, team_name = result
        else:
            # Get user's team
            team_id, team_name = await self.get_user_team(interaction.user.id, interaction.guild)

            if not team_id:
                await interaction.response.send_message(
                    "❌ You don't manage a team! Contact an admin to get assigned.",
                    ephemeral=True
                )
                return
        
        await interaction.response.defer(ephemeral=True)
        
        # Get current lineup and available players
        async with aiosqlite.connect(DB_PATH) as db:
            # Get current lineup
            cursor = await db.execute(
                """SELECT l.position_name, p.name, p.position, p.overall_rating
                   FROM lineups l
                   JOIN players p ON l.player_id = p.player_id
                   WHERE l.team_id = ?
                   ORDER BY l.slot_number""",
                (team_id,)
            )
            current_lineup = await cursor.fetchall()
            
            # Get all players on the team
            cursor = await db.execute(
                """SELECT player_id, name, position, overall_rating
                   FROM players WHERE team_id = ?
                   ORDER BY overall_rating DESC""",
                (team_id,)
            )
            roster = await cursor.fetchall()
        
        if len(roster) < 23:
            await interaction.followup.send(
                f"❌ You need at least 23 players on your roster to set a lineup (22 + 1 sub). You currently have {len(roster)} players.",
                ephemeral=True
            )
            return
        
        # Create the lineup view
        view = LineupView(team_id, team_name, current_lineup, roster, self.bot)
        await view.initialize()  # Initialize warnings and player IDs
        embed = view.create_embed()

        await interaction.followup.send(
            embed=embed,
            view=view,
            ephemeral=True
        )

    @app_commands.command(name="viewlineup", description="View your team's current lineup")
    @app_commands.autocomplete(team_name=team_autocomplete)
    async def view_lineup(self, interaction: discord.Interaction, team_name: str = None):
        # If no team specified, get user's team
        if not team_name:
            team_id, team_name = await self.get_user_team(interaction.user.id, interaction.guild)
            if not team_id:
                await interaction.response.send_message(
                    "❌ You don't manage a team! Specify a team name to view their lineup.",
                    ephemeral=True
                )
                return
        else:
            # Look up specified team (exact match due to autocomplete)
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "SELECT team_id FROM teams WHERE team_name = ?",
                    (team_name,)
                )
                result = await cursor.fetchone()
                if not result:
                    await interaction.response.send_message(
                        f"❌ Team '{team_name}' not found. Please select from the autocomplete suggestions.",
                        ephemeral=True
                    )
                    return
                team_id = result[0]
        
        # Get lineup
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                """SELECT l.position_name, p.name, p.position, p.overall_rating
                   FROM lineups l
                   JOIN players p ON l.player_id = p.player_id
                   WHERE l.team_id = ?
                   ORDER BY l.slot_number""",
                (team_id,)
            )
            lineup = await cursor.fetchall()
        
        # Get team emoji
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT emoji_id FROM teams WHERE team_id = ?",
                (team_id,)
            )
            result = await cursor.fetchone()
            emoji_id = result[0] if result else None
        
        # Get emoji
        emoji = ""
        if emoji_id:
            try:
                emoji_obj = interaction.client.get_emoji(int(emoji_id))
                if emoji_obj:
                    emoji = f"{emoji_obj} "
            except:
                pass
        
        # Create embed
        embed = discord.Embed(
            title=f"{emoji}{team_name} Lineup",
            color=discord.Color.blue()
        )
        
        # Group by rows with line names
        rows = [
            ("FB", ["LBP", "FB", "RBP"]),
            ("HB", ["LHB", "CHB", "RHB"]),
            ("C", ["LW", "C", "RW"]),
            ("HF", ["LHF", "CHF", "RHF"]),
            ("FF", ["LFP", "FF", "RFP"]),
            ("Fol", ["R", "RR", "RO"])
        ]
        
        lineup_dict = {pos_name: (name, pos, rating) for pos_name, name, pos, rating in lineup}
        
        # Build field display
        field_text = ""
        for line_name, positions in rows:
            row_text = []
            for pos_name in positions:
                if pos_name in lineup_dict:
                    name, pos, rating = lineup_dict[pos_name]
                    row_text.append(f"{name} ({rating})")
                else:
                    row_text.append("*Empty*")
            field_text += f"**{line_name}:**  {', '.join(row_text)}\n"
        
        # Add spacing before interchange
        field_text += "\n"
        
        # Interchange - all on one line
        int_players = []
        for pos_name in ["INT1", "INT2", "INT3", "INT4"]:
            if pos_name in lineup_dict:
                name, pos, rating = lineup_dict[pos_name]
                int_players.append(f"{name} ({rating})")
            else:
                int_players.append("*Empty*")
        
        field_text += f"**Int:**  {', '.join(int_players)}\n"
        
        # Sub
        if "SUB" in lineup_dict:
            name, pos, rating = lineup_dict["SUB"]
            field_text += f"**Sub:**  {name} ({rating})"
        else:
            field_text += f"**Sub:**  *Empty*"
        
        embed.description = field_text
        embed.set_footer(text=f"{len(lineup)}/23 players selected")
        
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="submitlineup", description="Submit your lineup for the current round")
    @app_commands.describe(team_name="[ADMIN ONLY] Team name to submit lineup for")
    @app_commands.autocomplete(team_name=team_autocomplete)
    async def submit_lineup(self, interaction: discord.Interaction, team_name: str = None):
        # If team_name specified, check if user is admin
        if team_name:
            if not await self.is_admin(interaction):
                await interaction.response.send_message(
                    "❌ Only admins can submit lineups for other teams!",
                    ephemeral=True
                )
                return

            # Look up specified team (exact match due to autocomplete)
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "SELECT team_id, team_name FROM teams WHERE team_name = ?",
                    (team_name,)
                )
                result = await cursor.fetchone()
                if not result:
                    await interaction.response.send_message(
                        f"❌ Team '{team_name}' not found. Please select from the autocomplete suggestions.",
                        ephemeral=True
                    )
                    return
                team_id, team_name = result
        else:
            # Get user's team
            team_id, team_name = await self.get_user_team(interaction.user.id, interaction.guild)

            if not team_id:
                await interaction.response.send_message(
                    "❌ You don't manage a team!",
                    ephemeral=True
                )
                return

        await interaction.response.defer(ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as db:
            # Get current lineup
            cursor = await db.execute(
                """SELECT l.position_name, p.player_id, p.name, p.position, p.overall_rating
                   FROM lineups l
                   JOIN players p ON l.player_id = p.player_id
                   WHERE l.team_id = ?
                   ORDER BY l.slot_number""",
                (team_id,)
            )
            lineup = await cursor.fetchall()

            # Get team emoji
            cursor = await db.execute(
                "SELECT emoji_id FROM teams WHERE team_id = ?",
                (team_id,)
            )
            result = await cursor.fetchone()
            emoji_id = result[0] if result else None

            # Get global lineups channel
            cursor = await db.execute(
                "SELECT setting_value FROM settings WHERE setting_key = ?",
                ("lineups_channel_id",)
            )
            result = await cursor.fetchone()
            lineup_channel_id = result[0] if result else None

            # Get current round and regular_rounds
            cursor = await db.execute(
                "SELECT current_round, regular_rounds FROM seasons WHERE status = 'active' LIMIT 1"
            )
            season_info = await cursor.fetchone()
            current_round = season_info[0] if season_info else 0
            regular_rounds = season_info[1] if season_info else 24

        # Validate lineup
        errors = []

        # Check if lineup has 23 players
        if len(lineup) < 23:
            empty_count = 23 - len(lineup)
            errors.append(f"❌ Lineup incomplete: {empty_count} empty position(s)")

        # Build position map for duplicate checking
        lineup_dict = {}
        for pos_name, player_id, name, pos, rating in lineup:
            lineup_dict[pos_name] = {'player_id': player_id, 'name': name, 'pos': pos, 'rating': rating}

        # Check for duplicates
        player_ids = [p_id for _, p_id, _, _, _ in lineup]
        seen = set()
        duplicates = []
        for pos_name, player_id, name, pos, rating in lineup:
            if player_ids.count(player_id) > 1 and player_id not in seen:
                duplicates.append(name)
                seen.add(player_id)

        if duplicates:
            errors.append(f"❌ Duplicate players: {', '.join(duplicates)}")

        # Check for injured players and suspended players
        if player_ids:
            async with aiosqlite.connect(DB_PATH) as db:
                placeholders = ','.join('?' * len(player_ids))

                # Check injuries
                cursor = await db.execute(
                    f"""SELECT p.name, i.return_round
                       FROM injuries i
                       JOIN players p ON i.player_id = p.player_id
                       WHERE i.player_id IN ({placeholders}) AND i.status = 'injured'""",
                    player_ids
                )
                injuries = await cursor.fetchall()

                injured_players = []
                for name, return_round in injuries:
                    weeks_left = return_round - current_round
                    if weeks_left > 0:
                        injured_players.append(f"{name} ({weeks_left}w)")

                if injured_players:
                    errors.append(f"❌ Injured players: {', '.join(injured_players)}")

                # Check suspensions
                cursor = await db.execute(
                    f"""SELECT p.name, s.return_round
                       FROM suspensions s
                       JOIN players p ON s.player_id = p.player_id
                       WHERE s.player_id IN ({placeholders}) AND s.status = 'suspended'""",
                    player_ids
                )
                suspensions = await cursor.fetchall()

                suspended_players = []
                for name, return_round in suspensions:
                    games_left = return_round - current_round
                    if games_left > 0:
                        suspended_players.append(f"{name} ({games_left}g)")

                if suspended_players:
                    errors.append(f"❌ Suspended players: {', '.join(suspended_players)}")

        # If there are errors, don't submit
        if errors:
            error_msg = "**Cannot submit lineup:**\n\n" + "\n".join(errors)
            error_msg += "\n\n*Fix these issues using `/setlineup` and try again.*"
            await interaction.followup.send(error_msg, ephemeral=True)
            return

        # If no lineup channel set, warn
        if not lineup_channel_id:
            await interaction.followup.send(
                "⚠️ No lineup submission channel has been set!\n"
                "Ask an admin to use `/setlineupschannel` to set it up.",
                ephemeral=True
            )
            return

        # Get the lineup channel
        lineup_channel = self.bot.get_channel(int(lineup_channel_id))
        if not lineup_channel:
            await interaction.followup.send(
                "❌ Lineup channel not found! Ask an admin to update it with `/setlineupschannel`.",
                ephemeral=True
            )
            return

        # Get team emoji
        emoji = ""
        if emoji_id:
            try:
                emoji_obj = self.bot.get_emoji(int(emoji_id))
                if emoji_obj:
                    emoji = f"{emoji_obj} "
            except:
                pass

        # Create lineup embed for submission
        rows = [
            ("FB", ["LBP", "FB", "RBP"]),
            ("HB", ["LHB", "CHB", "RHB"]),
            ("C", ["LW", "C", "RW"]),
            ("HF", ["LHF", "CHF", "RHF"]),
            ("FF", ["LFP", "FF", "RFP"]),
            ("Fol", ["R", "RR", "RO"])
        ]

        # Get the round name
        round_display = get_round_name(current_round, regular_rounds) if current_round > 0 else "Offseason"

        embed = discord.Embed(
            title=f"{emoji}{team_name} - {round_display} Lineup",
            color=discord.Color.green()
        )

        # Build field display
        field_text = ""
        for line_name, positions in rows:
            row_text = []
            for pos_name in positions:
                if pos_name in lineup_dict:
                    p = lineup_dict[pos_name]
                    row_text.append(f"{p['name']} ({p['rating']})")
                else:
                    row_text.append("*Empty*")
            field_text += f"**{line_name}:**  {', '.join(row_text)}\n"

        # Add spacing before interchange
        field_text += "\n"

        # Interchange - all on one line
        int_players = []
        for pos_name in ["INT1", "INT2", "INT3", "INT4"]:
            if pos_name in lineup_dict:
                p = lineup_dict[pos_name]
                int_players.append(f"{p['name']} ({p['rating']})")
            else:
                int_players.append("*Empty*")

        field_text += f"**Int:**  {', '.join(int_players)}\n"

        # Sub
        if "SUB" in lineup_dict:
            p = lineup_dict["SUB"]
            field_text += f"**Sub:**  {p['name']} ({p['rating']})"
        else:
            field_text += f"**Sub:**  *Empty*"

        embed.description = field_text

        # Post to lineup channel
        await lineup_channel.send(embed=embed)

        # Confirm to user
        await interaction.followup.send(
            f"✅ Lineup submitted to {lineup_channel.mention}!",
            ephemeral=True
        )

    @app_commands.command(name="clearlineup", description="Clear your team's lineup")
    async def clear_lineup(self, interaction: discord.Interaction):
        team_id, team_name = await self.get_user_team(interaction.user.id, interaction.guild)

        if not team_id:
            await interaction.response.send_message(
                "❌ You don't manage a team!",
                ephemeral=True
            )
            return

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM lineups WHERE team_id = ?", (team_id,))
            await db.commit()

        await interaction.response.send_message(
            f"✅ Cleared lineup for **{team_name}**",
            ephemeral=True
        )


class TeamLineupMenu(discord.ui.View):
    """Main menu for team lineup management"""
    def __init__(self, team_id, team_name, lineup, roster, bot, emoji_id=None, has_starting_lineup=False):
        super().__init__(timeout=300)
        self.team_id = team_id
        self.team_name = team_name
        self.lineup = lineup
        self.roster = roster
        self.bot = bot
        self.emoji_id = emoji_id
        self.has_starting_lineup = has_starting_lineup

        # Add buttons
        self.add_buttons()

    def add_buttons(self):
        """Add all menu buttons"""
        # Row 1: Primary actions
        edit_btn = discord.ui.Button(label="📝 Edit Lineup", style=discord.ButtonStyle.primary, custom_id="edit_lineup")
        edit_btn.callback = self.edit_lineup_callback
        self.add_item(edit_btn)

        submit_btn = discord.ui.Button(label="📤 Submit Lineup", style=discord.ButtonStyle.success, custom_id="submit_lineup")
        submit_btn.callback = self.submit_lineup_callback
        self.add_item(submit_btn)

        # Row 2: Starting lineup management
        save_btn = discord.ui.Button(label="💾 Save as Starting Lineup", style=discord.ButtonStyle.secondary, custom_id="save_starting")
        save_btn.callback = self.save_starting_lineup_callback
        self.add_item(save_btn)

        revert_btn = discord.ui.Button(
            label="🔄 Revert to Starting Lineup",
            style=discord.ButtonStyle.secondary,
            custom_id="revert_starting",
            disabled=not self.has_starting_lineup
        )
        revert_btn.callback = self.revert_starting_lineup_callback
        self.add_item(revert_btn)

        # Row 3: Clear action
        clear_btn = discord.ui.Button(label="🗑️ Clear Lineup", style=discord.ButtonStyle.danger, custom_id="clear_lineup")
        clear_btn.callback = self.clear_lineup_callback
        self.add_item(clear_btn)

    async def edit_lineup_callback(self, interaction: discord.Interaction):
        """Open the lineup editor"""
        # Create LineupView with current data
        view = LineupView(self.team_id, self.team_name, [], self.roster, self.bot, self.emoji_id)

        # Convert lineup dict to format expected by LineupView
        view.lineup = self.lineup.copy()

        await view.initialize()
        view.add_position_buttons()
        embed = view.create_embed()

        await interaction.response.edit_message(embed=embed, view=view)

    async def submit_lineup_callback(self, interaction: discord.Interaction):
        """Submit the lineup for the current round"""
        await interaction.response.defer(ephemeral=True)

        # Call the submit logic (similar to /submitlineup)
        async with aiosqlite.connect(DB_PATH) as db:
            # Get current round
            cursor = await db.execute(
                "SELECT current_round, regular_rounds FROM seasons WHERE status = 'active' LIMIT 1"
            )
            season_info = await cursor.fetchone()
            if not season_info:
                await interaction.followup.send("❌ No active season!", ephemeral=True)
                return

            current_round = season_info[0]
            regular_rounds = season_info[1]

            # Get lineup from database
            cursor = await db.execute(
                """SELECT l.position_name, p.player_id, p.name, p.position, p.overall_rating
                   FROM lineups l
                   JOIN players p ON l.player_id = p.player_id
                   WHERE l.team_id = ?
                   ORDER BY l.slot_number""",
                (self.team_id,)
            )
            lineup = await cursor.fetchall()

            # Validation (copied from submitlineup command)
            errors = []
            if len(lineup) < 23:
                empty_count = 23 - len(lineup)
                errors.append(f"❌ Lineup incomplete: {empty_count} position(s) empty")

            # Check for duplicates
            player_ids = [p[1] for p in lineup]
            if len(player_ids) != len(set(player_ids)):
                errors.append("❌ Duplicate players in lineup")

            # Check for injured/suspended players
            if player_ids:
                placeholders = ','.join('?' * len(player_ids))

                # Check injuries
                cursor = await db.execute(
                    f"""SELECT p.name, i.return_round
                       FROM injuries i
                       JOIN players p ON i.player_id = p.player_id
                       WHERE i.player_id IN ({placeholders}) AND i.status = 'injured'""",
                    player_ids
                )
                injuries = await cursor.fetchall()
                injured_players = []
                for name, return_round in injuries:
                    weeks_left = return_round - current_round
                    if weeks_left > 0:
                        injured_players.append(f"{name} ({weeks_left}w)")
                if injured_players:
                    errors.append(f"❌ Injured players: {', '.join(injured_players)}")

                # Check suspensions
                cursor = await db.execute(
                    f"""SELECT p.name, s.return_round
                       FROM suspensions s
                       JOIN players p ON s.player_id = p.player_id
                       WHERE s.player_id IN ({placeholders}) AND s.status = 'suspended'""",
                    player_ids
                )
                suspensions = await cursor.fetchall()
                suspended_players = []
                for name, return_round in suspensions:
                    games_left = return_round - current_round
                    if games_left > 0:
                        suspended_players.append(f"{name} ({games_left}g)")
                if suspended_players:
                    errors.append(f"❌ Suspended players: {', '.join(suspended_players)}")

            if errors:
                await interaction.followup.send("\n".join(errors), ephemeral=True)
                return

            # Get lineup channel
            cursor = await db.execute(
                "SELECT setting_value FROM settings WHERE setting_key = ?",
                ("lineups_channel_id",)
            )
            result = await cursor.fetchone()
            if not result or not result[0]:
                await interaction.followup.send("❌ Lineups channel not set! Ask an admin to use `/setlineupschannel`", ephemeral=True)
                return

            lineup_channel = self.bot.get_channel(int(result[0]))
            if not lineup_channel:
                await interaction.followup.send("❌ Lineup channel not found!", ephemeral=True)
                return

        # Build lineup embed
        round_display = get_round_name(current_round, regular_rounds) if current_round > 0 else "Offseason"

        emoji = self.bot.get_emoji(int(self.emoji_id)) if self.emoji_id else ""
        embed = discord.Embed(
            title=f"{emoji}{self.team_name} - {round_display} Lineup",
            color=discord.Color.green()
        )

        # Format lineup
        rows = [
            ("FB", ["LBP", "FB", "RBP"]),
            ("HB", ["LHB", "CHB", "RHB"]),
            ("C", ["LW", "C", "RW"]),
            ("HF", ["LHF", "CHF", "RHF"]),
            ("FF", ["LFP", "FF", "RFP"]),
            ("Fol", ["R", "RR", "RO"])
        ]

        lineup_dict = {pos_name: (name, pos, rating) for pos_name, player_id, name, pos, rating in lineup}
        field_text = ""

        for line_name, positions in rows:
            row_text = []
            for pos_name in positions:
                if pos_name in lineup_dict:
                    p = lineup_dict[pos_name]
                    row_text.append(f"{p[0]} ({p[2]})")
                else:
                    row_text.append("*Empty*")
            field_text += f"**{line_name}:**  {', '.join(row_text)}\n"

        field_text += "\n"

        # Interchange
        int_players = []
        for pos_name in ["INT1", "INT2", "INT3", "INT4"]:
            if pos_name in lineup_dict:
                p = lineup_dict[pos_name]
                int_players.append(f"{p[0]} ({p[2]})")
            else:
                int_players.append("*Empty*")
        field_text += f"**Int:**  {', '.join(int_players)}\n"

        # Sub
        if "SUB" in lineup_dict:
            p = lineup_dict["SUB"]
            field_text += f"**Sub:**  {p[0]} ({p[2]})"
        else:
            field_text += f"**Sub:**  *Empty*"

        embed.description = field_text

        # Post to lineup channel
        await lineup_channel.send(embed=embed)

        # Confirm
        await interaction.followup.send(f"✅ Lineup submitted to {lineup_channel.mention}!", ephemeral=True)

    async def save_starting_lineup_callback(self, interaction: discord.Interaction):
        """Save current lineup as starting lineup"""
        await interaction.response.defer(ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as db:
            # Get current lineup from database
            cursor = await db.execute(
                """SELECT position_name, player_id
                   FROM lineups
                   WHERE team_id = ?""",
                (self.team_id,)
            )
            lineup_data = await cursor.fetchall()

            if not lineup_data:
                await interaction.followup.send("❌ Cannot save empty lineup!", ephemeral=True)
                return

            # Convert to JSON
            lineup_json = json.dumps(dict(lineup_data))

            # Save to starting_lineups table
            await db.execute(
                """INSERT OR REPLACE INTO starting_lineups (team_id, lineup_data, last_updated)
                   VALUES (?, ?, CURRENT_TIMESTAMP)""",
                (self.team_id, lineup_json)
            )
            await db.commit()

        await interaction.followup.send("✅ Starting lineup saved!", ephemeral=True)

        # Update button state
        self.has_starting_lineup = True
        self.clear_items()
        self.add_buttons()

        # Refresh the menu
        embed = await self.create_menu_embed()
        await interaction.edit_original_response(embed=embed, view=self)

    async def revert_starting_lineup_callback(self, interaction: discord.Interaction):
        """Revert to saved starting lineup"""
        await interaction.response.defer(ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as db:
            # Get saved starting lineup
            cursor = await db.execute(
                "SELECT lineup_data FROM starting_lineups WHERE team_id = ?",
                (self.team_id,)
            )
            result = await cursor.fetchone()

            if not result:
                await interaction.followup.send("❌ No starting lineup saved!", ephemeral=True)
                return

            lineup_data = json.loads(result[0])

            # Clear current lineup
            await db.execute("DELETE FROM lineups WHERE team_id = ?", (self.team_id,))

            # Insert saved lineup
            for position_name, player_id in lineup_data.items():
                slot_number = AFL_POSITIONS.index(position_name) + 1
                await db.execute(
                    "INSERT INTO lineups (team_id, player_id, slot_number, position_name) VALUES (?, ?, ?, ?)",
                    (self.team_id, int(player_id), slot_number, position_name)
                )

            await db.commit()

        await interaction.followup.send("✅ Reverted to starting lineup!", ephemeral=True)

        # Refresh the menu with updated lineup
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                """SELECT l.position_name, p.name, p.position, p.overall_rating, p.player_id
                   FROM lineups l
                   JOIN players p ON l.player_id = p.player_id
                   WHERE l.team_id = ?
                   ORDER BY l.slot_number""",
                (self.team_id,)
            )
            lineup = await cursor.fetchall()

        # Update lineup dict
        self.lineup = {}
        for pos_name, name, pos, rating, player_id in lineup:
            self.lineup[pos_name] = {'name': name, 'pos': pos, 'rating': rating, 'player_id': player_id}

        embed = await self.create_menu_embed()
        await interaction.edit_original_response(embed=embed, view=self)

    async def clear_lineup_callback(self, interaction: discord.Interaction):
        """Clear the entire lineup"""
        await interaction.response.defer(ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM lineups WHERE team_id = ?", (self.team_id,))
            await db.commit()

        self.lineup = {}

        await interaction.followup.send("✅ Lineup cleared!", ephemeral=True)

        # Refresh menu
        embed = await self.create_menu_embed()
        await interaction.edit_original_response(embed=embed, view=self)

    async def create_menu_embed(self):
        """Create the menu display embed"""
        emoji = self.bot.get_emoji(int(self.emoji_id)) if self.emoji_id else ""
        embed = discord.Embed(
            title=f"{emoji}{self.team_name} - Lineup Management",
            color=discord.Color.blue()
        )

        # Display current lineup
        rows = [
            ("FB", ["LBP", "FB", "RBP"]),
            ("HB", ["LHB", "CHB", "RHB"]),
            ("C", ["LW", "C", "RW"]),
            ("HF", ["LHF", "CHF", "RHF"]),
            ("FF", ["LFP", "FF", "RFP"]),
            ("Fol", ["R", "RR", "RO"])
        ]

        field_text = ""
        for line_name, positions in rows:
            row_text = []
            for pos_name in positions:
                if pos_name in self.lineup:
                    p = self.lineup[pos_name]
                    row_text.append(f"{p['name']} ({p['rating']})")
                else:
                    row_text.append("*Empty*")
            field_text += f"**{line_name}:**  {', '.join(row_text)}\n"

        field_text += "\n"

        # Interchange
        int_players = []
        for pos_name in ["INT1", "INT2", "INT3", "INT4"]:
            if pos_name in self.lineup:
                p = self.lineup[pos_name]
                int_players.append(f"{p['name']} ({p['rating']})")
            else:
                int_players.append("*Empty*")
        field_text += f"**Int:**  {', '.join(int_players)}\n"

        # Sub
        if "SUB" in self.lineup:
            p = self.lineup["SUB"]
            field_text += f"**Sub:**  {p['name']} ({p['rating']})"
        else:
            field_text += f"**Sub:**  *Empty*"

        embed.description = field_text
        embed.set_footer(text=f"{len(self.lineup)}/23 positions filled")

        return embed


class LineupView(discord.ui.View):
    def __init__(self, team_id, team_name, current_lineup, roster, bot, emoji_id=None):
        super().__init__(timeout=300)  # 5 minute timeout
        self.team_id = team_id
        self.team_name = team_name
        self.roster = roster
        self.bot = bot
        self.emoji_id = emoji_id
        self.selected_position = None  # Track which position is being edited
        self.player_page = 0  # Current page of players in dropdown
        self.warnings = []  # Store lineup warnings

        # Build lineup dict (position_name -> player info)
        self.lineup = {}
        for pos_name, name, pos, rating in current_lineup:
            self.lineup[pos_name] = {'name': name, 'pos': pos, 'rating': rating, 'player_id': None}

        # Add position buttons
        self.current_group = 0  # 0=backs, 1=mids, 2=forwards, 3=interchange
        self.add_position_buttons()

    async def initialize(self):
        """Initialize player IDs and warnings (call this after creating the view)"""
        await self.refresh_lineup_ids()
        await self.update_warnings()
    
    async def refresh_lineup_ids(self):
        """Get player IDs for current lineup players"""
        async with aiosqlite.connect(DB_PATH) as db:
            for pos_name in self.lineup:
                cursor = await db.execute(
                    """SELECT p.player_id FROM lineups l
                       JOIN players p ON l.player_id = p.player_id
                       WHERE l.team_id = ? AND l.position_name = ?""",
                    (self.team_id, pos_name)
                )
                result = await cursor.fetchone()
                if result:
                    self.lineup[pos_name]['player_id'] = result[0]
    
    def add_position_buttons(self):
        """Add buttons for current position group"""
        self.clear_items()
        
        groups = [
            (["LBP", "FB", "RBP", "LHB", "CHB", "RHB"]),
            (["LW", "C", "RW", "R", "RR", "RO"]),
            (["LHF", "CHF", "RHF", "LFP", "FF", "RFP"]),
            (["INT1", "INT2", "INT3", "INT4", "SUB"])
        ]
        
        positions = groups[self.current_group]
        
        # Add position buttons for current group
        for pos_name in positions:
            self.add_item(PositionButton(pos_name, self))
        
        # Add player select dropdown if a position is selected
        if self.selected_position:
            self.add_item(PlayerSelect(self.selected_position, self))
            # Add pagination buttons if needed
            total_players = self.get_sorted_roster_count()
            if total_players > 25:
                if self.player_page > 0:
                    self.add_item(PrevPageButton(self))
                if (self.player_page + 1) * 25 < total_players:
                    self.add_item(NextPageButton(self))
        
        # Add navigation buttons
        if self.current_group < 3:
            self.add_item(NextGroupButton(self))
        if self.current_group > 0:
            self.add_item(PrevGroupButton(self))
        
        # Add clear and save buttons
        if self.selected_position and self.selected_position in self.lineup:
            self.add_item(ClearPositionButton(self))
        self.add_item(SaveLineupButton(self))
    
    def get_sorted_roster(self):
        """Get roster sorted by relevance to selected position"""
        if not self.selected_position:
            return self.roster
        
        # Define position priorities
        defensive_positions = ["LBP", "FB", "RBP", "LHB", "CHB", "RHB"]
        midfield_positions = ["LW", "C", "RW", "R", "RR", "RO"]
        forward_positions = ["LHF", "CHF", "RHF", "LFP", "FF", "RFP"]

        # Define preferred player positions for each field position type
        if self.selected_position in defensive_positions:
            priority_positions = ["GEN DEF", "KEY DEF", "DEF-MID", "RUCK-DEF", "UTILITY", "SWINGMAN"]
        elif self.selected_position in midfield_positions:
            # Prioritize ruck positions for R only
            if self.selected_position == "R":
                priority_positions = ["RUCK", "RUCK-DEF", "RUCK-FWD"]
            else:
                # LW, C, RW, RR, RO prioritize midfield positions
                priority_positions = ["MID", "MID-FWD", "DEF-MID", "UTILITY"]
        elif self.selected_position in forward_positions:
            priority_positions = ["GEN FWD", "KEY FWD", "MID-FWD", "RUCK-FWD", "UTILITY", "SWINGMAN"]
        else:  # Interchange
            return self.roster  # No sorting for interchange
        
        # Get players already in lineup
        used_ids = {p.get('player_id') for p in self.lineup.values() if p.get('player_id')}
        
        # Sort roster: priority positions first, then by rating
        def sort_key(player):
            player_id, name, pos, rating = player
            # Check if position is in priority list
            if pos in priority_positions:
                return (0, -rating)  # Highest priority
            else:
                return (1, -rating)  # Normal priority

        return sorted(self.roster, key=sort_key)
    
    def get_sorted_roster_count(self):
        """Get count of all players (no filtering - all players can be moved)"""
        return len(self.roster)

    def get_duplicate_players(self):
        """Check for duplicate players in lineup - returns list of player names that appear more than once"""
        player_ids = [p.get('player_id') for p in self.lineup.values() if p.get('player_id')]
        duplicates = []
        seen = set()
        for pos_name, player_info in self.lineup.items():
            player_id = player_info.get('player_id')
            if player_id and player_ids.count(player_id) > 1 and player_id not in seen:
                duplicates.append(player_info['name'])
                seen.add(player_id)
        return duplicates

    async def get_injured_players(self):
        """Check for injured players in lineup - returns list of (player_name, weeks_left) tuples"""
        injured = []
        player_ids = [p.get('player_id') for p in self.lineup.values() if p.get('player_id')]

        if not player_ids:
            return injured

        async with aiosqlite.connect(DB_PATH) as db:
            # Get current round
            cursor = await db.execute(
                "SELECT current_round FROM seasons WHERE status = 'active' LIMIT 1"
            )
            season_info = await cursor.fetchone()
            current_round = season_info[0] if season_info else 0

            # Check for injuries
            placeholders = ','.join('?' * len(player_ids))
            cursor = await db.execute(
                f"""SELECT p.name, i.return_round
                   FROM injuries i
                   JOIN players p ON i.player_id = p.player_id
                   WHERE i.player_id IN ({placeholders}) AND i.status = 'injured'""",
                player_ids
            )
            injuries = await cursor.fetchall()

            for name, return_round in injuries:
                weeks_left = return_round - current_round
                if weeks_left > 0:
                    injured.append((name, weeks_left))

        return injured

    async def get_suspended_players(self):
        """Check for suspended players in lineup - returns list of (player_name, games_left) tuples"""
        suspended = []
        player_ids = [p.get('player_id') for p in self.lineup.values() if p.get('player_id')]

        if not player_ids:
            return suspended

        async with aiosqlite.connect(DB_PATH) as db:
            # Get current round
            cursor = await db.execute(
                "SELECT current_round FROM seasons WHERE status = 'active' LIMIT 1"
            )
            season_info = await cursor.fetchone()
            current_round = season_info[0] if season_info else 0

            # Check for suspensions
            placeholders = ','.join('?' * len(player_ids))
            cursor = await db.execute(
                f"""SELECT p.name, s.return_round
                   FROM suspensions s
                   JOIN players p ON s.player_id = p.player_id
                   WHERE s.player_id IN ({placeholders}) AND s.status = 'suspended'""",
                player_ids
            )
            suspensions = await cursor.fetchall()

            for name, return_round in suspensions:
                games_left = return_round - current_round
                if games_left > 0:
                    suspended.append((name, games_left))

        return suspended

    async def update_warnings(self):
        """Update the warnings list based on current lineup"""
        self.warnings = []

        # Check for duplicates
        duplicates = self.get_duplicate_players()
        if duplicates:
            self.warnings.append(f"⚠️ **Duplicate players:** {', '.join(duplicates)}")

        # Check for injured players
        injured = await self.get_injured_players()
        if injured:
            injured_str = ', '.join([f"{name} ({weeks}w)" for name, weeks in injured])
            self.warnings.append(f"🚑 **Injured players:** {injured_str}")

        # Check for suspended players
        suspended = await self.get_suspended_players()
        if suspended:
            suspended_str = ', '.join([f"{name} ({games}g)" for name, games in suspended])
            self.warnings.append(f"🚫 **Suspended players:** {suspended_str}")

    def create_embed(self):
        """Create the lineup display embed"""
        rows = [
            ("FB", ["LBP", "FB", "RBP"]),
            ("HB", ["LHB", "CHB", "RHB"]),
            ("C", ["LW", "C", "RW"]),
            ("HF", ["LHF", "CHF", "RHF"]),
            ("FF", ["LFP", "FF", "RFP"]),
            ("Fol", ["R", "RR", "RO"])
        ]
        
        # Get team emoji
        emoji = ""
        if self.emoji_id:
            try:
                emoji_obj = self.bot.get_emoji(int(self.emoji_id))
                if emoji_obj:
                    emoji = f"{emoji_obj} "
            except:
                pass
        
        title = f"{emoji}{self.team_name} Lineup"
        if self.selected_position:
            title += f" (Editing: {self.selected_position})"
        
        embed = discord.Embed(
            title=title,
            description="Click a position button, then select a player from the dropdown below.",
            color=discord.Color.green()
        )
        
        # Show field positions
        field_text = ""
        for line_name, positions in rows:
            row_text = []
            for pos_name in positions:
                prefix = "→ " if pos_name == self.selected_position else ""
                if pos_name in self.lineup:
                    p = self.lineup[pos_name]
                    row_text.append(f"{prefix}{p['name']} ({p['rating']})")
                else:
                    row_text.append(f"{prefix}*Empty*")
            field_text += f"**{line_name}:**  {', '.join(row_text)}\n"
        
        # Add spacing before interchange
        field_text += "\n"
        
        # Show interchange - all on one line
        int_players = []
        for pos_name in ["INT1", "INT2", "INT3", "INT4"]:
            prefix = "→ " if pos_name == self.selected_position else ""
            if pos_name in self.lineup:
                p = self.lineup[pos_name]
                int_players.append(f"{prefix}{p['name']} ({p['rating']})")
            else:
                int_players.append(f"{prefix}*Empty*")
        
        field_text += f"**Int:**  {', '.join(int_players)}\n"
        
        # Show sub
        prefix = "→ " if "SUB" == self.selected_position else ""
        if "SUB" in self.lineup:
            p = self.lineup["SUB"]
            field_text += f"**Sub:**  {prefix}{p['name']} ({p['rating']})"
        else:
            field_text += f"**Sub:**  {prefix}*Empty*"
        
        embed.add_field(name="\u200b", value=field_text, inline=False)

        # Add warnings if any exist
        if self.warnings:
            embed.add_field(name="\u200b", value="\n".join(self.warnings), inline=False)

        embed.set_footer(text=f"{len(self.lineup)}/23 positions filled • {len(self.roster)} players available")

        return embed


class PositionButton(discord.ui.Button):
    def __init__(self, position_name, parent_view):
        # Show if position is filled and if it's selected
        label = position_name
        if position_name == parent_view.selected_position:
            style = discord.ButtonStyle.primary
        elif position_name in parent_view.lineup:
            style = discord.ButtonStyle.success
        else:
            style = discord.ButtonStyle.secondary
        
        super().__init__(label=label, style=style, custom_id=f"pos_{position_name}")
        self.position_name = position_name
        self.parent_view = parent_view
    
    async def callback(self, interaction: discord.Interaction):
        # Select this position for editing and reset to first page
        self.parent_view.selected_position = self.position_name
        self.parent_view.player_page = 0
        self.parent_view.add_position_buttons()
        
        embed = self.parent_view.create_embed()
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class PrevPageButton(discord.ui.Button):
    def __init__(self, parent_view):
        super().__init__(label="◀ Prev", style=discord.ButtonStyle.secondary, row=4)
        self.parent_view = parent_view
    
    async def callback(self, interaction: discord.Interaction):
        self.parent_view.player_page -= 1
        self.parent_view.add_position_buttons()
        embed = self.parent_view.create_embed()
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class NextPageButton(discord.ui.Button):
    def __init__(self, parent_view):
        super().__init__(label="Next ▶", style=discord.ButtonStyle.secondary, row=4)
        self.parent_view = parent_view
    
    async def callback(self, interaction: discord.Interaction):
        self.parent_view.player_page += 1
        self.parent_view.add_position_buttons()
        embed = self.parent_view.create_embed()
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class ClearPositionButton(discord.ui.Button):
    def __init__(self, parent_view):
        super().__init__(label="✗ Clear", style=discord.ButtonStyle.danger)
        self.parent_view = parent_view
    
    async def callback(self, interaction: discord.Interaction):
        pos_name = self.parent_view.selected_position
        
        # Remove from database
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM lineups WHERE team_id = ? AND position_name = ?",
                (self.parent_view.team_id, pos_name)
            )
            await db.commit()
        
        # Remove from lineup
        if pos_name in self.parent_view.lineup:
            del self.parent_view.lineup[pos_name]

        # Update warnings and refresh view
        await self.parent_view.update_warnings()
        self.parent_view.add_position_buttons()
        embed = self.parent_view.create_embed()

        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class NextGroupButton(discord.ui.Button):
    def __init__(self, parent_view):
        super().__init__(label="Next →", style=discord.ButtonStyle.primary)
        self.parent_view = parent_view
    
    async def callback(self, interaction: discord.Interaction):
        self.parent_view.current_group += 1
        self.parent_view.add_position_buttons()
        
        embed = self.parent_view.create_embed()
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class PrevGroupButton(discord.ui.Button):
    def __init__(self, parent_view):
        super().__init__(label="← Back", style=discord.ButtonStyle.primary)
        self.parent_view = parent_view
    
    async def callback(self, interaction: discord.Interaction):
        self.parent_view.current_group -= 1
        self.parent_view.add_position_buttons()
        
        embed = self.parent_view.create_embed()
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class PlayerSelect(discord.ui.Select):
    def __init__(self, position_name, parent_view):
        self.position_name = position_name
        self.parent_view = parent_view
        
        # Get sorted roster
        sorted_roster = parent_view.get_sorted_roster()

        # Get players already in lineup (for display purposes)
        used_ids = {p.get('player_id') for p in parent_view.lineup.values() if p.get('player_id')}

        # Build options from all players with pagination
        options = []
        start_idx = parent_view.player_page * 25
        end_idx = start_idx + 25
        count = 0
        added = 0

        for player_id, name, pos, rating in sorted_roster:
            # Show all players - they can be moved between positions
            # Check if this player is in the current page
            if count >= start_idx and added < 25:
                # Mark if player is currently in lineup
                label = f"{name} ({rating} OVR)"
                if player_id in used_ids:
                    # Find which position they're in
                    current_pos = None
                    for pos_name, player_info in parent_view.lineup.items():
                        if player_info.get('player_id') == player_id:
                            current_pos = pos_name
                            break
                    if current_pos and current_pos != position_name:
                        label += f" [Currently in {current_pos}]"

                options.append(
                    discord.SelectOption(
                        label=label,
                        description=f"{pos}",
                        value=str(player_id)
                    )
                )
                added += 1

            count += 1
            if added >= 25:
                break
        
        if not options:
            options.append(discord.SelectOption(label="No players available", value="none"))
        
        # Add page indicator to placeholder
        total_available = count
        current_page = parent_view.player_page + 1
        total_pages = (total_available + 24) // 25
        placeholder = f"Select player for {position_name}"
        if total_pages > 1:
            placeholder += f" (Page {current_page}/{total_pages})"
        
        super().__init__(
            placeholder=placeholder,
            options=options,
            custom_id=f"player_select_{position_name}"
        )
    
    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.defer()
            return
        
        player_id = int(self.values[0])
        
        # Get slot number for this position
        slot_number = AFL_POSITIONS.index(self.position_name) + 1
        
        # Update lineup in database
        async with aiosqlite.connect(DB_PATH) as db:
            # Remove player from any existing position
            await db.execute(
                "DELETE FROM lineups WHERE team_id = ? AND player_id = ?",
                (self.parent_view.team_id, player_id)
            )
            
            # Add to new position
            await db.execute(
                "INSERT OR REPLACE INTO lineups (team_id, player_id, slot_number, position_name) VALUES (?, ?, ?, ?)",
                (self.parent_view.team_id, player_id, slot_number, self.position_name)
            )
            await db.commit()
            
            # Get player info
            cursor = await db.execute(
                "SELECT name, position, overall_rating FROM players WHERE player_id = ?",
                (player_id,)
            )
            name, pos, rating = await cursor.fetchone()

        # Remove player from old position in lineup dict (if they were in a different position)
        for pos_name, player_info in list(self.parent_view.lineup.items()):
            if player_info.get('player_id') == player_id and pos_name != self.position_name:
                # Delete the old position entry so it shows as "Empty"
                del self.parent_view.lineup[pos_name]

        # Update parent view with new position
        self.parent_view.lineup[self.position_name] = {
            'name': name,
            'pos': pos,
            'rating': rating,
            'player_id': player_id
        }

        # Reset to first page, update warnings, and refresh view
        self.parent_view.player_page = 0
        await self.parent_view.update_warnings()
        self.parent_view.add_position_buttons()
        embed = self.parent_view.create_embed()

        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class SaveLineupButton(discord.ui.Button):
    def __init__(self, parent_view):
        super().__init__(label="💾 Save", style=discord.ButtonStyle.success)
        self.parent_view = parent_view
    
    async def callback(self, interaction: discord.Interaction):
        filled_slots = len(self.parent_view.lineup)
        
        if filled_slots < 23:
            await interaction.response.send_message(
                f"⚠️ Your lineup has only {filled_slots}/23 players. Continue adding players or save as is?",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"✅ Lineup saved! {filled_slots}/23 players selected.",
                ephemeral=True
            )
        
        # Disable all buttons
        for item in self.parent_view.children:
            item.disabled = True
        
        embed = self.parent_view.create_embed()
        embed.color = discord.Color.green()
        embed.title = f"✅ {self.parent_view.team_name} - Lineup Saved"
        
        await interaction.message.edit(embed=embed, view=self.parent_view)


async def setup(bot):
    await bot.add_cog(LineupCommands(bot))