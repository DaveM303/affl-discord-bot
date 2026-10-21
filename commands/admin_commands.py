import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
import asyncio
import io
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
import db_transfer
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
        """Autocomplete for player names with format: Name (Team, POS, age, OVR).
        Admins only: the cog's interaction_check doesn't run for autocomplete
        (discord.py only checks when the command is actually used), and these
        commands are visible to everyone - so without this, any member typing
        /updateplayer or /removeplayer would see every player's OVR, including
        Draft Pool prospects whose ratings are hidden until they're drafted."""
        if not await is_admin_user(interaction):
            return []
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

    async def father_son_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Teams a player can be a father-son selection for (every team but
        the Draft Pool), plus "none" to clear it in /updateplayer."""
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT team_name FROM teams WHERE team_name != 'Draft Pool' ORDER BY team_name"
            )
            teams = [row[0] for row in await cursor.fetchall()]
        choices = [app_commands.Choice(name=t, value=t) for t in teams if current.lower() in t.lower()]
        if current.lower() in "none":
            choices.insert(0, app_commands.Choice(name="none", value="none"))
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

    @staticmethod
    async def find_father_son_club(db, text):
        """(team_id, team_name) for a father-son club typed or picked from
        the autocomplete - exact name first, then a unique partial match -
        or None. The Draft Pool is never a club."""
        cursor = await db.execute(
            "SELECT team_id, team_name FROM teams WHERE team_name != 'Draft Pool' ORDER BY team_name"
        )
        teams = await cursor.fetchall()
        exact = [t for t in teams if t[1].lower() == text.strip().lower()]
        if exact:
            return exact[0]
        partial = [t for t in teams if text.strip().lower() in t[1].lower()]
        return partial[0] if len(partial) == 1 else None

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
        admin_controls_channel="Channel for admin control panels (live matches and live drafts)",
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
        admin_controls_channel: discord.TextChannel = None,
        live_match_feed_channel: discord.TextChannel = None,
        results_channel: discord.TextChannel = None,
        ladder_channel: discord.TextChannel = None,
        season_1_year: int = None,
        result_delay_seconds: app_commands.Range[int, 0, 300] = None
    ):
        # If no parameters provided, show current settings
        if all(ch is None for ch in [lineups_channel, delist_log_channel, trade_approval_channel, trade_log_channel, auctions_log_channel, bot_logs_channel, draft_channel, injury_list_channel, admin_controls_channel, live_match_feed_channel, results_channel, ladder_channel, season_1_year, result_delay_seconds]):
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

            # Admin Controls Channel (live match and live draft control panels)
            if 'live_match_control_channel_id' in settings and settings['live_match_control_channel_id']:
                channel = interaction.guild.get_channel(int(settings['live_match_control_channel_id']))
                channel_display = channel.mention if channel else f"<#{settings['live_match_control_channel_id']}> (channel not found)"
            else:
                channel_display = "*Not set*"
            embed.add_field(name="Admin Controls Channel", value=channel_display, inline=False)

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

            if admin_controls_channel:
                await db.execute(
                    "INSERT OR REPLACE INTO settings (setting_key, setting_value) VALUES (?, ?)",
                    ("live_match_control_channel_id", str(admin_controls_channel.id))
                )
                updates.append(f"Admin Controls Channel → {admin_controls_channel.mention}")

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
        contract_expiry="Contract expiry season (optional)",
        plays_like="Who they play like, e.g. Dustin Martin (optional)",
        father_son_club="Club they're a father-son selection for (optional)"
    )
    @app_commands.autocomplete(position=position_autocomplete, father_son_club=father_son_autocomplete)
    async def add_player(
        self,
        interaction: discord.Interaction,
        name: str,
        position: str,
        rating: int,
        age: int,
        team_name: str = None,
        contract_expiry: int = None,
        plays_like: str = None,
        father_son_club: str = None
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

            father_son = None
            if father_son_club and father_son_club.strip().lower() != "none":
                father_son = await self.find_father_son_club(db, father_son_club)
                if father_son is None:
                    await interaction.response.send_message(
                        f"❌ No single team matches father-son club '{father_son_club}'",
                        ephemeral=True
                    )
                    return
            plays_like = plays_like.strip() if plays_like and plays_like.strip() else None

            # Calculate birth_year from age
            current_year = await get_current_year(db)
            if current_year is None:
                current_year = 1
            birth_year = current_year - age

            # Add player
            await db.execute(
                """INSERT INTO players (name, position, overall_rating, age, birth_year, team_id, contract_expiry,
                                        plays_like, father_son_club_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (name, normalized_pos, rating, age, birth_year, team_id, contract_expiry,
                 plays_like, father_son[0] if father_son else None)
            )
            await db.commit()

            team_text = f"to **{team_name}**" if team_name else "as delisted"
            contract_text = f", contract expires Season {contract_expiry}" if contract_expiry else ""
            success_msg = f"✅ Added **{name}** ({normalized_pos}, {rating} OVR, {age}yo{contract_text}) {team_text}!"
            if plays_like:
                success_msg += f"\nPlays like *{plays_like}*"
            if father_son:
                success_msg += f"\nFather-son: **{father_son[1]}**"

            await interaction.response.send_message(success_msg)

    @app_commands.command(name="importplayers", description="[ADMIN] Add many players at once from a CSV file")
    @app_commands.describe(file="A CSV of players - leave empty to get a template")
    async def import_players(self, interaction: discord.Interaction, file: discord.Attachment = None):
        """Bulk-adds players from a CSV - a draft class into the Draft Pool,
        or players straight onto teams or the delisted list. Without a file,
        replies with a template. With one, every row is checked first and a
        preview must be confirmed before anything is saved."""
        if file is None:
            await interaction.response.send_message(
                "**Importing players**\n"
                "1. Open this template in Excel or Google Sheets and add one row per player.\n"
                "2. **Name, Position, OVR, Age** are required. **Team** is the team's name, `Draft Pool` for "
                "draft prospects, or blank for delisted. **Contract Expiry** (a season number), **Plays Like** "
                "and **Father-Son Club** are optional.\n"
                "3. Save it as **CSV** (Excel: Save As > CSV; Google Sheets: File > Download > CSV) and run "
                "`/importplayers` again with the file attached.\n\n"
                f"Positions: {get_positions_string()}",
                file=discord.File(io.BytesIO(PLAYER_IMPORT_TEMPLATE.encode("utf-8")), filename="players_template.csv"),
                ephemeral=True,
            )
            return
        if file.size > PLAYER_IMPORT_MAX_BYTES:
            await interaction.response.send_message("❌ That file is too big for a player list (max 1 MB).",
                                                    ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        data = await file.read()
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT team_id, team_name FROM teams ORDER BY team_name")
            teams = await cursor.fetchall()
            players, errors = parse_players_csv(data, teams)
            cursor = await db.execute(
                """SELECT lower(p.name), COALESCE(t.team_name, 'Delisted') FROM players p
                   LEFT JOIN teams t ON p.team_id = t.team_id"""
            )
            existing = {}
            for name, team_name in await cursor.fetchall():
                existing.setdefault(name, team_name)

        view = PlayerImportView(interaction.user.id, players, errors, existing)
        await interaction.followup.send(embed=view.embed(file.filename), view=view, ephemeral=True)

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
        contract_expiry="Contract expiry season (optional)",
        plays_like="Who they play like (optional, 'none' to clear)",
        father_son_club="Father-son club (optional, 'none' to clear)"
    )
    @app_commands.autocomplete(position=position_autocomplete, name=player_name_autocomplete, team=team_autocomplete,
                               father_son_club=father_son_autocomplete)
    async def update_player(
        self,
        interaction: discord.Interaction,
        name: str,
        new_name: str = None,
        ovr: int = None,
        age: int = None,
        position: str = None,
        team: str = None,
        contract_expiry: int = None,
        plays_like: str = None,
        father_son_club: str = None
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
                """SELECT p.player_id, p.name, p.overall_rating, p.age, p.position, p.team_id, p.contract_expiry,
                          p.plays_like, fs.team_name
                   FROM players p LEFT JOIN teams fs ON p.father_son_club_id = fs.team_id
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

            (player_id, player_name, old_rating, old_age, old_position, old_team_id, old_contract_expiry,
             old_plays_like, old_father_son_name) = player

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

            if plays_like is not None:
                new_plays_like = None if plays_like.strip().lower() in ("", "none") else plays_like.strip()
                updates.append("plays_like = ?")
                values.append(new_plays_like)
                changes.append(f"Plays Like: {old_plays_like or 'None'} → {new_plays_like or 'None'}")

            if father_son_club is not None:
                if father_son_club.strip().lower() in ("", "none"):
                    father_son = None
                else:
                    father_son = await self.find_father_son_club(db, father_son_club)
                    if father_son is None:
                        await interaction.response.send_message(
                            f"❌ No single team matches father-son club '{father_son_club}'",
                            ephemeral=True
                        )
                        return
                updates.append("father_son_club_id = ?")
                values.append(father_son[0] if father_son else None)
                changes.append(f"Father-Son Club: {old_father_son_name or 'None'} → "
                               f"{father_son[1] if father_son else 'None'}")

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

    @app_commands.command(name="exportdb", description="[ADMIN] Download all bot data as a .db file")
    async def export_db(self, interaction: discord.Interaction):
        """The whole database - every table, every season, always complete,
        so nothing needs updating here as features add new data. Edit it in
        DB Browser for SQLite and bring it back with /importdb, or keep it
        as a backup."""
        await interaction.response.defer(ephemeral=True)
        try:
            data, manifest = await asyncio.to_thread(db_transfer.export_snapshot_bytes, DB_PATH)
        except Exception as e:
            await interaction.followup.send(f"❌ Export failed: {e}", ephemeral=True)
            return

        limit = interaction.guild.filesize_limit if interaction.guild else 10 * 1024 * 1024
        if len(data) > limit:
            await interaction.followup.send(
                f"❌ The database is {len(data) / 1024 / 1024:.1f}MB - over this server's "
                f"{limit / 1024 / 1024:.0f}MB upload limit.",
                ephemeral=True
            )
            return

        exported_at = datetime.fromisoformat(manifest["exported_at"])
        await interaction.followup.send(
            "📦 **All bot data, as of now.**\n"
            "• To edit: open it in DB Browser for SQLite, make your changes, click **Write Changes**, "
            "then upload it with `/importdb`.\n"
            "• Or keep it as a backup.",
            file=discord.File(io.BytesIO(data), filename=f"affl_bot_{exported_at:%Y-%m-%d_%H%M}.db"),
            ephemeral=True
        )

    @app_commands.command(name="importdb", description="[ADMIN] Replace all bot data with a .db file from /exportdb")
    @app_commands.describe(file="A .db file from /exportdb - this bot's or another bot's")
    async def import_db(self, interaction: discord.Interaction, file: discord.Attachment):
        """Shows what the import will do, then replaces everything once the
        admin confirms. This server's channels/roles/emojis are always kept
        (see db_transfer.resolve_wiring), which is what lets one command both
        restore a backup and copy main-bot data onto the test bot."""
        await interaction.response.defer(ephemeral=True)

        if not file.filename.lower().endswith(".db"):
            await interaction.followup.send("❌ Upload the .db file that `/exportdb` gives you.", ephemeral=True)
            return

        temp_dir = tempfile.mkdtemp(prefix="affl_import_")
        upload_path = os.path.join(temp_dir, "upload.db")
        try:
            await file.save(upload_path)
            manifest = await asyncio.to_thread(db_transfer.inspect_upload, upload_path)
            live_instance_id = await asyncio.to_thread(db_transfer.read_instance_id, DB_PATH)
            same_bot = bool(manifest and live_instance_id and manifest.get("instance_id") == live_instance_id)
            changed = (
                await asyncio.to_thread(db_transfer.changes_since_export, DB_PATH, manifest)
                if same_bot else []
            )
        except db_transfer.DatabaseImportError as e:
            shutil.rmtree(temp_dir, ignore_errors=True)
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        except Exception as e:
            shutil.rmtree(temp_dir, ignore_errors=True)
            await interaction.followup.send(f"❌ Couldn't read that file: {e}", ephemeral=True)
            return

        view = ImportDbConfirmView(self, temp_dir, upload_path)
        await interaction.followup.send(
            embed=_import_preview_embed(manifest, same_bot, changed), view=view, ephemeral=True
        )

    def _exists_on_server(self, guild, kind, value):
        try:
            object_id = int(value)
        except (TypeError, ValueError):
            return False
        if kind == "channel":
            return guild.get_channel(object_id) is not None
        if kind == "role":
            return guild.get_role(object_id) is not None
        return self.bot.get_emoji(object_id) is not None

    async def _run_import(self, guild, upload_path):
        """Replaces the live database with the upload, keeping this server's
        own IDs. Returns db_transfer.resolve_wiring's `unset` report."""
        current = await asyncio.to_thread(db_transfer.read_server_wiring, DB_PATH)
        incoming = await asyncio.to_thread(db_transfer.read_server_wiring, upload_path)
        instance_id = await asyncio.to_thread(db_transfer.read_instance_id, DB_PATH)
        resolved, unset = db_transfer.resolve_wiring(
            current, incoming, lambda kind, value: self._exists_on_server(guild, kind, value)
        )

        await asyncio.to_thread(db_transfer.replace_database, upload_path, DB_PATH)
        # Before the IDs go back in: a file from an older version of the bot
        # may be missing columns they're written to (e.g. scratch_teams.emoji_id).
        await _run_init_db()
        await asyncio.to_thread(db_transfer.apply_server_wiring, DB_PATH, resolved, instance_id)
        return unset


async def _run_init_db():
    """bot.py's schema setup - adds any tables/columns an imported file from
    an older version of the bot is missing. bot.py runs as __main__, so
    init_db is taken from there rather than importing a second copy of it."""
    init_db = getattr(sys.modules.get("__main__"), "init_db", None)
    if init_db is None:
        from bot import init_db
    await init_db()


def _import_preview_embed(manifest, same_bot, changed):
    lines = []
    if manifest:
        exported_at = int(datetime.fromisoformat(manifest["exported_at"]).timestamp())
        source = "this bot" if same_bot else "another bot"
        lines.append(f"Exported from **{source}** <t:{exported_at}:R> (<t:{exported_at}:f>).")
    else:
        lines.append(
            "This file has no export record (it wasn't made by `/exportdb`), "
            "so it can't be checked for changes made since."
        )

    if same_bot and changed:
        lines.append(
            "\n:exclamation: **Changed on this bot since that export - these changes will be lost:**\n"
            + "\n".join(f"• {db_transfer.pretty_name(table)}" for table in changed)
        )
    elif same_bot:
        lines.append("\n✅ Nothing has changed on this bot since that export.")
    else:
        lines.append("\nEverything on this bot will be replaced with the file's data.")

    lines.append("\nThis server's channels, roles and emojis are kept - the file's won't overwrite them.")
    return discord.Embed(
        title="Replace all bot data?",
        description="\n".join(lines),
        color=discord.Color.orange() if changed else discord.Color.blurple()
    )


def _import_summary(unset):
    lines = [
        "✅ **All bot data has been replaced with the uploaded file.**",
        "This server's channels, roles and emojis were kept.",
    ]
    if unset["settings"]:
        lines.append(
            "\n:exclamation: **Channels not set on this server** (set with `/config`): "
            + ", ".join(db_transfer.pretty_name(key) for key in unset["settings"])
        )
    if unset["teams"]:
        lines.append(
            "\n:exclamation: **Teams missing a role or channel on this server** (set with `/updateteam`): "
            + ", ".join(unset["teams"])
        )
    lines.append("\nRestart the bot so buttons on older messages pick up the new data.")
    lines.append("Attached is the data that was just replaced - `/importdb` it to undo.")
    return "\n".join(lines)


class ImportDbConfirmView(discord.ui.View):
    """/importdb's Replace / Cancel step. Holds the uploaded file in a temp
    directory until it's confirmed, cancelled, or times out."""
    def __init__(self, cog, temp_dir, upload_path):
        super().__init__(timeout=300)
        self.cog = cog
        self.temp_dir = temp_dir
        self.upload_path = upload_path

    def _finish(self):
        self.stop()
        for item in self.children:
            item.disabled = True
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @discord.ui.button(label="Replace All Data", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        from commands import match_commands
        if match_commands._active_live_match is not None:
            await interaction.response.send_message(
                "❌ A live match is in progress - finish or abandon it before importing.", ephemeral=True
            )
            return

        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="⏳ Importing...", view=self)

        stamp = f"{datetime.now(timezone.utc):%Y-%m-%d_%H%M}"
        try:
            previous_data, _ = await asyncio.to_thread(db_transfer.export_snapshot_bytes, DB_PATH)
        except Exception as e:
            self._finish()
            await interaction.edit_original_response(content=f"❌ Import cancelled - couldn't back up the current data first: {e}", view=self)
            return
        previous_file = discord.File(io.BytesIO(previous_data), filename=f"affl_bot_before_import_{stamp}.db")

        try:
            unset = await self.cog._run_import(interaction.guild, self.upload_path)
        except Exception as e:
            self._finish()
            await interaction.edit_original_response(content="❌ Import failed.", view=self)
            await interaction.followup.send(
                f"❌ **Import failed partway:** {e}\n"
                "Attached is the data from before the import - `/importdb` it to put things back.",
                file=previous_file, ephemeral=True
            )
            return

        self._finish()
        await interaction.edit_original_response(content="✅ Imported.", view=self)
        await interaction.followup.send(_import_summary(unset), file=previous_file, ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._finish()
        await interaction.response.edit_message(content="Import cancelled - nothing was changed.", view=self)

    async def on_timeout(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# /importplayers - bulk-add players from a CSV file
# ---------------------------------------------------------------------------
# The admin fills a spreadsheet, saves it as CSV and attaches it. Every row
# is checked before anything is saved; a row with any problem blocks the
# whole import until the file is fixed, rather than quietly being skipped.

PLAYER_IMPORT_MAX_BYTES = 1_000_000
PLAYER_IMPORT_MAX_ROWS = 1000
PLAYER_IMPORT_LINES_SHOWN = 15
DRAFT_POOL_TEAM = "Draft Pool"

# CSV column -> accepted header spellings, compared with everything but
# letters and digits stripped and lowercased ("Plays Like" -> "playslike").
# Any header starting "father" also counts as the father-son club.
PLAYER_IMPORT_COLUMNS = {
    "name": ("name", "player", "playername"),
    "position": ("position", "pos"),
    "ovr": ("ovr", "rating", "overall", "overallrating"),
    "age": ("age",),
    "team": ("team", "club", "teamname"),
    "contract_expiry": ("contractexpiry", "contract", "expiry", "contractend"),
    "plays_like": ("playslike",),
    "father_son": ("fsclub", "fs"),
}
PLAYER_IMPORT_REQUIRED = ("name", "position", "ovr", "age")
DELISTED_WORDS = ("", "delisted", "none", "free agent", "fa")

PLAYER_IMPORT_TEMPLATE = (
    "Name,Position,OVR,Age,Team,Contract Expiry,Plays Like,Father-Son Club\r\n"
    "Harley Reid,MID-FWD,72,18,Draft Pool,,Dustin Martin,\r\n"
    "Jed Walter,KEY FWD,68,18,Draft Pool,,,Gold Coast Suns\r\n"
)


def _header_key(text):
    return "".join(ch for ch in str(text).lower() if ch.isalnum())


def parse_players_csv(data, teams):
    """Reads a players CSV (bytes). teams: [(team_id, team_name)], used for
    the Team and Father-Son Club columns. Returns (players, errors):
    players is a list of dicts (name, position, ovr, age, team_id,
    team_name, contract_expiry, plays_like, father_son_club_id, row) for
    every valid row; errors is a list of "Row N (name): ..." strings, rows
    numbered as a spreadsheet shows them (the header is row 1)."""
    import csv

    for encoding in ("utf-8-sig", "cp1252"):  # utf-8-sig strips Excel's byte order mark
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        return [], ["The file isn't text - save the spreadsheet as **CSV** and attach that."]

    lines = text.splitlines()
    if not lines or not lines[0].strip():
        return [], ["The file is empty."]
    try:
        # Some Excel locales save CSVs with ; between values.
        dialect = csv.Sniffer().sniff(lines[0], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))

    columns = {}
    for index, key in enumerate(_header_key(cell) for cell in rows[0]):
        for field, spellings in PLAYER_IMPORT_COLUMNS.items():
            if key in spellings or (field == "father_son" and key.startswith("father")):
                columns.setdefault(field, index)
    missing = [field for field in PLAYER_IMPORT_REQUIRED if field not in columns]
    if missing:
        names = {"name": "Name", "position": "Position", "ovr": "OVR", "age": "Age"}
        return [], [f"Missing column(s): {', '.join(names[f] for f in missing)}. The first row must be the "
                    "column headers - run `/importplayers` with no file for a template."]

    by_name = {name.lower(): (team_id, name) for team_id, name in teams}

    def find_team(text):
        exact = by_name.get(text.lower())
        if exact:
            return exact
        partial = [team for key, team in by_name.items() if text.lower() in key]
        return partial[0] if len(partial) == 1 else None

    players, errors, seen = [], [], {}
    for row_number, row in enumerate(rows[1:], start=2):
        if not any(value.strip() for value in row):
            continue
        if len(players) + len(errors) >= PLAYER_IMPORT_MAX_ROWS:
            errors.append(f"More than {PLAYER_IMPORT_MAX_ROWS} players - split the file and import it in parts.")
            break

        def cell(field):
            index = columns.get(field)
            return row[index].strip() if index is not None and index < len(row) else ""

        problems = []
        name = " ".join(cell("name").split())
        if not name:
            problems.append("no name")
        elif name.lower() in seen:
            problems.append(f"same name as row {seen[name.lower()]}")

        is_valid, position = validate_position(cell("position"))
        if not cell("position"):
            problems.append("no position")
        elif not is_valid:
            problems.append(f"\"{cell('position')}\" isn't a position")

        numbers = {}
        for field, label, low, high, required in (("ovr", "OVR", 1, 100, True), ("age", "age", 14, 50, True),
                                                  ("contract_expiry", "contract expiry", 1, 999, False)):
            raw = cell(field)
            if not raw:
                if required:
                    problems.append(f"no {label}")
                numbers[field] = None
                continue
            try:
                value = float(raw)  # Excel can save 72 as 72.0
                if value != int(value):
                    raise ValueError
                numbers[field] = int(value)
            except ValueError:
                problems.append(f"{label} \"{raw}\" isn't a whole number")
                continue
            if not low <= numbers[field] <= high:
                problems.append(f"{label} {numbers[field]} isn't between {low} and {high}")

        team_text = cell("team")
        team = None
        if team_text.lower() not in DELISTED_WORDS:
            team = find_team(team_text)
            if team is None:
                problems.append(f"team \"{team_text}\" doesn't match one team")

        club_text = cell("father_son")
        club = find_team(club_text) if club_text else None
        if club_text and (club is None or club[1] == DRAFT_POOL_TEAM):
            problems.append(f"father-son club \"{club_text}\" doesn't match one team")
            club = None

        if problems:
            errors.append(f"Row {row_number} ({name or 'no name'}): " + "; ".join(problems))
            continue
        seen[name.lower()] = row_number
        players.append({
            "name": name, "position": position, "ovr": numbers["ovr"], "age": numbers["age"],
            "team_id": team[0] if team else None, "team_name": team[1] if team else "Delisted",
            "contract_expiry": numbers.get("contract_expiry"),
            "plays_like": cell("plays_like") or None,
            "father_son_club_id": club[0] if club else None,
            "row": row_number,
        })

    if not players and not errors:
        errors.append("No players found - the file only has a header row.")
    return players, errors


class PlayerImportView(discord.ui.View):
    """Preview of a /importplayers file, with Import / Cancel. Errors block
    the import. A row headed for the Draft Pool whose name is already in
    the pool is skipped; a name that exists anywhere else is just flagged,
    since two real players can share a name. Only the admin who ran the
    command can press the buttons."""

    def __init__(self, user_id, players, errors, existing):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.errors = errors
        self.skipped = [p for p in players
                        if p["team_name"] == DRAFT_POOL_TEAM and existing.get(p["name"].lower()) == DRAFT_POOL_TEAM]
        self.adding = [p for p in players if p not in self.skipped]
        self.same_name = [(p, existing[p["name"].lower()]) for p in self.adding if p["name"].lower() in existing]

        if not errors and self.adding:
            import_btn = discord.ui.Button(label=f"Import {len(self.adding)} player{'s' if len(self.adding) != 1 else ''}", style=discord.ButtonStyle.success)
            import_btn.callback = self._import
            self.add_item(import_btn)
        cancel_btn = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel_btn.callback = self._cancel
        self.add_item(cancel_btn)

    def embed(self, filename):
        if self.errors:
            shown = self.errors[:PLAYER_IMPORT_LINES_SHOWN]
            more = len(self.errors) - len(shown)
            description = (f"**{len(self.errors)} problem(s)** - fix them in the spreadsheet and run "
                           "`/importplayers` again. Nothing has been imported.\n\n"
                           + "\n".join(f"• {e}" for e in shown) + (f"\n… and {more} more" if more else ""))
            return discord.Embed(title=f"❌ {filename}", description=description[:4000], color=discord.Color.red())

        destinations = {}
        for p in self.adding:
            destinations[p["team_name"]] = destinations.get(p["team_name"], 0) + 1
        lines = [f"**{len(self.adding)} player{'s' if len(self.adding) != 1 else ''}** ready to import:"]
        lines += [f"• {team}: {count}" for team, count in sorted(destinations.items(), key=lambda item: -item[1])]
        if self.skipped:
            lines.append(f"\nSkipping {len(self.skipped)} already in the Draft Pool: "
                         + ", ".join(p["name"] for p in self.skipped[:10]) + ("…" if len(self.skipped) > 10 else ""))
        if self.same_name:
            lines.append(f"\n:warning: {len(self.same_name)} share a name with an existing player (they'll still be "
                         "added - Cancel if they're the same person):")
            lines += [f"• {p['name']} (row {p['row']}) - already at {team}"
                      for p, team in self.same_name[:PLAYER_IMPORT_LINES_SHOWN]]
            if len(self.same_name) > PLAYER_IMPORT_LINES_SHOWN:
                lines.append(f"… and {len(self.same_name) - PLAYER_IMPORT_LINES_SHOWN} more")
        if not self.adding:
            lines = ["Every player in this file is already in the Draft Pool - nothing to import."]
        embed = discord.Embed(title=f"Import players - {filename}", description="\n".join(lines)[:4000],
                              color=discord.Color.blurple())
        sample = [f"{p['name']} · {p['position']} · {p['ovr']} OVR · {p['age']}yo · {p['team_name']}"
                  + (f" · plays like {p['plays_like']}" if p["plays_like"] else "")
                  for p in self.adding[:10]]
        if sample:
            embed.add_field(name="First rows", value="\n".join(sample)[:1024], inline=False)
        embed.set_footer(text="Nothing is saved until you press Import.")
        return embed

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Only the admin who ran this import can confirm it.",
                                                    ephemeral=True)
            return False
        return True

    async def _import(self, interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            current_year = await get_current_year(db) or 1
            await db.executemany(
                """INSERT INTO players (name, position, overall_rating, age, birth_year, team_id,
                                        contract_expiry, plays_like, father_son_club_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(p["name"], p["position"], p["ovr"], p["age"], current_year - p["age"], p["team_id"],
                  p["contract_expiry"], p["plays_like"], p["father_son_club_id"]) for p in self.adding],
            )
            await db.commit()
        destinations = {}
        for p in self.adding:
            destinations[p["team_name"]] = destinations.get(p["team_name"], 0) + 1
        embed = discord.Embed(
            title="✅ Players Imported",
            description=f"Added **{len(self.adding)}** players:\n"
                        + "\n".join(f"• {team}: {count}" for team, count in sorted(destinations.items())),
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(embed=embed, view=None)

    async def _cancel(self, interaction):
        await interaction.response.edit_message(
            embed=discord.Embed(title="Import cancelled", description="Nothing was changed.",
                                color=discord.Color.light_grey()),
            view=None,
        )


async def setup(bot):
    await bot.add_cog(AdminCommands(bot))