import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
import pandas as pd
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
import io
import json
from config import DB_PATH, ADMIN_ROLE_ID
from positions import validate_position, get_positions_string
from utils import get_current_year, is_admin_user, get_team_emoji

class AdminCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def player_name_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for player names with format: Name (Team, POS, age, OVR)"""
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
                # Format: Name (Team, POS, age yo, OVR)
                team_prefix = team_name if team_name else "Delisted"
                display_name = f"{name} ({team_prefix}, {position}, {age}yo, {rating} OVR)"

                # Value is player_id so we can query by ID later
                choices.append(app_commands.Choice(name=display_name, value=str(player_id)))

        # Return up to 25 choices (Discord limit)
        return choices[:25]

    async def team_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for team names"""
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT team_name FROM teams ORDER BY team_name"
            )
            teams = await cursor.fetchall()

        # Filter teams based on what the user has typed
        choices = []
        for (team_name,) in teams:
            if current.lower() in team_name.lower():
                choices.append(app_commands.Choice(name=team_name, value=team_name))

        # Add special "delisted" option
        if current.lower() in "delisted":
            choices.insert(0, app_commands.Choice(name="delisted", value="delisted"))

        # Return up to 25 choices (Discord limit)
        return choices[:25]

    async def position_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for positions"""
        from positions import VALID_POSITIONS

        # Filter positions based on what the user has typed
        choices = []
        for position in VALID_POSITIONS:
            if current.lower() in position.lower():
                choices.append(app_commands.Choice(name=position, value=position))

        # Return up to 25 choices (Discord limit)
        return choices[:25]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Check if user has admin permissions based on config"""
        if await is_admin_user(interaction):
            return True

        if ADMIN_ROLE_ID:
            await interaction.response.send_message(
                "❌ You need the admin role to use this command.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "❌ You need Administrator permissions to use this command.",
                ephemeral=True
            )
        return False

    @app_commands.command(name="updateteam", description="[ADMIN] Update a team's settings")
    @app_commands.describe(
        team_name="Name of the team to update",
        new_name="New team name (optional)",
        role="New Discord role (optional)",
        emoji="New team emoji (optional)",
        channel="New team channel (optional)",
        primary_color="Primary team color as a hex code, e.g. #1E5C3A (optional) - fills the team's cell in /ladder",
        secondary_color="Secondary team color as a hex code, e.g. #FFFFFF (optional) - used for the team name text in /ladder"
    )
    @app_commands.autocomplete(team_name=team_autocomplete)
    async def update_team(
        self,
        interaction: discord.Interaction,
        team_name: str,
        new_name: str = None,
        role: discord.Role = None,
        emoji: str = None,
        channel: discord.TextChannel = None,
        primary_color: str = None,
        secondary_color: str = None
    ):
        async with aiosqlite.connect(DB_PATH) as db:
            # Find the team
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

            team_id, current_name = team
            updates = []
            values = []
            changes = []

            # Update team name
            if new_name:
                updates.append("team_name = ?")
                values.append(new_name)
                changes.append(f"Name: {current_name} → {new_name}")

            # Update role
            if role:
                updates.append("role_id = ?")
                values.append(str(role.id))
                changes.append(f"Role: {role.mention}")

            # Update emoji
            if emoji:
                import re
                emoji_match = re.match(r'<a?:(\w+):(\d+)>', emoji)
                if emoji_match:
                    emoji_id = emoji_match.group(2)
                else:
                    emoji_id = emoji

                updates.append("emoji_id = ?")
                values.append(emoji_id)
                changes.append(f"Emoji: {emoji}")

            # Update channel
            if channel:
                updates.append("channel_id = ?")
                values.append(str(channel.id))
                changes.append(f"Channel: {channel.mention}")

            # Update colors - each accepts "#1E5C3A" or "1E5C3A", stored
            # without the '#' (ladder_image.py re-adds it when needed for
            # Pillow). primary_color fills the team's cell background in
            # /ladder; secondary_color is the team name text color drawn on
            # top of it.
            import re
            for column, value, label in (
                ("color", primary_color, "Primary color"),
                ("color_secondary", secondary_color, "Secondary color"),
            ):
                if not value:
                    continue
                hex_match = re.fullmatch(r'#?([0-9a-fA-F]{6})', value.strip())
                if not hex_match:
                    await interaction.response.send_message(
                        f"❌ '{value}' isn't a valid hex color - use a 6-digit hex code like `#1E5C3A`.",
                        ephemeral=True
                    )
                    return
                normalized_color = hex_match.group(1).upper()
                updates.append(f"{column} = ?")
                values.append(normalized_color)
                changes.append(f"{label}: #{normalized_color}")

            if not updates:
                await interaction.response.send_message(
                    "❌ No updates specified!",
                    ephemeral=True
                )
                return

            # Perform update
            values.append(team_id)
            query = f"UPDATE teams SET {', '.join(updates)} WHERE team_id = ?"

            try:
                await db.execute(query, values)
                await db.commit()

                # Build response
                response = f"✅ Updated **{current_name}**\n\n"
                response += "\n".join(changes)

                await interaction.response.send_message(response)
            except aiosqlite.IntegrityError:
                await interaction.response.send_message(
                    f"❌ Team name **{new_name}** already exists!",
                    ephemeral=True
                )

    @app_commands.command(name="config", description="[ADMIN] Configure bot settings")
    @app_commands.describe(
        lineups_channel="Channel where all lineup submissions are posted",
        delist_log_channel="Channel where player delistings are logged",
        trade_approval_channel="Channel where trades are sent for moderator approval",
        trade_log_channel="Channel where approved trades are announced",
        auctions_log_channel="Channel where free agency auction results are logged",
        bot_logs_channel="Channel where bot actions (bids, re-signs, matches) are logged",
        draft_channel="Channel where draft picks are announced",
        injury_list_channel="Channel where injury list is posted when advancing rounds",
        live_match_control_channel="Channel where the live match control panel (Start Qtr/pause/skip/abandon) is posted",
        live_match_feed_channel="Channel where live match events (goals, behinds, injuries) are posted",
        results_channel="Channel where completed in-season match results are posted",
        ladder_channel="Channel where the ladder is posted when advancing to the next round",
        season_1_year="Calendar year of Season 1 (for player aging)",
        result_delay_seconds="Delay between match results when simming a full round (0 = post all results at once)"
    )
    async def config(
        self,
        interaction: discord.Interaction,
        lineups_channel: discord.TextChannel = None,
        delist_log_channel: discord.TextChannel = None,
        trade_approval_channel: discord.TextChannel = None,
        trade_log_channel: discord.TextChannel = None,
        auctions_log_channel: discord.TextChannel = None,
        bot_logs_channel: discord.TextChannel = None,
        draft_channel: discord.TextChannel = None,
        injury_list_channel: discord.TextChannel = None,
        live_match_control_channel: discord.TextChannel = None,
        live_match_feed_channel: discord.TextChannel = None,
        results_channel: discord.TextChannel = None,
        ladder_channel: discord.TextChannel = None,
        season_1_year: int = None,
        result_delay_seconds: app_commands.Range[int, 0, 300] = None
    ):
        # If no parameters provided, show current settings
        if all(ch is None for ch in [lineups_channel, delist_log_channel, trade_approval_channel, trade_log_channel, auctions_log_channel, bot_logs_channel, draft_channel, injury_list_channel, live_match_control_channel, live_match_feed_channel, results_channel, ladder_channel, season_1_year, result_delay_seconds]):
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    """SELECT setting_key, setting_value FROM settings
                       WHERE setting_key IN ('lineups_channel_id', 'delist_log_channel_id',
                                             'trade_approval_channel_id', 'trade_log_channel_id',
                                             'auctions_log_channel_id', 'bot_logs_channel_id', 'draft_channel_id',
                                             'injury_list_channel_id', 'live_match_control_channel_id',
                                             'live_match_feed_channel_id', 'results_channel_id',
                                             'ladder_channel_id', 'season_1_year', 'result_delay_seconds')"""
                )
                results = await cursor.fetchall()

            embed = discord.Embed(title="⚙️ Bot Configuration", color=discord.Color.blue())

            settings = {key: value for key, value in results}

            # Lineups Channel
            if 'lineups_channel_id' in settings and settings['lineups_channel_id']:
                channel = interaction.guild.get_channel(int(settings['lineups_channel_id']))
                channel_display = channel.mention if channel else f"<#{settings['lineups_channel_id']}> (channel not found)"
            else:
                channel_display = "*Not set*"
            embed.add_field(name="Lineups Channel", value=channel_display, inline=False)

            # Delist Log Channel
            if 'delist_log_channel_id' in settings and settings['delist_log_channel_id']:
                channel = interaction.guild.get_channel(int(settings['delist_log_channel_id']))
                channel_display = channel.mention if channel else f"<#{settings['delist_log_channel_id']}> (channel not found)"
            else:
                channel_display = "*Not set*"
            embed.add_field(name="Delist Log Channel", value=channel_display, inline=False)

            # Trade Approval Channel
            if 'trade_approval_channel_id' in settings and settings['trade_approval_channel_id']:
                channel = interaction.guild.get_channel(int(settings['trade_approval_channel_id']))
                channel_display = channel.mention if channel else f"<#{settings['trade_approval_channel_id']}> (channel not found)"
            else:
                channel_display = "*Not set*"
            embed.add_field(name="Trade Approval Channel", value=channel_display, inline=False)

            # Trade Log Channel
            if 'trade_log_channel_id' in settings and settings['trade_log_channel_id']:
                channel = interaction.guild.get_channel(int(settings['trade_log_channel_id']))
                channel_display = channel.mention if channel else f"<#{settings['trade_log_channel_id']}> (channel not found)"
            else:
                channel_display = "*Not set*"
            embed.add_field(name="Trade Log Channel", value=channel_display, inline=False)

            # Auctions Log Channel
            if 'auctions_log_channel_id' in settings and settings['auctions_log_channel_id']:
                channel = interaction.guild.get_channel(int(settings['auctions_log_channel_id']))
                channel_display = channel.mention if channel else f"<#{settings['auctions_log_channel_id']}> (channel not found)"
            else:
                channel_display = "*Not set*"
            embed.add_field(name="Auctions Log Channel", value=channel_display, inline=False)

            # Bot Logs Channel
            if 'bot_logs_channel_id' in settings and settings['bot_logs_channel_id']:
                channel = interaction.guild.get_channel(int(settings['bot_logs_channel_id']))
                channel_display = channel.mention if channel else f"<#{settings['bot_logs_channel_id']}> (channel not found)"
            else:
                channel_display = "*Not set*"
            embed.add_field(name="Bot Logs Channel", value=channel_display, inline=False)

            # Draft Channel
            if 'draft_channel_id' in settings and settings['draft_channel_id']:
                channel = interaction.guild.get_channel(int(settings['draft_channel_id']))
                channel_display = channel.mention if channel else f"<#{settings['draft_channel_id']}> (channel not found)"
            else:
                channel_display = "*Not set*"
            embed.add_field(name="Draft Channel", value=channel_display, inline=False)

            # Injury List Channel
            if 'injury_list_channel_id' in settings and settings['injury_list_channel_id']:
                channel = interaction.guild.get_channel(int(settings['injury_list_channel_id']))
                channel_display = channel.mention if channel else f"<#{settings['injury_list_channel_id']}> (channel not found)"
            else:
                channel_display = "*Not set*"
            embed.add_field(name="Injury List Channel", value=channel_display, inline=False)

            # Live Match Control Channel
            if 'live_match_control_channel_id' in settings and settings['live_match_control_channel_id']:
                channel = interaction.guild.get_channel(int(settings['live_match_control_channel_id']))
                channel_display = channel.mention if channel else f"<#{settings['live_match_control_channel_id']}> (channel not found)"
            else:
                channel_display = "*Not set*"
            embed.add_field(name="Live Match Control Channel", value=channel_display, inline=False)

            # Live Match Feed Channel
            if 'live_match_feed_channel_id' in settings and settings['live_match_feed_channel_id']:
                channel = interaction.guild.get_channel(int(settings['live_match_feed_channel_id']))
                channel_display = channel.mention if channel else f"<#{settings['live_match_feed_channel_id']}> (channel not found)"
            else:
                channel_display = "*Not set*"
            embed.add_field(name="Live Match Feed Channel", value=channel_display, inline=False)

            # Results Channel
            if 'results_channel_id' in settings and settings['results_channel_id']:
                channel = interaction.guild.get_channel(int(settings['results_channel_id']))
                channel_display = channel.mention if channel else f"<#{settings['results_channel_id']}> (channel not found)"
            else:
                channel_display = "*Not set*"
            embed.add_field(name="Results Channel", value=channel_display, inline=False)

            # Ladder Channel
            if 'ladder_channel_id' in settings and settings['ladder_channel_id']:
                channel = interaction.guild.get_channel(int(settings['ladder_channel_id']))
                channel_display = channel.mention if channel else f"<#{settings['ladder_channel_id']}> (channel not found)"
            else:
                channel_display = "*Not set*"
            embed.add_field(name="Ladder Channel", value=channel_display, inline=False)

            # Season 1 Year
            if 'season_1_year' in settings and settings['season_1_year']:
                year_display = settings['season_1_year']
            else:
                year_display = "*Not set (defaults to 2016)*"
            embed.add_field(name="Season 1 Year", value=year_display, inline=False)

            # Result Delay (Seconds)
            if 'result_delay_seconds' in settings and settings['result_delay_seconds'] is not None:
                delay_display = f"{settings['result_delay_seconds']}s"
            else:
                delay_display = "*Not set (defaults to 15s)*"
            embed.add_field(name="Result Delay (Full Round Sim)", value=delay_display, inline=False)

            embed.set_footer(text="Use /config with parameters to update settings")

            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # Update settings
        updates = []
        async with aiosqlite.connect(DB_PATH) as db:
            if lineups_channel:
                await db.execute(
                    "INSERT OR REPLACE INTO settings (setting_key, setting_value) VALUES (?, ?)",
                    ("lineups_channel_id", str(lineups_channel.id))
                )
                updates.append(f"Lineups Channel → {lineups_channel.mention}")

            if delist_log_channel:
                await db.execute(
                    "INSERT OR REPLACE INTO settings (setting_key, setting_value) VALUES (?, ?)",
                    ("delist_log_channel_id", str(delist_log_channel.id))
                )
                updates.append(f"Delist Log Channel → {delist_log_channel.mention}")

            if trade_approval_channel:
                await db.execute(
                    "INSERT OR REPLACE INTO settings (setting_key, setting_value) VALUES (?, ?)",
                    ("trade_approval_channel_id", str(trade_approval_channel.id))
                )
                updates.append(f"Trade Approval Channel → {trade_approval_channel.mention}")

            if trade_log_channel:
                await db.execute(
                    "INSERT OR REPLACE INTO settings (setting_key, setting_value) VALUES (?, ?)",
                    ("trade_log_channel_id", str(trade_log_channel.id))
                )
                updates.append(f"Trade Log Channel → {trade_log_channel.mention}")

            if auctions_log_channel:
                await db.execute(
                    "INSERT OR REPLACE INTO settings (setting_key, setting_value) VALUES (?, ?)",
                    ("auctions_log_channel_id", str(auctions_log_channel.id))
                )
                updates.append(f"Auctions Log Channel → {auctions_log_channel.mention}")

            if bot_logs_channel:
                await db.execute(
                    "INSERT OR REPLACE INTO settings (setting_key, setting_value) VALUES (?, ?)",
                    ("bot_logs_channel_id", str(bot_logs_channel.id))
                )
                updates.append(f"Bot Logs Channel → {bot_logs_channel.mention}")

            if draft_channel:
                await db.execute(
                    "INSERT OR REPLACE INTO settings (setting_key, setting_value) VALUES (?, ?)",
                    ("draft_channel_id", str(draft_channel.id))
                )
                updates.append(f"Draft Channel → {draft_channel.mention}")

            if injury_list_channel:
                await db.execute(
                    "INSERT OR REPLACE INTO settings (setting_key, setting_value) VALUES (?, ?)",
                    ("injury_list_channel_id", str(injury_list_channel.id))
                )
                updates.append(f"Injury List Channel → {injury_list_channel.mention}")

            if live_match_control_channel:
                await db.execute(
                    "INSERT OR REPLACE INTO settings (setting_key, setting_value) VALUES (?, ?)",
                    ("live_match_control_channel_id", str(live_match_control_channel.id))
                )
                updates.append(f"Live Match Control Channel → {live_match_control_channel.mention}")

            if live_match_feed_channel:
                await db.execute(
                    "INSERT OR REPLACE INTO settings (setting_key, setting_value) VALUES (?, ?)",
                    ("live_match_feed_channel_id", str(live_match_feed_channel.id))
                )
                updates.append(f"Live Match Feed Channel → {live_match_feed_channel.mention}")

            if results_channel:
                await db.execute(
                    "INSERT OR REPLACE INTO settings (setting_key, setting_value) VALUES (?, ?)",
                    ("results_channel_id", str(results_channel.id))
                )
                updates.append(f"Results Channel → {results_channel.mention}")

            if ladder_channel:
                await db.execute(
                    "INSERT OR REPLACE INTO settings (setting_key, setting_value) VALUES (?, ?)",
                    ("ladder_channel_id", str(ladder_channel.id))
                )
                updates.append(f"Ladder Channel → {ladder_channel.mention}")

            if season_1_year is not None:
                await db.execute(
                    "INSERT OR REPLACE INTO settings (setting_key, setting_value) VALUES (?, ?)",
                    ("season_1_year", str(season_1_year))
                )
                updates.append(f"Season 1 Year → {season_1_year}")

            if result_delay_seconds is not None:
                await db.execute(
                    "INSERT OR REPLACE INTO settings (setting_key, setting_value) VALUES (?, ?)",
                    ("result_delay_seconds", str(result_delay_seconds))
                )
                updates.append(f"Result Delay (Full Round Sim) → {result_delay_seconds}s")

            await db.commit()

        if updates:
            await interaction.response.send_message(
                "✅ **Configuration Updated:**\n" + "\n".join(updates),
                ephemeral=True
            )
        else:
            await interaction.response.send_message("❌ No settings were updated!", ephemeral=True)

    @app_commands.command(name="addplayer", description="[ADMIN] Add a new player")
    @app_commands.describe(
        name="Player name",
        position="Player position",
        rating="Overall rating (1-100)",
        age="Player age",
        team_name="Team name (leave empty for delisted)",
        contract_expiry="Contract expiry season (optional)"
    )
    @app_commands.autocomplete(position=position_autocomplete)
    async def add_player(
        self,
        interaction: discord.Interaction,
        name: str,
        position: str,
        rating: int,
        age: int,
        team_name: str = None,
        contract_expiry: int = None
    ):
        is_valid, normalized_pos = validate_position(position)
        if not is_valid:
            await interaction.response.send_message(
                f"❌ Invalid position! Valid positions are:\n{get_positions_string()}",
                ephemeral=True
            )
            return
        
        if not 1 <= rating <= 100:
            await interaction.response.send_message(
                "❌ Rating must be between 1 and 100!",
                ephemeral=True
            )
            return
        
        async with aiosqlite.connect(DB_PATH) as db:
            team_id = None

            # If team specified, find it
            if team_name:
                cursor = await db.execute(
                    "SELECT team_id FROM teams WHERE team_name LIKE ?",
                    (f"%{team_name}%",)
                )
                team = await cursor.fetchone()

                if not team:
                    await interaction.response.send_message(
                        f"❌ No team found matching '{team_name}'",
                        ephemeral=True
                    )
                    return

                team_id = team[0]

            # Calculate birth_year from age
            current_year = await get_current_year(db)
            if current_year is None:
                current_year = 1
            birth_year = current_year - age

            # Add player
            await db.execute(
                """INSERT INTO players (name, position, overall_rating, age, birth_year, team_id, contract_expiry)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (name, normalized_pos, rating, age, birth_year, team_id, contract_expiry)
            )
            await db.commit()

            team_text = f"to **{team_name}**" if team_name else "as delisted"
            contract_text = f", contract expires Season {contract_expiry}" if contract_expiry else ""
            success_msg = f"✅ Added **{name}** ({normalized_pos}, {rating} OVR, {age}yo{contract_text}) {team_text}!"

            await interaction.response.send_message(success_msg)

    @app_commands.command(name="removeplayer", description="[ADMIN] Remove a player")
    @app_commands.describe(name="Player name")
    @app_commands.autocomplete(name=player_name_autocomplete)
    async def remove_player(self, interaction: discord.Interaction, name: str):
        async with aiosqlite.connect(DB_PATH) as db:
            # Get player by ID (name is actually player_id from autocomplete)
            try:
                player_id = int(name)
            except ValueError:
                await interaction.response.send_message(
                    f"❌ Invalid player selection. Please use the autocomplete suggestions.",
                    ephemeral=True
                )
                return

            cursor = await db.execute(
                "SELECT player_id, name FROM players WHERE player_id = ?",
                (player_id,)
            )
            player = await cursor.fetchone()

            if not player:
                await interaction.response.send_message(
                    f"❌ Player not found. Please select from the autocomplete suggestions.",
                    ephemeral=True
                )
                return

            player_id, player_name = player
            
            await db.execute("DELETE FROM players WHERE player_id = ?", (player_id,))
            await db.commit()
            
            await interaction.response.send_message(
                f"✅ Removed **{player_name}** from the league!"
            )

    @app_commands.command(name="updateplayer", description="[ADMIN] Update a player's stats")
    @app_commands.describe(
        name="Player name",
        new_name="New player name (optional)",
        ovr="New overall rating (optional)",
        age="New age (optional)",
        position="New position (optional)",
        team="New team (optional, use 'delisted' to release)",
        contract_expiry="Contract expiry season (optional)"
    )
    @app_commands.autocomplete(position=position_autocomplete, name=player_name_autocomplete, team=team_autocomplete)
    async def update_player(
        self,
        interaction: discord.Interaction,
        name: str,
        new_name: str = None,
        ovr: int = None,
        age: int = None,
        position: str = None,
        team: str = None,
        contract_expiry: int = None
    ):
        async with aiosqlite.connect(DB_PATH) as db:
            # Get player by ID (name is actually player_id from autocomplete)
            try:
                player_id = int(name)
            except ValueError:
                await interaction.response.send_message(
                    f"❌ Invalid player selection. Please use the autocomplete suggestions.",
                    ephemeral=True
                )
                return

            cursor = await db.execute(
                """SELECT player_id, name, overall_rating, age, position, team_id, contract_expiry
                   FROM players WHERE player_id = ?""",
                (player_id,)
            )
            player = await cursor.fetchone()

            if not player:
                await interaction.response.send_message(
                    f"❌ Player not found. Please select from the autocomplete suggestions.",
                    ephemeral=True
                )
                return

            player_id, player_name, old_rating, old_age, old_position, old_team_id, old_contract_expiry = player

            # Get old team name if exists
            old_team_name = None
            if old_team_id:
                cursor = await db.execute(
                    "SELECT team_name FROM teams WHERE team_id = ?",
                    (old_team_id,)
                )
                result = await cursor.fetchone()
                if result:
                    old_team_name = result[0]

            updates = []
            values = []
            changes = []

            # Check for duplicate names if changing name
            duplicate_warning = None
            if new_name is not None:
                cursor = await db.execute(
                    "SELECT player_id, name FROM players WHERE LOWER(name) = LOWER(?) AND player_id != ?",
                    (new_name, player_id)
                )
                duplicate = await cursor.fetchone()
                if duplicate:
                    duplicate_warning = f"\n\n⚠️ Note: Another player named **{duplicate[1]}** already exists (ID: {duplicate[0]})."

                updates.append("name = ?")
                values.append(new_name)
                changes.append(f"Name: {player_name} → {new_name}")

            if ovr is not None:
                if not 1 <= ovr <= 100:
                    await interaction.response.send_message(
                        "❌ Rating must be between 1 and 100!",
                        ephemeral=True
                    )
                    return
                updates.append("overall_rating = ?")
                values.append(ovr)
                changes.append(f"OVR: {old_rating} → {ovr}")

            if age is not None:
                # Calculate new birth_year from age
                current_year = await get_current_year(db)
                if current_year is None:
                    current_year = 1
                birth_year = current_year - age

                updates.append("age = ?")
                updates.append("birth_year = ?")
                values.append(age)
                values.append(birth_year)
                changes.append(f"Age: {old_age} → {age}")

            if position is not None:
                is_valid, normalized_pos = validate_position(position)
                if not is_valid:
                    await interaction.response.send_message(
                        f"❌ Invalid position! Valid positions are:\n{get_positions_string()}",
                        ephemeral=True
                    )
                    return
                updates.append("position = ?")
                values.append(normalized_pos)
                changes.append(f"Position: {old_position} → {normalized_pos}")

            if team is not None:
                # Handle team update
                if team.lower() in ['delisted', 'delist', 'del']:
                    # Release to free agency
                    new_team_id = None
                    new_team_display = "FA"
                    new_team_emoji = None
                else:
                    # Find the new team
                    cursor = await db.execute(
                        "SELECT team_id, team_name, emoji_id FROM teams WHERE team_name LIKE ?",
                        (f"%{team}%",)
                    )
                    team_result = await cursor.fetchone()

                    if not team_result:
                        await interaction.response.send_message(
                            f"❌ No team found matching '{team}'",
                            ephemeral=True
                        )
                        return

                    new_team_id = team_result[0]
                    new_team_name = team_result[1]
                    new_team_emoji_id = team_result[2]

                    # Get emoji for new team
                    new_team_emoji_obj = get_team_emoji(interaction.client, new_team_emoji_id)
                    new_team_emoji = str(new_team_emoji_obj) if new_team_emoji_obj else None

                    new_team_display = new_team_emoji if new_team_emoji else new_team_name

                # Get emoji for old team
                old_team_display = "FA"
                if old_team_id:
                    cursor = await db.execute(
                        "SELECT emoji_id FROM teams WHERE team_id = ?",
                        (old_team_id,)
                    )
                    result = await cursor.fetchone()
                    if result and result[0]:
                        emoji_obj = get_team_emoji(interaction.client, result[0])
                        old_team_display = str(emoji_obj) if emoji_obj else old_team_name
                    else:
                        old_team_display = old_team_name

                updates.append("team_id = ?")
                values.append(new_team_id)
                changes.append(f"Team: {old_team_display} → {new_team_display}")

            if contract_expiry is not None:
                updates.append("contract_expiry = ?")
                values.append(contract_expiry)
                old_expiry_display = f"Season {old_contract_expiry}" if old_contract_expiry else "None"
                changes.append(f"Contract Expiry: {old_expiry_display} → Season {contract_expiry}")

            if not updates:
                await interaction.response.send_message(
                    "❌ No updates specified!",
                    ephemeral=True
                )
                return

            values.append(player_id)
            query = f"UPDATE players SET {', '.join(updates)} WHERE player_id = ?"

            await db.execute(query, values)
            await db.commit()

            # Build response with changes
            response = f"✅ Updated **{player_name}**\n\n"
            response += "\n".join(changes)

            # Add duplicate warning if applicable
            if duplicate_warning:
                response += duplicate_warning

            await interaction.response.send_message(response, ephemeral=True)

    @app_commands.command(name="exportdata", description="[ADMIN] Export all teams and players to Excel")
    async def export_data(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Export Teams
                cursor = await db.execute(
                    """SELECT team_name as Team_Name, role_id as Role_ID, emoji_id as Emoji_ID, channel_id as Channel_ID,
                              color as Primary_Color, color_secondary as Secondary_Color
                       FROM teams ORDER BY team_name"""
                )
                teams = await cursor.fetchall()
                teams_df = pd.DataFrame(teams, columns=['Team_Name', 'Role_ID', 'Emoji_ID', 'Channel_ID', 'Primary_Color', 'Secondary_Color'])
                teams_df['Role_ID'] = teams_df['Role_ID'].fillna('')
                teams_df['Emoji_ID'] = teams_df['Emoji_ID'].fillna('')
                teams_df['Channel_ID'] = teams_df['Channel_ID'].fillna('')
                teams_df['Primary_Color'] = teams_df['Primary_Color'].fillna('')
                teams_df['Secondary_Color'] = teams_df['Secondary_Color'].fillna('')
                
                # Games_Played is a career total (all seasons) derived from
                # player_match_stats - one bulk query for every player rather
                # than N calls to utils.get_games_played (that helper is for
                # a single-player lookup, e.g. a future player-profile
                # command, not bulk export). Not read on import - see the
                # comment on players_df['Games_Played'] below.
                cursor = await db.execute(
                    "SELECT player_id, COUNT(*) FROM player_match_stats GROUP BY player_id"
                )
                games_played_by_player_id = {row[0]: row[1] for row in await cursor.fetchall()}

                # Export Players (full replace on import - rows with a Player_ID update
                # that player, rows without one become new players, and any player not
                # present in this sheet on import is removed, same as every other sheet)
                cursor = await db.execute(
                    """SELECT p.player_id as Player_ID, p.name as Name, t.team_name as Team,
                              p.age as Age, p.birth_year as Birth_Year, p.position as Pos, p.overall_rating as OVR,
                              p.contract_expiry as Contract_Expiry, p.plays_like as Plays_Like, fs.team_name as Father_Son_Club
                       FROM players p
                       LEFT JOIN teams t ON p.team_id = t.team_id
                       LEFT JOIN teams fs ON p.father_son_club_id = fs.team_id
                       ORDER BY p.name"""
                )
                players = await cursor.fetchall()
                players_df = pd.DataFrame(players, columns=['Player_ID', 'Name', 'Team', 'Age', 'Birth_Year', 'Pos', 'OVR', 'Contract_Expiry', 'Plays_Like', 'Father_Son_Club'])
                players_df['Team'] = players_df['Team'].fillna('')
                players_df['Birth_Year'] = players_df['Birth_Year'].fillna('')
                players_df['Contract_Expiry'] = players_df['Contract_Expiry'].fillna('')
                players_df['Plays_Like'] = players_df['Plays_Like'].fillna('')
                players_df['Father_Son_Club'] = players_df['Father_Son_Club'].fillna('')
                # Derived/read-only, same as every other computed column here -
                # never read on import (the Players import block below selects
                # named columns explicitly, so simply not referencing
                # Games_Played there is sufficient; do not "helpfully" wire it in).
                players_df['Games_Played'] = players_df['Player_ID'].map(games_played_by_player_id).fillna(0).astype(int)
                players_df = players_df[['Player_ID', 'Name', 'Team', 'Age', 'Birth_Year', 'Pos', 'OVR', 'Games_Played',
                                          'Contract_Expiry', 'Plays_Like', 'Father_Son_Club']]

                # Export Lineups (merged Current, Starting, and Submitted into one sheet with Type column)
                lineups_list = []

                # Export Current Lineups
                cursor = await db.execute(
                    """SELECT t.team_name as Team_Name, l.position_name as Position,
                              l.player_id as Player_ID, p.name as Player_Name
                       FROM lineups l
                       JOIN teams t ON l.team_id = t.team_id
                       JOIN players p ON l.player_id = p.player_id
                       ORDER BY t.team_name, l.slot_number"""
                )
                current_lineups = await cursor.fetchall()
                for team_name, position, player_id, player_name in current_lineups:
                    lineups_list.append({
                        'Type': 'current',
                        'Team_Name': team_name,
                        'Position': position,
                        'Player_ID': player_id,
                        'Player_Name': player_name,
                        'Season': '',
                        'Round': ''
                    })

                # Load a full player_id -> name map once, so the starting/submitted
                # lineup loops below can look up names without a query per player
                cursor = await db.execute("SELECT player_id, name FROM players")
                player_name_by_id = {pid: name for pid, name in await cursor.fetchall()}

                # Export Starting Lineups (flatten JSON)
                cursor = await db.execute(
                    """SELECT t.team_name, sl.lineup_data
                       FROM starting_lineups sl
                       JOIN teams t ON sl.team_id = t.team_id
                       ORDER BY t.team_name"""
                )
                starting_lineup_rows = await cursor.fetchall()
                for team_name, lineup_json in starting_lineup_rows:
                    if lineup_json:
                        lineup_data = json.loads(lineup_json)
                        for position_name, player_id in lineup_data.items():
                            player_id = int(player_id)
                            player_name = player_name_by_id.get(player_id, f"Unknown ({player_id})")
                            lineups_list.append({
                                'Type': 'starting',
                                'Team_Name': team_name,
                                'Position': position_name,
                                'Player_ID': player_id,
                                'Player_Name': player_name,
                                'Season': '',
                                'Round': ''
                            })

                lineups_df = pd.DataFrame(lineups_list)
                if lineups_df.empty:
                    lineups_df = pd.DataFrame(columns=['Type', 'Team_Name', 'Position', 'Player_ID', 'Player_Name', 'Season', 'Round'])

                # Export Seasons
                cursor = await db.execute(
                    """SELECT season_number as Season, current_round as Current_Round,
                              regular_rounds as Regular_Rounds, total_rounds as Total_Rounds,
                              round_name as Round_Name, status as Status
                       FROM seasons ORDER BY season_number"""
                )
                seasons = await cursor.fetchall()
                seasons_df = pd.DataFrame(seasons, columns=['Season', 'Current_Round', 'Regular_Rounds', 'Total_Rounds', 'Round_Name', 'Status'])

                # Export Injuries (removed Recovery_Rounds - redundant with Injury_Round + Return_Round;
                # removed Status - only active injuries are ever stored, recovered ones are deleted)
                cursor = await db.execute(
                    """SELECT i.player_id as Player_ID, p.name as Player_Name, t.team_name as Team,
                              i.injury_type as Injury_Type, i.injury_round as Injury_Round,
                              i.return_round as Return_Round
                       FROM injuries i
                       JOIN players p ON i.player_id = p.player_id
                       LEFT JOIN teams t ON p.team_id = t.team_id
                       ORDER BY i.return_round"""
                )
                injuries = await cursor.fetchall()
                injuries_df = pd.DataFrame(injuries, columns=['Player_ID', 'Player_Name', 'Team', 'Injury_Type', 'Injury_Round', 'Return_Round'])
                injuries_df['Team'] = injuries_df['Team'].fillna('Delisted')

                # Export Suspensions (removed Status - only active suspensions
                # are ever stored, completed ones are deleted). Games_Missed
                # (the original total) and Games_Remaining (what's actually
                # left - only ticks down on rounds the player's team played)
                # are both exported since neither is derivable from the
                # other without knowing how many rounds have passed.
                cursor = await db.execute(
                    """SELECT s.player_id as Player_ID, p.name as Player_Name, t.team_name as Team,
                              s.suspension_round as Suspension_Round, s.games_missed as Games_Missed,
                              s.games_remaining as Games_Remaining, s.suspension_reason as Reason
                       FROM suspensions s
                       JOIN players p ON s.player_id = p.player_id
                       LEFT JOIN teams t ON p.team_id = t.team_id
                       ORDER BY s.suspension_round"""
                )
                suspensions = await cursor.fetchall()
                suspensions_df = pd.DataFrame(suspensions, columns=['Player_ID', 'Player_Name', 'Team', 'Suspension_Round', 'Games_Missed', 'Games_Remaining', 'Reason'])
                suspensions_df['Team'] = suspensions_df['Team'].fillna('Delisted')

                # Export Trades
                cursor = await db.execute(
                    """SELECT tr.trade_id as Trade_ID, t1.team_name as Initiating_Team, t2.team_name as Receiving_Team,
                              tr.initiating_players as Initiating_Players, tr.receiving_players as Receiving_Players,
                              tr.initiating_picks as Initiating_Picks, tr.receiving_picks as Receiving_Picks,
                              tr.status as Status, tr.created_at as Created_At, tr.responded_at as Responded_At,
                              tr.approved_at as Approved_At, tr.created_by_user_id as Created_By_User_ID,
                              tr.responded_by_user_id as Responded_By_User_ID, tr.approved_by_user_id as Approved_By_User_ID,
                              tr.original_trade_id as Original_Trade_ID
                       FROM trades tr
                       JOIN teams t1 ON tr.initiating_team_id = t1.team_id
                       JOIN teams t2 ON tr.receiving_team_id = t2.team_id
                       ORDER BY tr.created_at DESC"""
                )
                trades = await cursor.fetchall()
                trades_df = pd.DataFrame(trades, columns=['Trade_ID', 'Initiating_Team', 'Receiving_Team', 'Initiating_Players', 'Receiving_Players', 'Initiating_Picks', 'Receiving_Picks', 'Status', 'Created_At', 'Responded_At', 'Approved_At', 'Created_By_User_ID', 'Responded_By_User_ID', 'Approved_By_User_ID', 'Original_Trade_ID'])
                trades_df = trades_df.fillna('')

                # Export Settings
                cursor = await db.execute(
                    """SELECT setting_key as Setting_Key, setting_value as Setting_Value
                       FROM settings ORDER BY setting_key"""
                )
                settings = await cursor.fetchall()
                settings_df = pd.DataFrame(settings, columns=['Setting_Key', 'Setting_Value'])
                settings_df = settings_df.fillna('')

                # Export Matches
                cursor = await db.execute(
                    """SELECT m.match_id as Match_ID, s.season_number as Season,
                              m.round_number as Round, ht.team_name as Home_Team,
                              at.team_name as Away_Team, m.home_score as Home_Score,
                              m.away_score as Away_Score, m.simulated as Simulated
                       FROM matches m
                       JOIN seasons s ON m.season_id = s.season_id
                       JOIN teams ht ON m.home_team_id = ht.team_id
                       JOIN teams at ON m.away_team_id = at.team_id
                       ORDER BY s.season_number, m.round_number, m.match_id"""
                )
                matches = await cursor.fetchall()
                matches_df = pd.DataFrame(matches, columns=['Match_ID', 'Season', 'Round', 'Home_Team', 'Away_Team', 'Home_Score', 'Away_Score', 'Simulated'])

                # Export Player_Match_Stats - full replace on import, same as
                # most other sheets (see the import block below). team_id is
                # the team the player played FOR in that specific match (not
                # their current team) - same historical-accuracy reasoning
                # Injuries/Suspensions already use for their own team
                # snapshots, so a traded player's old rows correctly keep
                # showing their team at the time.
                cursor = await db.execute(
                    """SELECT pms.match_id as Match_ID, s.season_number as Season,
                              m.round_number as Round, t.team_name as Team,
                              pms.player_id as Player_ID, p.name as Player_Name,
                              opp.team_name as Opponent,
                              pms.disposals as Disposals, pms.goals as Goals, pms.behinds as Behinds,
                              pms.marks as Marks, pms.tackles as Tackles, pms.spoils as Spoils,
                              pms.hitouts as Hitouts, pms.brownlow_votes as Brownlow_Votes,
                              pms.best_fairest_votes as Best_Fairest_Votes
                       FROM player_match_stats pms
                       JOIN matches m ON pms.match_id = m.match_id
                       JOIN seasons s ON m.season_id = s.season_id
                       JOIN teams t ON pms.team_id = t.team_id
                       JOIN players p ON pms.player_id = p.player_id
                       JOIN teams opp ON opp.team_id = (
                           CASE WHEN pms.team_id = m.home_team_id THEN m.away_team_id ELSE m.home_team_id END
                       )
                       ORDER BY s.season_number, m.round_number, pms.match_id, t.team_name, pms.disposals DESC"""
                )
                player_match_stats = await cursor.fetchall()
                player_match_stats_df = pd.DataFrame(player_match_stats, columns=[
                    'Match_ID', 'Season', 'Round', 'Team', 'Player_ID', 'Player_Name', 'Opponent',
                    'Disposals', 'Goals', 'Behinds', 'Marks', 'Tackles', 'Spoils', 'Hitouts', 'Brownlow_Votes',
                    'Best_Fairest_Votes',
                ])

                # Export Drafts (check which columns exist for backwards compatibility)
                cursor = await db.execute("PRAGMA table_info(drafts)")
                draft_columns = await cursor.fetchall()
                draft_column_names = [col[1] for col in draft_columns]

                # Build SELECT query based on available columns
                draft_select = "draft_id as Draft_ID, draft_name as Draft_Name, season_number as Season_Number, status as Status, rounds as Rounds, rookie_contract_years as Rookie_Contract_Years, created_at as Created_At, ladder_set_at as Ladder_Set_At"
                draft_col_list = ['Draft_ID', 'Draft_Name', 'Season_Number', 'Status', 'Rounds', 'Rookie_Contract_Years', 'Created_At', 'Ladder_Set_At']

                if 'started_at' in draft_column_names:
                    draft_select += ", started_at as Started_At"
                    draft_col_list.append('Started_At')
                if 'completed_at' in draft_column_names:
                    draft_select += ", completed_at as Completed_At"
                    draft_col_list.append('Completed_At')
                if 'current_pick_number' in draft_column_names:
                    draft_select += ", current_pick_number as Current_Pick_Number"
                    draft_col_list.append('Current_Pick_Number')

                cursor = await db.execute(f"SELECT {draft_select} FROM drafts ORDER BY draft_id")
                drafts = await cursor.fetchall()
                drafts_df = pd.DataFrame(drafts, columns=draft_col_list)
                drafts_df = drafts_df.fillna('')

                # Export Draft Picks
                cursor = await db.execute(
                    """SELECT dp.pick_id as Pick_ID, dp.draft_name as Draft_Name,
                              dp.round_number as Round, dp.pick_number as Pick,
                              dp.pick_origin as Pick_Origin, ct.team_name as Current_Team,
                              dp.player_selected_id as Player_ID, p.name as Player_Name,
                              dp.passed as Passed, dp.picked_at as Picked_At
                       FROM draft_picks dp
                       JOIN teams ct ON dp.current_team_id = ct.team_id
                       LEFT JOIN players p ON dp.player_selected_id = p.player_id
                       ORDER BY dp.draft_name, dp.round_number, dp.pick_number"""
                )
                draft_picks = await cursor.fetchall()
                draft_picks_df = pd.DataFrame(draft_picks, columns=['Pick_ID', 'Draft_Name', 'Round', 'Pick', 'Pick_Origin', 'Current_Team', 'Player_ID', 'Player_Name', 'Passed', 'Picked_At'])
                draft_picks_df['Pick_Origin'] = draft_picks_df['Pick_Origin'].fillna('')
                draft_picks_df['Player_ID'] = draft_picks_df['Player_ID'].fillna('')
                draft_picks_df['Player_Name'] = draft_picks_df['Player_Name'].fillna('')
                draft_picks_df['Picked_At'] = draft_picks_df['Picked_At'].fillna('')

                # Export Ladder Positions
                cursor = await db.execute(
                    """SELECT lp.ladder_id as Ladder_ID, s.season_number as Season,
                              t.team_name as Team, lp.position as Position
                       FROM ladder_positions lp
                       JOIN seasons s ON lp.season_id = s.season_id
                       JOIN teams t ON lp.team_id = t.team_id
                       ORDER BY s.season_number, lp.position"""
                )
                ladder_positions = await cursor.fetchall()
                ladder_positions_df = pd.DataFrame(ladder_positions, columns=['Ladder_ID', 'Season', 'Team', 'Position'])

                # Export Compensation Chart as 2D table (individual ages 19-33, individual OVRs 70-99)
                cursor = await db.execute(
                    """SELECT min_age, max_age, min_ovr, max_ovr, compensation_band
                       FROM compensation_chart
                       ORDER BY min_age, min_ovr"""
                )
                compensation_data = await cursor.fetchall()

                # Build map of (age, ovr) -> band by expanding ranges
                comp_map = {}  # (age, ovr) -> band
                for min_age, max_age, min_ovr, max_ovr, band in compensation_data:
                    # Expand age range
                    age_end = max_age if max_age else 99
                    # Expand OVR range
                    ovr_end = max_ovr if max_ovr else 99

                    for age in range(min_age, age_end + 1):
                        for ovr in range(min_ovr, ovr_end + 1):
                            comp_map[(age, ovr)] = band

                # Create 2D grid: rows = ages 19-33, columns = OVRs 70-99
                ages = list(range(19, 34))  # 19 to 33 inclusive
                ovrs = list(range(70, 100))  # 70 to 99 inclusive

                # Build header row
                header = ['Age \\ OVR'] + [str(ovr) for ovr in ovrs]

                # Build data rows
                table_data = []
                for age in ages:
                    row = [str(age)]
                    for ovr in ovrs:
                        band = comp_map.get((age, ovr), '')
                        row.append(band if band else '')
                    table_data.append(row)

                compensation_chart_df = pd.DataFrame(table_data, columns=header)

                # Export Contract Config
                cursor = await db.execute(
                    """SELECT min_age as Min_Age, max_age as Max_Age, contract_years as Contract_Years
                       FROM contract_config
                       ORDER BY min_age"""
                )
                contract_config = await cursor.fetchall()
                contract_config_df = pd.DataFrame(contract_config, columns=['Min_Age', 'Max_Age', 'Contract_Years'])
                contract_config_df['Max_Age'] = contract_config_df['Max_Age'].fillna('')

                # Export Draft Value Index
                cursor = await db.execute(
                    """SELECT pick_number as Pick_Number, points_value as Points_Value
                       FROM draft_value_index
                       ORDER BY pick_number"""
                )
                draft_value_index = await cursor.fetchall()
                draft_value_index_df = pd.DataFrame(draft_value_index, columns=['Pick_Number', 'Points_Value'])

                # Export Free Agency Bids
                # (The free agency period itself is exported via the Settings sheet:
                #  fa_period_status / fa_period_season / fa_period_auction_points)
                cursor = await db.execute(
                    """SELECT fab.bid_id as Bid_ID, fab.season_number as Season_Number,
                              t.team_name as Team, fab.player_id as Player_ID,
                              p.name as Player_Name, fab.bid_amount as Bid_Amount,
                              fab.status as Status, fab.placed_at as Placed_At
                       FROM free_agency_bids fab
                       JOIN teams t ON fab.team_id = t.team_id
                       JOIN players p ON fab.player_id = p.player_id
                       ORDER BY fab.season_number DESC, fab.placed_at DESC"""
                )
                free_agency_bids = await cursor.fetchall()
                free_agency_bids_df = pd.DataFrame(free_agency_bids, columns=['Bid_ID', 'Season_Number', 'Team', 'Player_ID', 'Player_Name', 'Bid_Amount', 'Status', 'Placed_At'])
                free_agency_bids_df = free_agency_bids_df.fillna('')

                # Export Free Agency Re-Signs
                cursor = await db.execute(
                    """SELECT far.resign_id as Resign_ID, far.season_number as Season_Number,
                              t.team_name as Team, far.player_id as Player_ID,
                              p.name as Player_Name, far.confirmed as Confirmed,
                              far.confirmed_at as Confirmed_At
                       FROM free_agency_resigns far
                       JOIN teams t ON far.team_id = t.team_id
                       JOIN players p ON far.player_id = p.player_id
                       ORDER BY far.season_number DESC, far.confirmed_at DESC"""
                )
                free_agency_resigns = await cursor.fetchall()
                free_agency_resigns_df = pd.DataFrame(free_agency_resigns, columns=['Resign_ID', 'Season_Number', 'Team', 'Player_ID', 'Player_Name', 'Confirmed', 'Confirmed_At'])
                free_agency_resigns_df = free_agency_resigns_df.fillna('')

                # Export Free Agency Results
                cursor = await db.execute(
                    """SELECT far.result_id as Result_ID, far.season_number as Season_Number,
                              far.player_id as Player_ID, p.name as Player_Name,
                              orig.team_name as Original_Team, win.team_name as Winning_Team,
                              far.winning_bid as Winning_Bid, far.matched as Matched,
                              far.compensation_band as Compensation_Band, far.confirmed_at as Confirmed_At
                       FROM free_agency_results far
                       JOIN players p ON far.player_id = p.player_id
                       JOIN teams orig ON far.original_team_id = orig.team_id
                       LEFT JOIN teams win ON far.winning_team_id = win.team_id
                       ORDER BY far.season_number DESC, far.result_id"""
                )
                free_agency_results = await cursor.fetchall()
                free_agency_results_df = pd.DataFrame(free_agency_results, columns=['Result_ID', 'Season_Number', 'Player_ID', 'Player_Name', 'Original_Team', 'Winning_Team', 'Winning_Bid', 'Matched', 'Compensation_Band', 'Confirmed_At'])
                free_agency_results_df = free_agency_results_df.fillna('')

            # Create Excel file in memory
            output = io.BytesIO()

            # Columns that hold Discord snowflake IDs - these must be written as text,
            # otherwise Excel silently mangles large integers into scientific notation
            # or truncates precision, corrupting the ID on the next edit/re-import.
            id_columns = {
                'Role_ID', 'Emoji_ID', 'Channel_ID',
                'Created_By_User_ID', 'Responded_By_User_ID', 'Approved_By_User_ID',
            }

            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                # README first, so it's the leftmost/default tab when the file opens
                instructions = pd.DataFrame({
                    'IMPORTANT INSTRUCTIONS': [
                        '--- EXCEL IMPORT/EXPORT GUIDE ---',
                        '',
                        'NOTE: Role_ID / Emoji_ID / Channel_ID columns are pre-formatted as Text',
                        '  - If you type a NEW ID into a blank cell, format that cell as Text first',
                        '    (Right-click → Format Cells → Text) to avoid Excel corrupting the number',
                        '',
                        'To import: Use /importdata command and attach this file',
                        '',
                        '--- SHEET ORGANIZATION ---',
                        '',
                        'CORE DATA (Editable):',
                        '  • Teams - Team info, Discord role/emoji/channel IDs',
                        '  • Players - All players (see Key Features below for how edits/adds work)',
                        '  • Seasons - Season configuration',
                        '  • Settings - Bot settings (key/value)',
                        '  • Compensation_Chart - Free agency compensation bands by Age/OVR',
                        '  • Contract_Config - Contract length by age range',
                        '  • Draft_Value_Index - Draft pick number → points value',
                        '',
                        'RELATIONSHIPS/STATE (Editable):',
                        '  • Lineups - Current, starting, and submitted lineups (see Type column)',
                        '  • Injuries - Injury status (Recovery_Rounds calculated automatically)',
                        '  • Suspensions - Suspension status (Games_Remaining is the real "still serving" counter)',
                        '  • Drafts - Draft metadata (updates existing drafts only, does not create new ones)',
                        '  • Draft_Picks - Draft pick ownership',
                        '  • Ladder_Positions - Ladder position per team per season',
                        '  • Free_Agency_Bids - Free agency auction bids',
                        '  • Free_Agency_Re-Signs - Free re-sign confirmations',
                        '  • Free_Agency_Results - Free agency outcomes (winner, bid, compensation)',
                        '  • Matches - Fixtures AND results (see Key Features below for how adds work)',
                        '  • Player_Match_Stats - Full box score history, one row per player per completed',
                        '    match (see Key Features below - full replace, delete rows to delete stats)',
                        '',
                        'HISTORY (Read-only - import supported):',
                        '  • Trades - Trade history',
                        '',
                        '--- KEY FEATURES ---',
                        '',
                        '1. Players sheet fully replaces all player data on import',
                        '   - Rows WITH a Player_ID update that existing player',
                        '   - Rows with Player_ID left BLANK are added as new players',
                        '   - Any existing player NOT present in this sheet after import will be DELETED,',
                        '     along with anything that referenced them (lineups, injuries, etc.)',
                        '   - To add a new player: add a row and leave Player_ID blank',
                        '   - To remove a player: delete their row before importing',
                        '   - Games_Played is read-only (derived from Player_Match_Stats) - edits to it are ignored',
                        '',
                        '2. Matches sheet covers fixtures AND results, same Match_ID convention as Players',
                        '   - Rows WITH a Match_ID update that existing match',
                        '   - Rows with Match_ID left BLANK are added as new fixture entries',
                        '   - To add a fixture: add a row with Season/Round/Home_Team/Away_Team filled in',
                        '     and Match_ID, Home_Score, Away_Score, Simulated all left blank',
                        '   - A match already simulated in the bot keeps its real score/Simulated=True',
                        '     when re-exported - only touch those cells if you mean to overwrite a result',
                        '',
                        '3. Simplified Injuries/Suspensions',
                        '   - Injuries: just enter Injury_Round and Return_Round - Recovery_Rounds is calculated on import',
                        '   - Suspensions: enter Games_Missed and Games_Remaining directly (Games_Remaining is the',
                        '     real "still serving" counter - it only ticks down on rounds the team actually plays,',
                        '     unlike Injuries which count down every round regardless of byes)',
                        '',
                        '4. Lineups sheet covers all lineup types',
                        '   - The Type column distinguishes current / starting / submitted rows',
                        '   - All rows share the same Team/Position/Player format',
                        '',
                        '5. Player_Match_Stats sheet fully replaces all match stats on import',
                        '   - Every row needs a valid Match_ID (must exist in the Matches sheet/table) and Player_ID',
                        '   - To delete stats: delete their row(s) before importing - there is no Stat_ID to preserve,',
                        '     rows are just re-created fresh from whatever is in the sheet',
                        '   - Since Matches is also fully replaced on every import, reimporting an OLD Matches',
                        '     sheet alongside an OLD (or emptied) Player_Match_Stats sheet resets accumulated match',
                        '     stats back to that snapshot too - useful after a round of test-season simulation',
                        '',
                        '6. Almost every sheet fully replaces existing data on import',
                        '   - Players, Injuries, Suspensions, Draft_Picks, Ladder_Positions, Trades, Matches,',
                        '     Player_Match_Stats, Compensation_Chart, Contract_Config, Draft_Value_Index, and all',
                        '     Free_Agency sheets are cleared and replaced entirely from this file - double-check',
                        '     the data before importing',
                        '',
                        '--- VALID POSITIONS ---',
                        '',
                        'Player Positions: MID, KEY FWD, RUCK, GEN DEF, etc.',
                        'Lineup Positions: FB, CHB, LW, C, RW, CHF, FF, R, RR, RO, INT1-5',
                    ]
                })
                instructions.to_excel(writer, sheet_name='README', index=False)

                # All data sheets, in the same Core / Relationships / History grouping
                # documented in the README above
                sheets = [
                    ('Teams', teams_df),
                    ('Players', players_df),
                    ('Seasons', seasons_df),
                    ('Settings', settings_df),
                    ('Compensation_Chart', compensation_chart_df),
                    ('Contract_Config', contract_config_df),
                    ('Draft_Value_Index', draft_value_index_df),
                    ('Lineups', lineups_df),
                    ('Injuries', injuries_df),
                    ('Suspensions', suspensions_df),
                    ('Drafts', drafts_df),
                    ('Draft_Picks', draft_picks_df),
                    ('Ladder_Positions', ladder_positions_df),
                    ('Free_Agency_Bids', free_agency_bids_df),
                    ('Free_Agency_Re-Signs', free_agency_resigns_df),
                    ('Free_Agency_Results', free_agency_results_df),
                    ('Trades', trades_df),
                    ('Matches', matches_df),
                    ('Player_Match_Stats', player_match_stats_df),
                ]

                for sheet_name, df in sheets:
                    # Blanket fillna so every sheet shows blank cells consistently
                    # instead of a mix of empty strings and literal "nan" text
                    df.fillna('').to_excel(writer, sheet_name=sheet_name, index=False)

                # Formatting pass: bold/frozen header row, readable column widths,
                # and text-formatted ID columns so Excel can't corrupt Discord IDs
                for sheet_name, df in [('README', instructions)] + sheets:
                    worksheet = writer.sheets[sheet_name]
                    worksheet.freeze_panes = 'A2'

                    for col_idx, col_name in enumerate(df.columns, start=1):
                        cell = worksheet.cell(row=1, column=col_idx)
                        cell.font = Font(bold=True)

                        column_letter = get_column_letter(col_idx)
                        if col_name in id_columns:
                            for row_idx in range(2, len(df) + 2):
                                id_cell = worksheet.cell(row=row_idx, column=col_idx)
                                # Force the cell's actual value to text, not just its display
                                # format - otherwise pandas has already written it as a
                                # number and Excel/pandas will still mangle/round-trip it
                                # incorrectly on re-open or re-export.
                                if id_cell.value not in (None, ''):
                                    id_cell.value = str(id_cell.value)
                                id_cell.number_format = '@'
                            worksheet.column_dimensions[column_letter].width = 20
                        elif sheet_name == 'README':
                            # README is prose, not tabular data - give it room to read
                            worksheet.column_dimensions[column_letter].width = 90
                        else:
                            # Size to the widest of the header or a sample of values,
                            # capped so one long cell doesn't blow out the whole sheet
                            longest_value = df[col_name].astype(str).str.len().max() if len(df) else 0
                            width = max(len(str(col_name)), longest_value if pd.notna(longest_value) else 0) + 2
                            worksheet.column_dimensions[column_letter].width = min(max(width, 10), 40)
            output.seek(0)
            
            # Send file
            file = discord.File(output, filename='league_data.xlsx')
            stats = [
                f"{len(teams_df)} teams",
                f"{len(players_df)} players",
                f"{len(lineups_df)} lineup positions",
                f"{len(seasons_df)} seasons",
                f"{len(injuries_df)} injuries",
                f"{len(suspensions_df)} suspensions",
                f"{len(draft_picks_df)} draft picks",
                f"{len(trades_df)} trades",
                f"{len(matches_df)} matches",
                f"{len(settings_df)} settings"
            ]
            await interaction.followup.send(
                f"✅ Exported: {', '.join(stats)}",
                file=file,
                ephemeral=True
            )
            
        except Exception as e:
            await interaction.followup.send(f"❌ Error exporting data: {e}", ephemeral=True)

    @app_commands.command(name="importdata", description="[ADMIN] Import teams and players from Excel file")
    async def import_data(self, interaction: discord.Interaction, file: discord.Attachment):
        await interaction.response.defer(ephemeral=True)
        
        # Check file type
        if not file.filename.endswith(('.xlsx', '.xls')):
            await interaction.followup.send("❌ Please upload an Excel file (.xlsx or .xls)", ephemeral=True)
            return
        
        try:
            # Download file
            file_data = await file.read()
            excel_file = io.BytesIO(file_data)
            
            teams_added = 0
            teams_updated = 0
            players_added = 0
            players_updated = 0
            players_deleted = 0
            errors = []
            duplicate_warnings = []
            
            async with aiosqlite.connect(DB_PATH) as db:
                # Import Teams
                teams_df = pd.read_excel(excel_file, sheet_name='Teams', dtype={'Role_ID': str, 'Emoji_ID': str, 'Channel_ID': str, 'Primary_Color': str, 'Secondary_Color': str})

                for _, row in teams_df.iterrows():
                    team_name = str(row['Team_Name']).strip()
                    role_id = str(row['Role_ID']).strip() if pd.notna(row['Role_ID']) and row['Role_ID'] else None
                    emoji_id = str(row['Emoji_ID']).strip() if pd.notna(row['Emoji_ID']) and row['Emoji_ID'] else None
                    channel_id = str(row['Channel_ID']).strip() if 'Channel_ID' in row and pd.notna(row['Channel_ID']) and row['Channel_ID'] else None
                    # Primary_Color/Secondary_Color are optional columns, absent
                    # from exports made before team colors existed - checked
                    # with 'in row' the same way Channel_ID is above, so an
                    # older export file can still be imported without error.
                    primary_color = str(row['Primary_Color']).strip() if 'Primary_Color' in row and pd.notna(row['Primary_Color']) and row['Primary_Color'] else None
                    secondary_color = str(row['Secondary_Color']).strip() if 'Secondary_Color' in row and pd.notna(row['Secondary_Color']) and row['Secondary_Color'] else None

                    cursor = await db.execute("SELECT team_id FROM teams WHERE team_name = ?", (team_name,))
                    existing = await cursor.fetchone()

                    if existing:
                        await db.execute(
                            "UPDATE teams SET role_id = ?, emoji_id = ?, channel_id = ?, color = ?, color_secondary = ? WHERE team_name = ?",
                            (role_id, emoji_id, channel_id, primary_color, secondary_color, team_name)
                        )
                        teams_updated += 1
                    else:
                        await db.execute(
                            "INSERT INTO teams (team_name, role_id, emoji_id, channel_id, color, color_secondary) VALUES (?, ?, ?, ?, ?, ?)",
                            (team_name, role_id, emoji_id, channel_id, primary_color, secondary_color)
                        )
                        teams_added += 1

                # Import Players (full replace, same as every other sheet: rows with a
                # Player_ID keep that exact ID so lineups/draft picks/etc. that reference
                # them stay linked; rows with Player_ID left blank become new players)
                players_df = pd.read_excel(excel_file, sheet_name='Players')

                # Get team mapping
                cursor = await db.execute("SELECT team_id, team_name FROM teams")
                teams = await cursor.fetchall()
                team_map = {name.lower(): id for id, name in teams}

                # Count existing players so we can report how many were removed
                # (any pre-existing player not carried over via a Player_ID in this sheet)
                cursor = await db.execute("SELECT COUNT(*) FROM players")
                players_before_count = (await cursor.fetchone())[0]

                # Clear existing players before importing the replacement set
                await db.execute("DELETE FROM players")

                for _, row in players_df.iterrows():
                    # Skip fully blank rows (e.g. a player's row was cleared rather than
                    # deleted) instead of trying to parse them as a real player record
                    if row.isna().all() or (not pd.notna(row.get('Name')) and not str(row.get('Name', '')).strip()):
                        continue

                    player_id = None
                    if 'Player_ID' in players_df.columns and pd.notna(row['Player_ID']):
                        player_id = int(row['Player_ID'])

                    name = str(row['Name']).strip()
                    position = str(row['Pos']).strip()
                    rating = int(row['OVR'])
                    age = int(row['Age'])

                    # Get birth_year if present, otherwise calculate from age
                    birth_year = None
                    if 'Birth_Year' in players_df.columns and pd.notna(row['Birth_Year']) and row['Birth_Year']:
                        birth_year = int(row['Birth_Year'])
                    else:
                        # Calculate birth_year from age
                        current_year = await get_current_year(db)
                        if current_year is None:
                            current_year = 1
                        birth_year = current_year - age

                    # Get contract_expiry if present
                    contract_expiry = None
                    if 'Contract_Expiry' in players_df.columns and pd.notna(row['Contract_Expiry']) and row['Contract_Expiry']:
                        contract_expiry = int(row['Contract_Expiry'])

                    # Validate position
                    is_valid, normalized_pos = validate_position(position)
                    if not is_valid:
                        errors.append(f"Player '{name}': Invalid position '{position}'")
                        continue

                    # Get team ID
                    team_id = None
                    if 'Team' in players_df.columns and pd.notna(row['Team']) and row['Team']:
                        team_name_lower = str(row['Team']).strip().lower()
                        team_id = team_map.get(team_name_lower)

                    # Get father/son club ID
                    father_son_club_id = None
                    if 'Father_Son_Club' in players_df.columns and pd.notna(row['Father_Son_Club']) and row['Father_Son_Club']:
                        fs_team_name_lower = str(row['Father_Son_Club']).strip().lower()
                        father_son_club_id = team_map.get(fs_team_name_lower)

                    # Get plays_like value
                    plays_like = None
                    if 'Plays_Like' in players_df.columns and pd.notna(row['Plays_Like']) and row['Plays_Like']:
                        plays_like = str(row['Plays_Like']).strip()

                    if player_id is not None:
                        # Re-insert with the same ID so existing lineups/draft picks/
                        # injuries/etc. that reference this player stay linked
                        await db.execute(
                            """INSERT INTO players (player_id, name, position, overall_rating, age, birth_year, team_id, contract_expiry, father_son_club_id, plays_like)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (player_id, name, normalized_pos, rating, age, birth_year, team_id, contract_expiry, father_son_club_id, plays_like)
                        )
                        players_updated += 1
                    else:
                        # No Player_ID - new player, let SQLite assign the next ID
                        await db.execute(
                            """INSERT INTO players (name, position, overall_rating, age, birth_year, team_id, contract_expiry, father_son_club_id, plays_like)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (name, normalized_pos, rating, age, birth_year, team_id, contract_expiry, father_son_club_id, plays_like)
                        )
                        players_added += 1

                # Any pre-existing player not carried over via a Player_ID in this sheet
                # was not re-inserted above, and so was effectively deleted
                players_deleted = max(0, players_before_count - players_updated)

                # Import Lineups (merged Current, Starting, and Submitted; full replace so
                # a row removed from the sheet is actually removed from the database)
                current_lineups_imported = 0
                starting_lineups_imported = 0
                lineups_df = pd.read_excel(excel_file, sheet_name='Lineups')

                await db.execute("DELETE FROM lineups")
                await db.execute("DELETE FROM starting_lineups")

                # Valid lineup positions with slot numbers (for current lineups)
                valid_lineup_positions = [
                    "LBP", "FB", "RBP", "LHB", "CHB", "RHB",
                    "LW", "C", "RW", "LHF", "CHF", "RHF",
                    "LFP", "FF", "RFP", "R", "RR", "RO",
                    "INT1", "INT2", "INT3", "INT4", "INT5"
                ]

                # Group starting lineups by team
                team_starting_lineups = {}

                for _, row in lineups_df.iterrows():
                    lineup_type = str(row['Type']).strip().lower()
                    team_name = str(row['Team_Name'])
                    position = str(row['Position']).strip()
                    player_id = int(row['Player_ID'])

                    # Verify player exists
                    cursor = await db.execute("SELECT player_id FROM players WHERE player_id = ?", (player_id,))
                    player = await cursor.fetchone()
                    if not player:
                        continue

                    # Get team ID
                    cursor = await db.execute("SELECT team_id FROM teams WHERE team_name = ?", (team_name,))
                    team = await cursor.fetchone()
                    if not team:
                        continue

                    if lineup_type == 'current':
                        position_upper = position.upper()
                        if position_upper in valid_lineup_positions:
                            slot_number = valid_lineup_positions.index(position_upper) + 1
                            await db.execute(
                                """INSERT OR REPLACE INTO lineups (team_id, player_id, slot_number, position_name)
                                   VALUES (?, ?, ?, ?)""",
                                (team[0], player_id, slot_number, position_upper)
                            )
                            current_lineups_imported += 1
                        else:
                            errors.append(f"Current lineup: Invalid position '{position}' for Player_ID {player_id}")

                    elif lineup_type == 'starting':
                        if team_name not in team_starting_lineups:
                            team_starting_lineups[team_name] = {}
                        team_starting_lineups[team_name][position] = player_id

                # Insert/update starting lineups for each team
                for team_name, lineup_dict in team_starting_lineups.items():
                    # Get team ID
                    cursor = await db.execute("SELECT team_id FROM teams WHERE team_name = ?", (team_name,))
                    team = await cursor.fetchone()

                    if team:
                        lineup_json = json.dumps(lineup_dict)
                        await db.execute(
                            """INSERT OR REPLACE INTO starting_lineups (team_id, lineup_data, last_updated)
                               VALUES (?, ?, CURRENT_TIMESTAMP)""",
                            (team[0], lineup_json)
                        )
                        starting_lineups_imported += 1

                # Import Seasons
                seasons_imported = 0
                seasons_df = pd.read_excel(excel_file, sheet_name='Seasons')
                for _, row in seasons_df.iterrows():
                    await db.execute(
                        """INSERT OR REPLACE INTO seasons
                           (season_number, current_round, regular_rounds, total_rounds, round_name, status)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (int(row['Season']), int(row['Current_Round']), int(row['Regular_Rounds']),
                         int(row['Total_Rounds']), str(row['Round_Name']), str(row['Status']))
                    )
                    seasons_imported += 1

                # Import Injuries (calculate recovery_rounds from injury_round and return_round)
                injuries_imported = 0
                injuries_df = pd.read_excel(excel_file, sheet_name='Injuries')

                # Clear existing injuries before importing to avoid duplicates
                await db.execute("DELETE FROM injuries")

                for _, row in injuries_df.iterrows():
                    # Find player by ID
                    player_id = int(row['Player_ID'])
                    cursor = await db.execute("SELECT player_id FROM players WHERE player_id = ?", (player_id,))
                    player = await cursor.fetchone()
                    if player:
                        injury_round = int(row['Injury_Round'])
                        # Return_Round can be blank - recovery length still
                        # TBC (see season_commands.py's
                        # _roll_pending_injury_recoveries), not yet rolled
                        # at export time. Preserve that through the
                        # round-trip rather than crashing on int(NaN).
                        if pd.notna(row['Return_Round']):
                            return_round = int(row['Return_Round'])
                            recovery_rounds = return_round - injury_round
                        else:
                            return_round = None
                            recovery_rounds = None
                        await db.execute(
                            """INSERT INTO injuries
                               (player_id, injury_type, injury_round, recovery_rounds, return_round, status)
                               VALUES (?, ?, ?, ?, ?, 'injured')""",
                            (player_id, str(row['Injury_Type']), injury_round,
                             recovery_rounds, return_round)
                        )
                        injuries_imported += 1

                # Import Suspensions
                suspensions_imported = 0
                suspensions_df = pd.read_excel(excel_file, sheet_name='Suspensions')

                # Clear existing suspensions before importing to avoid duplicates
                await db.execute("DELETE FROM suspensions")

                for _, row in suspensions_df.iterrows():
                    # Find player by ID
                    player_id = int(row['Player_ID'])
                    cursor = await db.execute("SELECT player_id FROM players WHERE player_id = ?", (player_id,))
                    player = await cursor.fetchone()
                    if player:
                        suspension_round = int(row['Suspension_Round'])
                        # Older export files predate Games_Missed/Games_Remaining
                        # (they had Return_Round instead) - fall back to
                        # deriving from Return_Round if present, else assume
                        # the suspension is fully unserved. A blank
                        # Games_Missed cell in a CURRENT-format file (the
                        # Games_Missed column exists but this row's value is
                        # NaN) means a report-driven suspension that was
                        # still TBC at export time (see
                        # season_commands.py's _roll_pending_report_suspensions)
                        # - preserved as NULL/NULL rather than coerced to 0,
                        # so it still needs rolling after import instead of
                        # silently reading as "already served".
                        games_missed_col_exists = 'Games_Missed' in suspensions_df.columns
                        if games_missed_col_exists and not pd.isna(row['Games_Missed']):
                            games_missed = int(row['Games_Missed'])
                        elif games_missed_col_exists:
                            games_missed = None
                        elif 'Return_Round' in suspensions_df.columns:
                            games_missed = int(row['Return_Round']) - suspension_round
                        else:
                            games_missed = 0

                        if games_missed is None:
                            games_remaining = None
                        elif 'Games_Remaining' in suspensions_df.columns and not pd.isna(row['Games_Remaining']):
                            games_remaining = int(row['Games_Remaining'])
                        else:
                            games_remaining = games_missed

                        await db.execute(
                            """INSERT INTO suspensions
                               (player_id, suspension_round, games_missed, games_remaining, suspension_reason, status)
                               VALUES (?, ?, ?, ?, ?, 'suspended')""",
                            (player_id, suspension_round, games_missed,
                             games_remaining, str(row['Reason']))
                        )
                        suspensions_imported += 1

                # Import Trades
                trades_imported = 0
                trades_df = pd.read_excel(excel_file, sheet_name='Trades', dtype={'Created_By_User_ID': str, 'Responded_By_User_ID': str, 'Approved_By_User_ID': str})

                # Clear existing trades
                await db.execute("DELETE FROM trades")
                for _, row in trades_df.iterrows():
                    # Get team IDs
                    cursor = await db.execute("SELECT team_id FROM teams WHERE team_name = ?", (str(row['Initiating_Team']),))
                    init_team = await cursor.fetchone()
                    cursor = await db.execute("SELECT team_id FROM teams WHERE team_name = ?", (str(row['Receiving_Team']),))
                    recv_team = await cursor.fetchone()

                    if init_team and recv_team:
                        original_trade_id = int(row['Original_Trade_ID']) if pd.notna(row['Original_Trade_ID']) and row['Original_Trade_ID'] else None
                        created_by = str(row['Created_By_User_ID']) if pd.notna(row['Created_By_User_ID']) and row['Created_By_User_ID'] else None
                        responded_by = str(row['Responded_By_User_ID']) if pd.notna(row['Responded_By_User_ID']) and row['Responded_By_User_ID'] else None
                        approved_by = str(row['Approved_By_User_ID']) if pd.notna(row['Approved_By_User_ID']) and row['Approved_By_User_ID'] else None
                        created_at = str(row['Created_At']) if pd.notna(row['Created_At']) and row['Created_At'] else None
                        responded_at = str(row['Responded_At']) if pd.notna(row['Responded_At']) and row['Responded_At'] else None
                        approved_at = str(row['Approved_At']) if pd.notna(row['Approved_At']) and row['Approved_At'] else None

                        # Initiating_Picks/Receiving_Picks may be absent in files exported
                        # before these columns were added - default to '' for older files
                        initiating_picks = str(row['Initiating_Picks']) if 'Initiating_Picks' in row and pd.notna(row['Initiating_Picks']) else ''
                        receiving_picks = str(row['Receiving_Picks']) if 'Receiving_Picks' in row and pd.notna(row['Receiving_Picks']) else ''

                        await db.execute(
                            """INSERT INTO trades
                               (trade_id, initiating_team_id, receiving_team_id, initiating_players, receiving_players,
                                initiating_picks, receiving_picks, status, created_at, responded_at, approved_at,
                                created_by_user_id, responded_by_user_id, approved_by_user_id, original_trade_id)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (int(row['Trade_ID']), init_team[0], recv_team[0], str(row['Initiating_Players']),
                             str(row['Receiving_Players']), initiating_picks, receiving_picks,
                             str(row['Status']), created_at, responded_at, approved_at, created_by, responded_by, approved_by, original_trade_id)
                        )
                        trades_imported += 1

                # Import Settings
                settings_imported = 0
                settings_df = pd.read_excel(excel_file, sheet_name='Settings', dtype={'Setting_Value': str})
                for _, row in settings_df.iterrows():
                    setting_value = str(row['Setting_Value']) if pd.notna(row['Setting_Value']) and row['Setting_Value'] else None
                    await db.execute(
                        """INSERT OR REPLACE INTO settings (setting_key, setting_value)
                           VALUES (?, ?)""",
                        (str(row['Setting_Key']), setting_value)
                    )
                    settings_imported += 1

                # Import Matches - fixtures as well as results. Rows WITH a
                # Match_ID keep that exact ID (so player_match_stats rows
                # that reference an already-simulated match stay linked);
                # rows with Match_ID left BLANK are added as new fixture
                # entries, same "blank ID = new row" convention as Players.
                # Home_Score/Away_Score/Simulated default to an unplayed
                # fixture (0/0/False) when left blank, so a new fixture row
                # only needs Season/Round/Home_Team/Away_Team filled in.
                matches_added = 0
                matches_updated = 0
                matches_df = pd.read_excel(excel_file, sheet_name='Matches')

                await db.execute("DELETE FROM matches")

                for _, row in matches_df.iterrows():
                    if row.isna().all():
                        continue

                    # Season/Round are required - no sensible default for a
                    # fixture row - so skip (rather than crash on int(NaN))
                    # if either is left blank.
                    if pd.isna(row.get('Season')) or pd.isna(row.get('Round')):
                        errors.append(f"Matches: row skipped - Season and Round are both required (Home_Team={row.get('Home_Team')}, Away_Team={row.get('Away_Team')})")
                        continue

                    cursor = await db.execute("SELECT season_id FROM seasons WHERE season_number = ?", (int(row['Season']),))
                    season = await cursor.fetchone()

                    cursor = await db.execute("SELECT team_id FROM teams WHERE team_name = ?", (str(row['Home_Team']),))
                    home_team = await cursor.fetchone()

                    cursor = await db.execute("SELECT team_id FROM teams WHERE team_name = ?", (str(row['Away_Team']),))
                    away_team = await cursor.fetchone()

                    if not (season and home_team and away_team):
                        problems = []
                        if not season:
                            problems.append(f"Season '{row['Season']}' not found")
                        if not home_team:
                            problems.append(f"Home_Team '{row['Home_Team']}' not found")
                        if not away_team:
                            problems.append(f"Away_Team '{row['Away_Team']}' not found")
                        errors.append(f"Matches: row skipped - {'; '.join(problems)}")
                        continue

                    home_score = int(row['Home_Score']) if 'Home_Score' in matches_df.columns and pd.notna(row['Home_Score']) else 0
                    away_score = int(row['Away_Score']) if 'Away_Score' in matches_df.columns and pd.notna(row['Away_Score']) else 0
                    simulated = int(bool(row['Simulated'])) if 'Simulated' in matches_df.columns and pd.notna(row['Simulated']) else 0

                    match_id = None
                    if 'Match_ID' in matches_df.columns and pd.notna(row['Match_ID']):
                        match_id = int(row['Match_ID'])

                    if match_id is not None:
                        await db.execute(
                            """INSERT INTO matches
                               (match_id, season_id, round_number, home_team_id, away_team_id, home_score, away_score, simulated)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            (match_id, season[0], int(row['Round']), home_team[0], away_team[0],
                             home_score, away_score, simulated)
                        )
                        matches_updated += 1
                    else:
                        await db.execute(
                            """INSERT INTO matches
                               (season_id, round_number, home_team_id, away_team_id, home_score, away_score, simulated)
                               VALUES (?, ?, ?, ?, ?, ?, ?)""",
                            (season[0], int(row['Round']), home_team[0], away_team[0],
                             home_score, away_score, simulated)
                        )
                        matches_added += 1

                matches_imported = matches_added + matches_updated

                # Import Player_Match_Stats - full replace, same as every
                # other sheet (Injuries/Suspensions/etc): the sheet's
                # contents entirely replace what's in the table. No ID
                # preservation needed (nothing references stat_id as a FK),
                # so this is just DELETE then re-insert whatever rows are
                # present, resolving Match_ID/Player_ID/Team. Since Matches
                # was just fully replaced above, this also means reimporting
                # an OLD Matches sheet alongside an OLD (or empty)
                # Player_Match_Stats sheet naturally resets accumulated
                # match stats back to that snapshot too - matches this
                # sheet's real use case (undoing a round of test-season
                # simulation by reimporting an earlier data file).
                player_match_stats_imported = 0
                player_match_stats_df = pd.read_excel(excel_file, sheet_name='Player_Match_Stats')

                await db.execute("DELETE FROM player_match_stats")

                for _, row in player_match_stats_df.iterrows():
                    if row.isna().all():
                        continue

                    if pd.isna(row.get('Match_ID')) or pd.isna(row.get('Player_ID')):
                        errors.append(f"Player_Match_Stats: row skipped - Match_ID and Player_ID are both required (Player_Name={row.get('Player_Name')})")
                        continue

                    match_id = int(row['Match_ID'])
                    player_id = int(row['Player_ID'])

                    cursor = await db.execute("SELECT 1 FROM matches WHERE match_id = ?", (match_id,))
                    match_exists = await cursor.fetchone()
                    cursor = await db.execute("SELECT team_id FROM teams WHERE team_name = ?", (str(row.get('Team')),))
                    team = await cursor.fetchone()

                    if not (match_exists and team):
                        problems = []
                        if not match_exists:
                            problems.append(f"Match_ID {match_id} not found")
                        if not team:
                            problems.append(f"Team '{row.get('Team')}' not found")
                        errors.append(f"Player_Match_Stats: row skipped - {'; '.join(problems)}")
                        continue

                    disposals = int(row['Disposals']) if 'Disposals' in player_match_stats_df.columns and pd.notna(row['Disposals']) else 0
                    goals = int(row['Goals']) if 'Goals' in player_match_stats_df.columns and pd.notna(row['Goals']) else 0
                    behinds = int(row['Behinds']) if 'Behinds' in player_match_stats_df.columns and pd.notna(row['Behinds']) else 0
                    marks = int(row['Marks']) if 'Marks' in player_match_stats_df.columns and pd.notna(row['Marks']) else 0
                    tackles = int(row['Tackles']) if 'Tackles' in player_match_stats_df.columns and pd.notna(row['Tackles']) else 0
                    spoils = int(row['Spoils']) if 'Spoils' in player_match_stats_df.columns and pd.notna(row['Spoils']) else 0
                    hitouts = int(row['Hitouts']) if 'Hitouts' in player_match_stats_df.columns and pd.notna(row['Hitouts']) else 0
                    # Older export files predate Brownlow_Votes/Best_Fairest_Votes -
                    # default to 0 rather than failing the import.
                    brownlow_votes = int(row['Brownlow_Votes']) if 'Brownlow_Votes' in player_match_stats_df.columns and pd.notna(row['Brownlow_Votes']) else 0
                    best_fairest_votes = int(row['Best_Fairest_Votes']) if 'Best_Fairest_Votes' in player_match_stats_df.columns and pd.notna(row['Best_Fairest_Votes']) else 0

                    await db.execute(
                        """INSERT INTO player_match_stats
                           (match_id, player_id, team_id, disposals, goals, behinds, marks, tackles, spoils, hitouts, brownlow_votes, best_fairest_votes)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (match_id, player_id, team[0], disposals, goals, behinds, marks, tackles, spoils, hitouts, brownlow_votes, best_fairest_votes)
                    )
                    player_match_stats_imported += 1

                # Import Drafts (full replace, same as Players/Draft_Picks: rows with a
                # Draft_ID keep that exact ID so draft_picks stay linked; rows with
                # Draft_ID left blank become new drafts; omitted rows are deleted)
                drafts_imported = 0
                drafts_df = pd.read_excel(excel_file, sheet_name='Drafts')

                await db.execute("DELETE FROM drafts")

                for _, row in drafts_df.iterrows():
                    # Skip fully blank rows
                    if row.isna().all() or not str(row.get('Draft_Name', '')).strip():
                        continue

                    draft_id = int(row['Draft_ID']) if pd.notna(row['Draft_ID']) else None
                    draft_name = str(row['Draft_Name']).strip()
                    season_number = int(row['Season_Number']) if pd.notna(row['Season_Number']) else None
                    status = str(row['Status']) if pd.notna(row['Status']) and row['Status'] else 'future'
                    rounds = int(row['Rounds']) if pd.notna(row['Rounds']) else 4
                    rookie_contract_years = int(row['Rookie_Contract_Years']) if pd.notna(row['Rookie_Contract_Years']) else 3
                    created_at = str(row['Created_At']) if pd.notna(row['Created_At']) and row['Created_At'] else None
                    ladder_set_at = str(row['Ladder_Set_At']) if pd.notna(row['Ladder_Set_At']) and row['Ladder_Set_At'] else None
                    started_at = str(row['Started_At']) if 'Started_At' in row and pd.notna(row['Started_At']) and row['Started_At'] else None
                    completed_at = str(row['Completed_At']) if 'Completed_At' in row and pd.notna(row['Completed_At']) and row['Completed_At'] else None
                    current_pick_number = int(row['Current_Pick_Number']) if 'Current_Pick_Number' in row and pd.notna(row['Current_Pick_Number']) else 0

                    if draft_id is not None:
                        # Re-insert with the same ID so draft_picks referencing this draft stay linked
                        await db.execute(
                            """INSERT INTO drafts
                               (draft_id, draft_name, season_number, status, rounds, rookie_contract_years,
                                created_at, ladder_set_at, started_at, completed_at, current_pick_number)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (draft_id, draft_name, season_number, status, rounds, rookie_contract_years,
                             created_at, ladder_set_at, started_at, completed_at, current_pick_number)
                        )
                    else:
                        # No Draft_ID - new draft, let SQLite assign the next ID
                        await db.execute(
                            """INSERT INTO drafts
                               (draft_name, season_number, status, rounds, rookie_contract_years,
                                created_at, ladder_set_at, started_at, completed_at, current_pick_number)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (draft_name, season_number, status, rounds, rookie_contract_years,
                             created_at, ladder_set_at, started_at, completed_at, current_pick_number)
                        )
                    drafts_imported += 1

                # Import Draft Picks
                draft_picks_imported = 0
                draft_picks_df = pd.read_excel(excel_file, sheet_name='Draft_Picks')

                # Clear existing draft picks before importing to avoid duplicates
                await db.execute("DELETE FROM draft_picks")

                for _, row in draft_picks_df.iterrows():
                    # Get current team ID
                    cursor = await db.execute("SELECT team_id FROM teams WHERE team_name = ?", (str(row['Current_Team']),))
                    current_team = await cursor.fetchone()

                    # Parse original_team_id from pick_origin
                    original_team_id = None
                    pick_origin = str(row['Pick_Origin']) if pd.notna(row['Pick_Origin']) and row['Pick_Origin'] else ''
                    if pick_origin:
                        # Parse pick_origin format: "Team Name R1" or "Team Name F/S Match"
                        if ' R' in pick_origin:
                            team_name = pick_origin.split(' R')[0]
                        elif ' F/S' in pick_origin:
                            team_name = pick_origin.split(' F/S')[0]
                        else:
                            team_name = None

                        if team_name:
                            cursor = await db.execute("SELECT team_id FROM teams WHERE team_name = ?", (team_name,))
                            orig_team = await cursor.fetchone()
                            if orig_team:
                                original_team_id = orig_team[0]

                    # Fallback to current team if pick_origin parsing failed
                    if not original_team_id:
                        original_team_id = current_team[0] if current_team else None

                    # Get player ID if selected
                    player_id = None
                    if pd.notna(row['Player_ID']) and row['Player_ID']:
                        player_id = int(row['Player_ID'])
                        # Verify player exists
                        cursor = await db.execute("SELECT player_id FROM players WHERE player_id = ?", (player_id,))
                        player = await cursor.fetchone()
                        if not player:
                            player_id = None

                    # Handle NaN values for numeric fields
                    pick_id = int(row['Pick_ID']) if pd.notna(row['Pick_ID']) else None
                    round_number = int(row['Round']) if pd.notna(row['Round']) else None
                    pick_number = int(row['Pick']) if pd.notna(row['Pick']) else None
                    draft_name = str(row['Draft_Name']) if pd.notna(row['Draft_Name']) else ''

                    # Get season_number from draft_name if possible (format: "Season X National Draft")
                    season_number = None
                    if 'Season' in draft_name:
                        try:
                            # Extract season number from draft name (e.g., "Season 9 National Draft")
                            season_str = draft_name.split('Season')[1].split()[0]
                            # Draft is for season_number + 1 (Season 9 Draft is for Season 10)
                            season_number = int(season_str) + 1
                        except Exception:
                            pass

                    # Get or create draft_id
                    draft_id = None
                    if draft_name:
                        cursor = await db.execute(
                            "SELECT draft_id FROM drafts WHERE draft_name = ?",
                            (draft_name,)
                        )
                        draft_result = await cursor.fetchone()

                        if draft_result:
                            draft_id = draft_result[0]
                        else:
                            # Create draft if it doesn't exist
                            # Determine status based on whether pick_number is set
                            draft_status = 'current' if pick_number is not None else 'future'
                            cursor = await db.execute(
                                """INSERT INTO drafts (draft_name, season_number, status, rounds)
                                   VALUES (?, ?, ?, 4)""",
                                (draft_name, season_number, draft_status)
                            )
                            draft_id = cursor.lastrowid

                    if current_team and pick_id and draft_id:
                        # Get passed and picked_at if they exist
                        passed = int(row['Passed']) if 'Passed' in row and pd.notna(row['Passed']) else 0
                        picked_at = str(row['Picked_At']) if 'Picked_At' in row and pd.notna(row['Picked_At']) and row['Picked_At'] else None

                        # Use 0 for season_number if it's NULL/empty (manual drafts)
                        season_num = season_number if pd.notna(season_number) and season_number else 0

                        await db.execute(
                            """INSERT INTO draft_picks
                               (pick_id, draft_id, draft_name, season_number, round_number, pick_number,
                                pick_origin, original_team_id, current_team_id, player_selected_id, passed, picked_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (pick_id, draft_id, draft_name, season_num, round_number, pick_number,
                             pick_origin, original_team_id, current_team[0], player_id, passed, picked_at)
                        )
                        draft_picks_imported += 1

                # Import Ladder Positions
                ladder_positions_imported = 0
                ladder_positions_df = pd.read_excel(excel_file, sheet_name='Ladder_Positions')

                # Clear existing ladder positions
                await db.execute("DELETE FROM ladder_positions")

                for _, row in ladder_positions_df.iterrows():
                    # Get season ID
                    cursor = await db.execute("SELECT season_id FROM seasons WHERE season_number = ?", (int(row['Season']),))
                    season = await cursor.fetchone()

                    # Get team ID
                    cursor = await db.execute("SELECT team_id FROM teams WHERE team_name = ?", (str(row['Team']),))
                    team = await cursor.fetchone()

                    if season and team:
                        await db.execute(
                            """INSERT INTO ladder_positions
                               (ladder_id, season_id, team_id, position)
                               VALUES (?, ?, ?, ?)""",
                            (int(row['Ladder_ID']), season[0], team[0], int(row['Position']))
                        )
                        ladder_positions_imported += 1

                # Import Compensation Chart (2D table format with individual ages/OVRs)
                compensation_chart_imported = 0
                compensation_chart_df = pd.read_excel(excel_file, sheet_name='Compensation_Chart')

                # Clear existing compensation chart
                await db.execute("DELETE FROM compensation_chart")

                # Parse 2D table: first column is ages, other columns are individual OVRs
                age_col = compensation_chart_df.columns[0]  # Should be "Age \ OVR" or similar
                ovr_cols = compensation_chart_df.columns[1:]  # All other columns are OVR values

                # Build a map of (age, ovr) -> band
                cell_map = {}
                for _, row in compensation_chart_df.iterrows():
                    age_str = str(row[age_col]).strip()
                    if not age_str or age_str == '' or age_str == 'nan':
                        continue

                    age = int(float(age_str))  # Convert through float first to handle "19.0" format

                    # Process each OVR column
                    for ovr_col in ovr_cols:
                        band_value = row[ovr_col]
                        if pd.isna(band_value) or band_value == '':
                            continue

                        band = int(float(band_value))  # Convert through float first
                        # Column name might be int or string
                        try:
                            ovr = int(ovr_col)
                        except Exception:
                            ovr = int(float(str(ovr_col)))  # Handle string column names
                        cell_map[(age, ovr)] = band

                # Check if we parsed any data
                if not cell_map:
                    errors.append("Compensation Chart: No valid data found in sheet. Check that cells contain numeric values for bands.")

                # Group consecutive cells with same band into ranges
                # Process by band number
                bands = set(cell_map.values())
                for band in sorted(bands):
                    # Get all cells for this band
                    band_cells = {k for k, v in cell_map.items() if v == band}

                    # Group by age, then find consecutive OVR ranges
                    age_groups = {}
                    for age, ovr in band_cells:
                        if age not in age_groups:
                            age_groups[age] = []
                        age_groups[age].append(ovr)

                    # For each age, find consecutive OVR ranges
                    for age, ovrs in age_groups.items():
                        ovrs = sorted(ovrs)
                        # Find consecutive ranges
                        ranges = []
                        start = ovrs[0]
                        prev = ovrs[0]

                        for ovr in ovrs[1:]:
                            if ovr == prev + 1:
                                prev = ovr
                            else:
                                ranges.append((start, prev))
                                start = ovr
                                prev = ovr
                        ranges.append((start, prev))

                        # Insert each range
                        for min_ovr, max_ovr in ranges:
                            await db.execute(
                                """INSERT INTO compensation_chart (min_age, max_age, min_ovr, max_ovr, compensation_band)
                                   VALUES (?, ?, ?, ?, ?)""",
                                (age, age, min_ovr, max_ovr if max_ovr != min_ovr else None, band)
                            )
                            compensation_chart_imported += 1

                # Import Contract Config
                contract_config_imported = 0
                contract_config_df = pd.read_excel(excel_file, sheet_name='Contract_Config')

                # Clear existing contract config
                await db.execute("DELETE FROM contract_config")

                for _, row in contract_config_df.iterrows():
                    # Skip empty rows
                    if pd.isna(row['Min_Age']) or row['Min_Age'] == '':
                        continue

                    min_age = int(row['Min_Age'])
                    # Blank Max_Age means "no upper bound" - use 99 rather than NULL so the
                    # UNIQUE(min_age, max_age) constraint can actually detect duplicates
                    max_age = int(row['Max_Age']) if pd.notna(row['Max_Age']) and row['Max_Age'] != '' else 99
                    contract_years = int(row['Contract_Years'])

                    await db.execute(
                        """INSERT INTO contract_config (min_age, max_age, contract_years)
                           VALUES (?, ?, ?)""",
                        (min_age, max_age, contract_years)
                    )
                    contract_config_imported += 1

                # Import Draft Value Index
                draft_value_index_imported = 0
                draft_value_index_df = pd.read_excel(excel_file, sheet_name='Draft_Value_Index')

                # Clear existing draft value index
                await db.execute("DELETE FROM draft_value_index")

                for _, row in draft_value_index_df.iterrows():
                    # Skip empty rows
                    if pd.isna(row['Pick_Number']) or row['Pick_Number'] == '':
                        continue

                    pick_number = int(row['Pick_Number'])
                    points_value = int(row['Points_Value'])

                    await db.execute(
                        """INSERT INTO draft_value_index (pick_number, points_value)
                           VALUES (?, ?)""",
                        (pick_number, points_value)
                    )
                    draft_value_index_imported += 1

                # Note: the free agency period is now imported via the Settings sheet
                # (fa_period_status / fa_period_season / fa_period_auction_points)

                # Import Free Agency Bids (optional - clears existing bids)
                free_agency_bids_imported = 0
                free_agency_bids_df = pd.read_excel(excel_file, sheet_name='Free_Agency_Bids')

                # Clear existing free agency bids
                await db.execute("DELETE FROM free_agency_bids")

                for _, row in free_agency_bids_df.iterrows():
                    if pd.isna(row['Bid_ID']) or not row['Bid_ID']:
                        continue

                    bid_id = int(row['Bid_ID'])
                    season_number = int(row['Season_Number'])
                    team_name = str(row['Team'])
                    player_id = int(row['Player_ID'])
                    bid_amount = int(row['Bid_Amount'])
                    status = str(row['Status'])
                    placed_at = str(row['Placed_At']) if pd.notna(row['Placed_At']) and row['Placed_At'] else None

                    # Get team_id from name and verify player_id exists
                    cursor = await db.execute("SELECT team_id FROM teams WHERE team_name = ?", (team_name,))
                    team = await cursor.fetchone()
                    if not team:
                        errors.append(f"Free Agency Bids: Team '{team_name}' not found")
                        continue
                    team_id = team[0]

                    cursor = await db.execute("SELECT player_id FROM players WHERE player_id = ?", (player_id,))
                    player = await cursor.fetchone()
                    if not player:
                        errors.append(f"Free Agency Bids: Player_ID '{player_id}' not found")
                        continue

                    await db.execute(
                        """INSERT INTO free_agency_bids (bid_id, season_number, team_id, player_id, bid_amount, status, placed_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (bid_id, season_number, team_id, player_id, bid_amount, status, placed_at)
                    )
                    free_agency_bids_imported += 1

                # Import Free Agency Re-Signs
                free_agency_resigns_imported = 0
                free_agency_resigns_df = pd.read_excel(excel_file, sheet_name='Free_Agency_Re-Signs')

                # Clear existing free agency re-signs
                await db.execute("DELETE FROM free_agency_resigns")

                for _, row in free_agency_resigns_df.iterrows():
                    if pd.isna(row['Resign_ID']) or not row['Resign_ID']:
                        continue

                    resign_id = int(row['Resign_ID'])
                    season_number = int(row['Season_Number'])
                    team_name = str(row['Team'])
                    player_id = int(row['Player_ID'])
                    confirmed = int(row['Confirmed']) if pd.notna(row['Confirmed']) else 0
                    confirmed_at = str(row['Confirmed_At']) if pd.notna(row['Confirmed_At']) and row['Confirmed_At'] else None

                    # Get team_id from name and verify player_id exists
                    cursor = await db.execute("SELECT team_id FROM teams WHERE team_name = ?", (team_name,))
                    team = await cursor.fetchone()
                    if not team:
                        errors.append(f"Free Agency Re-Sign: Team '{team_name}' not found")
                        continue
                    team_id = team[0]

                    cursor = await db.execute("SELECT player_id FROM players WHERE player_id = ?", (player_id,))
                    player = await cursor.fetchone()
                    if not player:
                        errors.append(f"Free Agency Re-Sign: Player_ID '{player_id}' not found")
                        continue

                    await db.execute(
                        """INSERT INTO free_agency_resigns (resign_id, season_number, team_id, player_id, confirmed, confirmed_at)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (resign_id, season_number, team_id, player_id, confirmed, confirmed_at)
                    )
                    free_agency_resigns_imported += 1

                # Import Free Agency Results
                free_agency_results_imported = 0
                free_agency_results_df = pd.read_excel(excel_file, sheet_name='Free_Agency_Results')

                # Clear existing free agency results
                await db.execute("DELETE FROM free_agency_results")

                for _, row in free_agency_results_df.iterrows():
                    if pd.isna(row['Result_ID']) or not row['Result_ID']:
                        continue

                    result_id = int(row['Result_ID'])
                    season_number = int(row['Season_Number'])
                    player_id = int(row['Player_ID'])
                    original_team_name = str(row['Original_Team'])
                    winning_team_name = str(row['Winning_Team']) if pd.notna(row['Winning_Team']) and row['Winning_Team'] else None
                    winning_bid = int(row['Winning_Bid']) if pd.notna(row['Winning_Bid']) and row['Winning_Bid'] else None
                    matched = int(row['Matched']) if pd.notna(row['Matched']) else 0
                    compensation_band = int(row['Compensation_Band']) if pd.notna(row['Compensation_Band']) and row['Compensation_Band'] else None
                    confirmed_at = str(row['Confirmed_At']) if pd.notna(row['Confirmed_At']) and row['Confirmed_At'] else None

                    # Verify player_id exists and get team IDs from names
                    cursor = await db.execute("SELECT player_id FROM players WHERE player_id = ?", (player_id,))
                    player = await cursor.fetchone()
                    if not player:
                        errors.append(f"Free Agency Results: Player_ID '{player_id}' not found")
                        continue

                    cursor = await db.execute("SELECT team_id FROM teams WHERE team_name = ?", (original_team_name,))
                    orig_team = await cursor.fetchone()
                    if not orig_team:
                        errors.append(f"Free Agency Results: Original team '{original_team_name}' not found")
                        continue
                    original_team_id = orig_team[0]

                    winning_team_id = None
                    if winning_team_name:
                        cursor = await db.execute("SELECT team_id FROM teams WHERE team_name = ?", (winning_team_name,))
                        win_team = await cursor.fetchone()
                        if win_team:
                            winning_team_id = win_team[0]

                    await db.execute(
                        """INSERT INTO free_agency_results
                           (result_id, season_number, player_id, original_team_id, winning_team_id, winning_bid, matched, compensation_band, confirmed_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (result_id, season_number, player_id, original_team_id, winning_team_id, winning_bid, matched, compensation_band, confirmed_at)
                    )
                    free_agency_results_imported += 1

                # All sheets processed successfully with no errors - commit the entire
                # import as a single atomic transaction. If anything above raised, this
                # line is never reached and aiosqlite rolls back all uncommitted writes
                # automatically when the "async with" block exits via exception.
                await db.commit()

            # Build response
            response = "✅ **Import Complete!**\n\n"
            response += f"**Teams:** {teams_added} added, {teams_updated} updated\n"
            response += f"**Players:** {players_added} added, {players_updated} updated, {players_deleted} deleted\n"
            if current_lineups_imported > 0:
                response += f"**Current Lineups:** {current_lineups_imported} imported\n"
            if starting_lineups_imported > 0:
                response += f"**Starting Lineups:** {starting_lineups_imported} teams imported\n"
            if seasons_imported > 0:
                response += f"**Seasons:** {seasons_imported} imported\n"
            if injuries_imported > 0:
                response += f"**Injuries:** {injuries_imported} imported\n"
            if suspensions_imported > 0:
                response += f"**Suspensions:** {suspensions_imported} imported\n"
            if drafts_imported > 0:
                response += f"**Drafts:** {drafts_imported} updated\n"
            if draft_picks_imported > 0:
                response += f"**Draft Picks:** {draft_picks_imported} imported\n"
            if ladder_positions_imported > 0:
                response += f"**Ladder Positions:** {ladder_positions_imported} imported\n"
            if trades_imported > 0:
                response += f"**Trades:** {trades_imported} imported\n"
            if matches_imported > 0:
                response += f"**Matches:** {matches_added} added, {matches_updated} updated\n"
            if player_match_stats_imported > 0:
                response += f"**Player Match Stats:** {player_match_stats_imported} imported\n"
            if settings_imported > 0:
                response += f"**Settings:** {settings_imported} imported\n"
            if compensation_chart_imported > 0:
                response += f"**Compensation Chart:** {compensation_chart_imported} entries imported\n"
            if contract_config_imported > 0:
                response += f"**Contract Config:** {contract_config_imported} entries imported\n"
            if draft_value_index_imported > 0:
                response += f"**Draft Value Index:** {draft_value_index_imported} entries imported\n"
            if free_agency_bids_imported > 0:
                response += f"**Free Agency Bids:** {free_agency_bids_imported} imported\n"
            if free_agency_resigns_imported > 0:
                response += f"**Free Agency Re-Signs:** {free_agency_resigns_imported} imported\n"
            if free_agency_results_imported > 0:
                response += f"**Free Agency Results:** {free_agency_results_imported} imported\n"

            if duplicate_warnings:
                response += f"\n⚠️ **{len(duplicate_warnings)} Duplicate Name Warning(s):**\n"
                response += "\n".join(duplicate_warnings[:10])  # Show first 10 warnings
                if len(duplicate_warnings) > 10:
                    response += f"\n... and {len(duplicate_warnings) - 10} more"

            if errors:
                response += f"\n❌ **{len(errors)} Errors:**\n"
                response += "\n".join(errors[:10])  # Show first 10 errors
                if len(errors) > 10:
                    response += f"\n... and {len(errors) - 10} more"

            await interaction.followup.send(response, ephemeral=True)
            
        except Exception as e:
            await interaction.followup.send(
                f"❌ **Import failed and was rolled back - no changes were made:**\n{e}",
                ephemeral=True
            )

    @app_commands.command(name="exportdb", description="Export database file (Admin only)")
    async def export_db(self, interaction: discord.Interaction):
        """Export the database file for download"""
        await interaction.response.defer(ephemeral=True)

        try:
            # Create a discord.File from the database
            db_file = discord.File(DB_PATH, filename="affl_bot.db")

            await interaction.followup.send(
                "📦 Here's your database file:",
                file=db_file,
                ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Error exporting database: {e}", ephemeral=True)


async def setup(bot):
    await bot.add_cog(AdminCommands(bot))