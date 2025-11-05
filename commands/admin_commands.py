import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
import pandas as pd
import io
import json
from config import DB_PATH, ADMIN_ROLE_ID
from positions import validate_position, get_positions_string

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
        
        # Check if user is server owner (always allowed)
        if interaction.guild.owner_id == interaction.user.id:
            return True
        
        # If ADMIN_ROLE_ID is set, check for that specific role
        if ADMIN_ROLE_ID:
            member = interaction.guild.get_member(interaction.user.id) or interaction.user
            if member:
                # Check if user has the admin role
                admin_role_id = int(ADMIN_ROLE_ID) if isinstance(ADMIN_ROLE_ID, str) else ADMIN_ROLE_ID
                if any(role.id == admin_role_id for role in member.roles):
                    return True
            
            # If admin role is configured but user doesn't have it, deny access
            await interaction.response.send_message(
                "❌ You need the admin role to use this command.",
                ephemeral=True
            )
            return False
        
        # Method 3: Fall back to administrator permissions if no admin role configured
        try:
            if interaction.user.guild_permissions.administrator:
                return True
        except:
            pass
        
        # Method 4: Check roles directly for administrator permission
        member = interaction.guild.get_member(interaction.user.id)
        if member:
            for role in member.roles:
                if role.permissions.administrator:
                    return True
        
        # If all methods fail
        await interaction.response.send_message(
            "❌ You need Administrator permissions to use this command.",
            ephemeral=True
        )
        return False

    @app_commands.command(name="addteam", description="[ADMIN] Add a new team to the league")
    @app_commands.describe(
        team_name="Name of the team",
        role="Discord role for this team",
        emoji="Team emoji (optional)",
        channel="Team channel for notifications (optional)"
    )
    async def add_team(
        self,
        interaction: discord.Interaction,
        team_name: str,
        role: discord.Role,
        emoji: str = None,
        channel: discord.TextChannel = None
    ):
        async with aiosqlite.connect(DB_PATH) as db:
            # Extract emoji ID if custom emoji provided
            emoji_id = None
            if emoji:
                # Check if it's a custom emoji format <:name:id> or <a:name:id>
                import re
                emoji_match = re.match(r'<a?:(\w+):(\d+)>', emoji)
                if emoji_match:
                    emoji_id = emoji_match.group(2)
                # If not custom emoji format, just store as-is (could be unicode emoji)
                else:
                    emoji_id = emoji

            channel_id = str(channel.id) if channel else None

            try:
                await db.execute(
                    "INSERT INTO teams (team_name, role_id, emoji_id, channel_id) VALUES (?, ?, ?, ?)",
                    (team_name, str(role.id), emoji_id, channel_id)
                )
                await db.commit()

                # Build confirmation message
                msg = f"✅ Team **{team_name}** created!\n"
                msg += f"• Role: {role.mention}\n"
                if emoji:
                    msg += f"• Emoji: {emoji}\n"
                if channel:
                    msg += f"• Channel: {channel.mention}"

                await interaction.response.send_message(msg)
            except aiosqlite.IntegrityError:
                await interaction.response.send_message(
                    f"❌ Team name **{team_name}** already exists!",
                    ephemeral=True
                )

    @app_commands.command(name="updateteam", description="[ADMIN] Update a team's settings")
    @app_commands.describe(
        team_name="Name of the team to update",
        new_name="New team name (optional)",
        role="New Discord role (optional)",
        emoji="New team emoji (optional)",
        channel="New team channel (optional)"
    )
    async def update_team(
        self,
        interaction: discord.Interaction,
        team_name: str,
        new_name: str = None,
        role: discord.Role = None,
        emoji: str = None,
        channel: discord.TextChannel = None
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
        trade_log_channel="Channel where approved trades are announced"
    )
    async def config(
        self,
        interaction: discord.Interaction,
        lineups_channel: discord.TextChannel = None,
        delist_log_channel: discord.TextChannel = None,
        trade_approval_channel: discord.TextChannel = None,
        trade_log_channel: discord.TextChannel = None
    ):
        # If no parameters provided, show current settings
        if all(ch is None for ch in [lineups_channel, delist_log_channel, trade_approval_channel, trade_log_channel]):
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    """SELECT setting_key, setting_value FROM settings
                       WHERE setting_key IN ('lineups_channel_id', 'delist_log_channel_id',
                                             'trade_approval_channel_id', 'trade_log_channel_id')"""
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

            await db.commit()

        if updates:
            await interaction.response.send_message(
                "✅ **Configuration Updated:**\n" + "\n".join(updates)
            )
        else:
            await interaction.response.send_message("❌ No settings were updated!", ephemeral=True)

    @app_commands.command(name="removeteam", description="[ADMIN] Remove a team from the league")
    @app_commands.describe(team_name="Name of the team to remove")
    async def remove_team(self, interaction: discord.Interaction, team_name: str):
        async with aiosqlite.connect(DB_PATH) as db:
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
            
            # Free all players from this team
            await db.execute(
                "UPDATE players SET team_id = NULL WHERE team_id = ?",
                (team_id,)
            )
            
            # Delete the team
            await db.execute("DELETE FROM teams WHERE team_id = ?", (team_id,))
            await db.commit()
            
            await interaction.response.send_message(
                f"✅ Team **{team_name}** removed and all players released to free agency!"
            )

    @app_commands.command(name="addplayer", description="[ADMIN] Add a new player")
    @app_commands.describe(
        name="Player name",
        position="Player position",
        rating="Overall rating (1-100)",
        age="Player age",
        team_name="Team name (leave empty for delisted)"
    )
    @app_commands.autocomplete(position=position_autocomplete)
    async def add_player(
        self, 
        interaction: discord.Interaction, 
        name: str, 
        position: str, 
        rating: int,
        age: int,
        team_name: str = None
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
            # Check for duplicate names
            cursor = await db.execute(
                "SELECT player_id, name FROM players WHERE LOWER(name) = LOWER(?)",
                (name,)
            )
            duplicate = await cursor.fetchone()

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

            # Add player
            await db.execute(
                """INSERT INTO players (name, position, overall_rating, age, team_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (name, normalized_pos, rating, age, team_id)
            )
            await db.commit()

            team_text = f"to **{team_name}**" if team_name else "as delisted"
            success_msg = f"✅ Added **{name}** ({normalized_pos}, {rating} OVR, {age}yo) {team_text}!"

            # Add warning if duplicate name exists
            if duplicate:
                success_msg += f"\n\n⚠️ Note: Another player named **{duplicate[1]}** already exists (ID: {duplicate[0]})."

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
                updates.append("age = ?")
                values.append(age)
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
                    new_team_emoji = None
                    if new_team_emoji_id:
                        try:
                            emoji_obj = interaction.client.get_emoji(int(new_team_emoji_id))
                            if emoji_obj:
                                new_team_emoji = str(emoji_obj)
                        except:
                            pass

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
                        try:
                            emoji_obj = interaction.client.get_emoji(int(result[0]))
                            if emoji_obj:
                                old_team_display = str(emoji_obj)
                            else:
                                old_team_display = old_team_name
                        except:
                            old_team_display = old_team_name
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
                    """SELECT team_name as Team_Name, role_id as Role_ID, emoji_id as Emoji_ID, channel_id as Channel_ID
                       FROM teams ORDER BY team_name"""
                )
                teams = await cursor.fetchall()
                teams_df = pd.DataFrame(teams, columns=['Team_Name', 'Role_ID', 'Emoji_ID', 'Channel_ID'])
                teams_df['Role_ID'] = teams_df['Role_ID'].fillna('')
                teams_df['Emoji_ID'] = teams_df['Emoji_ID'].fillna('')
                teams_df['Channel_ID'] = teams_df['Channel_ID'].fillna('')
                
                # Export Players (existing players only - edit but don't add new)
                cursor = await db.execute(
                    """SELECT p.player_id as Player_ID, p.name as Name, t.team_name as Team,
                              p.age as Age, p.position as Pos, p.overall_rating as OVR,
                              p.contract_expiry as Contract_Expiry
                       FROM players p
                       LEFT JOIN teams t ON p.team_id = t.team_id
                       ORDER BY p.name"""
                )
                players = await cursor.fetchall()
                players_df = pd.DataFrame(players, columns=['Player_ID', 'Name', 'Team', 'Age', 'Pos', 'OVR', 'Contract_Expiry'])
                players_df['Team'] = players_df['Team'].fillna('')
                players_df['Contract_Expiry'] = players_df['Contract_Expiry'].fillna('')

                # Create Add_Players sheet (for bulk adding new players)
                add_players_df = pd.DataFrame(columns=['Name', 'Team', 'Age', 'Pos', 'OVR', 'Contract_Expiry'])
                add_players_df = add_players_df.fillna('')
                
                # Export Current Lineups (editable)
                cursor = await db.execute(
                    """SELECT t.team_name as Team_Name, l.position_name as Position,
                              p.name as Player_Name
                       FROM lineups l
                       JOIN teams t ON l.team_id = t.team_id
                       JOIN players p ON l.player_id = p.player_id
                       ORDER BY t.team_name, l.slot_number"""
                )
                current_lineups = await cursor.fetchall()
                current_lineups_df = pd.DataFrame(current_lineups, columns=['Team_Name', 'Position', 'Player_Name'])

                # Export Seasons
                cursor = await db.execute(
                    """SELECT season_number as Season, current_round as Current_Round,
                              regular_rounds as Regular_Rounds, total_rounds as Total_Rounds,
                              round_name as Round_Name, status as Status
                       FROM seasons ORDER BY season_number"""
                )
                seasons = await cursor.fetchall()
                seasons_df = pd.DataFrame(seasons, columns=['Season', 'Current_Round', 'Regular_Rounds', 'Total_Rounds', 'Round_Name', 'Status'])

                # Export Injuries (removed Recovery_Rounds - redundant with Injury_Round + Return_Round)
                cursor = await db.execute(
                    """SELECT p.name as Player_Name, t.team_name as Team,
                              i.injury_type as Injury_Type, i.injury_round as Injury_Round,
                              i.return_round as Return_Round, i.status as Status
                       FROM injuries i
                       JOIN players p ON i.player_id = p.player_id
                       LEFT JOIN teams t ON p.team_id = t.team_id
                       ORDER BY i.status, i.return_round"""
                )
                injuries = await cursor.fetchall()
                injuries_df = pd.DataFrame(injuries, columns=['Player_Name', 'Team', 'Injury_Type', 'Injury_Round', 'Return_Round', 'Status'])
                injuries_df['Team'] = injuries_df['Team'].fillna('Delisted')

                # Export Suspensions (removed Games_Missed - redundant with Suspension_Round + Return_Round)
                cursor = await db.execute(
                    """SELECT p.name as Player_Name, t.team_name as Team,
                              s.suspension_round as Suspension_Round, s.return_round as Return_Round,
                              s.suspension_reason as Reason, s.status as Status
                       FROM suspensions s
                       JOIN players p ON s.player_id = p.player_id
                       LEFT JOIN teams t ON p.team_id = t.team_id
                       ORDER BY s.status, s.return_round"""
                )
                suspensions = await cursor.fetchall()
                suspensions_df = pd.DataFrame(suspensions, columns=['Player_Name', 'Team', 'Suspension_Round', 'Return_Round', 'Reason', 'Status'])
                suspensions_df['Team'] = suspensions_df['Team'].fillna('Delisted')

                # Export Trades
                cursor = await db.execute(
                    """SELECT tr.trade_id as Trade_ID, t1.team_name as Initiating_Team, t2.team_name as Receiving_Team,
                              tr.initiating_players as Initiating_Players, tr.receiving_players as Receiving_Players,
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
                trades_df = pd.DataFrame(trades, columns=['Trade_ID', 'Initiating_Team', 'Receiving_Team', 'Initiating_Players', 'Receiving_Players', 'Status', 'Created_At', 'Responded_At', 'Approved_At', 'Created_By_User_ID', 'Responded_By_User_ID', 'Approved_By_User_ID', 'Original_Trade_ID'])
                trades_df = trades_df.fillna('')

                # Export Settings
                cursor = await db.execute(
                    """SELECT setting_key as Setting_Key, setting_value as Setting_Value
                       FROM settings ORDER BY setting_key"""
                )
                settings = await cursor.fetchall()
                settings_df = pd.DataFrame(settings, columns=['Setting_Key', 'Setting_Value'])
                settings_df = settings_df.fillna('')

                # Export Starting Lineups (flatten JSON to Team/Position/Player_Name format)
                cursor = await db.execute(
                    """SELECT t.team_name, sl.lineup_data
                       FROM starting_lineups sl
                       JOIN teams t ON sl.team_id = t.team_id
                       ORDER BY t.team_name"""
                )
                starting_lineup_rows = await cursor.fetchall()

                # Convert JSON lineups to rows
                starting_lineups_list = []
                for team_name, lineup_json in starting_lineup_rows:
                    if lineup_json:
                        lineup_data = json.loads(lineup_json)
                        for position_name, player_id in lineup_data.items():
                            # Get player name
                            cursor = await db.execute("SELECT name FROM players WHERE player_id = ?", (int(player_id),))
                            player = await cursor.fetchone()
                            player_name = player[0] if player else f"Unknown ({player_id})"
                            starting_lineups_list.append({
                                'Team_Name': team_name,
                                'Position': position_name,
                                'Player_Name': player_name
                            })

                starting_lineups_df = pd.DataFrame(starting_lineups_list)
                if starting_lineups_df.empty:
                    starting_lineups_df = pd.DataFrame(columns=['Team_Name', 'Position', 'Player_Name'])

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

                # Export Draft Picks
                cursor = await db.execute(
                    """SELECT dp.pick_id as Pick_ID, dp.draft_name as Draft_Name,
                              dp.round_number as Round, dp.pick_number as Pick,
                              dp.pick_origin as Pick_Origin, ct.team_name as Current_Team,
                              p.name as Player_Selected
                       FROM draft_picks dp
                       JOIN teams ct ON dp.current_team_id = ct.team_id
                       LEFT JOIN players p ON dp.player_selected_id = p.player_id
                       ORDER BY dp.draft_name, dp.round_number, dp.pick_number"""
                )
                draft_picks = await cursor.fetchall()
                draft_picks_df = pd.DataFrame(draft_picks, columns=['Pick_ID', 'Draft_Name', 'Round', 'Pick', 'Pick_Origin', 'Current_Team', 'Player_Selected'])
                draft_picks_df['Pick_Origin'] = draft_picks_df['Pick_Origin'].fillna('')
                draft_picks_df['Player_Selected'] = draft_picks_df['Player_Selected'].fillna('')

                # Export Submitted Lineups
                cursor = await db.execute(
                    """SELECT sl.submission_id as Submission_ID, t.team_name as Team_Name,
                              s.season_number as Season, sl.round_number as Round,
                              sl.player_ids as Player_IDs, sl.submitted_at as Submitted_At
                       FROM submitted_lineups sl
                       JOIN teams t ON sl.team_id = t.team_id
                       JOIN seasons s ON sl.season_id = s.season_id
                       ORDER BY s.season_number, sl.round_number, t.team_name"""
                )
                submitted_lineups = await cursor.fetchall()
                submitted_lineups_df = pd.DataFrame(submitted_lineups, columns=['Submission_ID', 'Team_Name', 'Season', 'Round', 'Player_IDs', 'Submitted_At'])

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

                # Export Free Agency Periods
                cursor = await db.execute(
                    """SELECT period_id as Period_ID, season_number as Season_Number,
                              status as Status, auction_points as Auction_Points,
                              started_at as Started_At,
                              bidding_ended_at as Bidding_Ended_At,
                              matching_ended_at as Matching_Ended_At
                       FROM free_agency_periods
                       ORDER BY season_number DESC"""
                )
                free_agency_periods = await cursor.fetchall()
                free_agency_periods_df = pd.DataFrame(free_agency_periods, columns=['Period_ID', 'Season_Number', 'Status', 'Auction_Points', 'Started_At', 'Bidding_Ended_At', 'Matching_Ended_At'])
                free_agency_periods_df = free_agency_periods_df.fillna('')

            # Create Excel file in memory
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                # Core data sheets (editable)
                teams_df.to_excel(writer, sheet_name='Teams', index=False)
                players_df.to_excel(writer, sheet_name='Players', index=False)
                add_players_df.to_excel(writer, sheet_name='Add_Players', index=False)
                seasons_df.to_excel(writer, sheet_name='Seasons', index=False)
                settings_df.to_excel(writer, sheet_name='Settings', index=False)
                compensation_chart_df.to_excel(writer, sheet_name='Compensation_Chart', index=False)
                contract_config_df.to_excel(writer, sheet_name='Contract_Config', index=False)

                # Relationship/State sheets (editable)
                current_lineups_df.to_excel(writer, sheet_name='Current_Lineups', index=False)
                starting_lineups_df.to_excel(writer, sheet_name='Starting_Lineups', index=False)
                injuries_df.to_excel(writer, sheet_name='Injuries', index=False)
                suspensions_df.to_excel(writer, sheet_name='Suspensions', index=False)
                draft_picks_df.to_excel(writer, sheet_name='Draft_Picks', index=False)
                free_agency_periods_df.to_excel(writer, sheet_name='Free_Agency_Periods', index=False)

                # History/Read-only sheets
                trades_df.to_excel(writer, sheet_name='Trades', index=False)
                matches_df.to_excel(writer, sheet_name='Matches', index=False)
                submitted_lineups_df.to_excel(writer, sheet_name='Submitted_Lineups', index=False)
                
                # Add instructions sheet
                instructions = pd.DataFrame({
                    'IMPORTANT INSTRUCTIONS': [
                        '--- EXCEL IMPORT/EXPORT GUIDE ---',
                        '',
                        'IMPORTANT: Before editing Teams sheet:',
                        '  - Select entire Role_ID column → Right-click → Format Cells → Text',
                        '  - This prevents Excel from corrupting Discord IDs',
                        '',
                        'To import: Use /importdata command and attach this file',
                        '',
                        '--- SHEET ORGANIZATION ---',
                        '',
                        'CORE DATA (Editable):',
                        '  • Teams - Team info, Discord role/emoji IDs',
                        '  • Players - Edit EXISTING players only (has Player_ID column)',
                        '  • Add_Players - Bulk add NEW players here (no Player_ID needed)',
                        '  • Seasons - Season configuration',
                        '  • Settings - Bot settings',
                        '',
                        'RELATIONSHIPS/STATE (Editable):',
                        '  • Current_Lineups - Active team lineups (Team/Position/Player)',
                        '  • Starting_Lineups - Saved lineup templates (Team/Position/Player)',
                        '  • Injuries - Injury status (Recovery calculated automatically)',
                        '  • Suspensions - Suspension status (Games missed calculated automatically)',
                        '  • Draft_Picks - Draft pick ownership',
                        '',
                        'HISTORY (Read-only - import supported):',
                        '  • Trades - Trade history',
                        '  • Matches - Match results',
                        '  • Submitted_Lineups - Historical lineup submissions',
                        '',
                        '--- KEY FEATURES ---',
                        '',
                        '1. Players sheet includes Player_ID column',
                        '   - Only updates EXISTING players',
                        '   - Cannot add new players through this sheet',
                        '',
                        '2. Add_Players sheet for bulk adding',
                        '   - Fill in Name, Team, Age, Pos, OVR',
                        '   - Import will add all new players at once',
                        '   - Skips duplicates automatically',
                        '',
                        '3. Simplified Injuries/Suspensions',
                        '   - Just enter Injury_Round and Return_Round',
                        '   - Recovery_Rounds/Games_Missed calculated on import',
                        '',
                        '4. Consistent lineup formats',
                        '   - Current_Lineups and Starting_Lineups use identical format',
                        '   - Easy to copy/paste between sheets',
                        '',
                        '--- VALID POSITIONS ---',
                        '',
                        'Player Positions: MID, KEY FWD, RUCK, GEN DEF, etc.',
                        'Lineup Positions: FB, CHB, LW, C, RW, CHF, FF, R, RR, RO, INT1-4, SUB',
                    ]
                })
                instructions.to_excel(writer, sheet_name='README', index=False)
            output.seek(0)
            
            # Send file
            file = discord.File(output, filename='league_data.xlsx')
            stats = [
                f"{len(teams_df)} teams",
                f"{len(players_df)} players",
                f"{len(current_lineups_df)} current lineup positions",
                f"{len(starting_lineups_df)} starting lineup positions",
                f"{len(seasons_df)} seasons",
                f"{len(injuries_df)} injuries",
                f"{len(suspensions_df)} suspensions",
                f"{len(draft_picks_df)} draft picks",
                f"{len(trades_df)} trades",
                f"{len(matches_df)} matches",
                f"{len(submitted_lineups_df)} submitted lineups",
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
            errors = []
            duplicate_warnings = []
            
            async with aiosqlite.connect(DB_PATH) as db:
                # Import Teams
                try:
                    teams_df = pd.read_excel(excel_file, sheet_name='Teams', dtype={'Role_ID': str, 'Emoji_ID': str, 'Channel_ID': str})

                    for _, row in teams_df.iterrows():
                        try:
                            team_name = str(row['Team_Name']).strip()
                            role_id = str(row['Role_ID']).strip() if pd.notna(row['Role_ID']) and row['Role_ID'] else None
                            emoji_id = str(row['Emoji_ID']).strip() if pd.notna(row['Emoji_ID']) and row['Emoji_ID'] else None
                            channel_id = str(row['Channel_ID']).strip() if 'Channel_ID' in row and pd.notna(row['Channel_ID']) and row['Channel_ID'] else None

                            cursor = await db.execute("SELECT team_id FROM teams WHERE team_name = ?", (team_name,))
                            existing = await cursor.fetchone()

                            if existing:
                                await db.execute("UPDATE teams SET role_id = ?, emoji_id = ?, channel_id = ? WHERE team_name = ?", (role_id, emoji_id, channel_id, team_name))
                                teams_updated += 1
                            else:
                                await db.execute("INSERT INTO teams (team_name, role_id, emoji_id, channel_id) VALUES (?, ?, ?, ?)", (team_name, role_id, emoji_id, channel_id))
                                teams_added += 1
                        except Exception as e:
                            errors.append(f"Team '{team_name}': {str(e)}")
                    
                    await db.commit()
                except Exception as e:
                    errors.append(f"Teams sheet error: {str(e)}")
                
                # Import Players (UPDATE existing players only - use Add_Players sheet to add new)
                try:
                    players_df = pd.read_excel(excel_file, sheet_name='Players')

                    # Get team mapping
                    cursor = await db.execute("SELECT team_id, team_name FROM teams")
                    teams = await cursor.fetchall()
                    team_map = {name.lower(): id for id, name in teams}

                    for _, row in players_df.iterrows():
                        try:
                            # Use Player_ID if available for more reliable matching
                            player_id = None
                            if 'Player_ID' in players_df.columns and pd.notna(row['Player_ID']):
                                player_id = int(row['Player_ID'])

                            name = str(row['Name']).strip()
                            position = str(row['Pos']).strip()
                            rating = int(row['OVR'])
                            age = int(row['Age'])

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

                            # Check if player exists (by ID first, then by name)
                            existing = None
                            if player_id:
                                cursor = await db.execute("SELECT player_id FROM players WHERE player_id = ?", (player_id,))
                                existing = await cursor.fetchone()

                            if not existing:
                                cursor = await db.execute("SELECT player_id FROM players WHERE name = ?", (name,))
                                existing = await cursor.fetchone()

                            # Only UPDATE existing players (don't add new ones)
                            if existing:
                                await db.execute(
                                    """UPDATE players
                                       SET name = ?, position = ?, overall_rating = ?, age = ?, team_id = ?, contract_expiry = ?
                                       WHERE player_id = ?""",
                                    (name, normalized_pos, rating, age, team_id, contract_expiry, existing[0])
                                )
                                players_updated += 1
                            else:
                                errors.append(f"Player '{name}' not found - use Add_Players sheet to add new players")

                        except Exception as e:
                            errors.append(f"Player '{name}': {str(e)}")
                    
                    await db.commit()
                except Exception as e:
                    errors.append(f"Players sheet error: {str(e)}")

                # Import Add_Players (bulk add new players)
                try:
                    add_players_df = pd.read_excel(excel_file, sheet_name='Add_Players')

                    # Get team mapping
                    cursor = await db.execute("SELECT team_id, team_name FROM teams")
                    teams = await cursor.fetchall()
                    team_map = {name.lower(): id for id, name in teams}

                    for _, row in add_players_df.iterrows():
                        try:
                            # Skip empty rows
                            if not pd.notna(row['Name']) or not str(row['Name']).strip():
                                continue

                            name = str(row['Name']).strip()
                            position = str(row['Pos']).strip()
                            rating = int(row['OVR'])
                            age = int(row['Age'])

                            # Get contract_expiry if present
                            contract_expiry = None
                            if 'Contract_Expiry' in add_players_df.columns and pd.notna(row['Contract_Expiry']) and row['Contract_Expiry']:
                                contract_expiry = int(row['Contract_Expiry'])

                            # Validate position
                            is_valid, normalized_pos = validate_position(position)
                            if not is_valid:
                                errors.append(f"Add_Players - '{name}': Invalid position '{position}'")
                                continue

                            # Get team ID
                            team_id = None
                            if 'Team' in add_players_df.columns and pd.notna(row['Team']) and row['Team']:
                                team_name_lower = str(row['Team']).strip().lower()
                                team_id = team_map.get(team_name_lower)

                            # Check if player already exists
                            cursor = await db.execute("SELECT player_id FROM players WHERE name = ?", (name,))
                            existing = await cursor.fetchone()

                            if existing:
                                duplicate_warnings.append(f"Add_Players: '{name}' already exists (ID: {existing[0]}) - skipped")
                            else:
                                await db.execute(
                                    """INSERT INTO players (name, position, overall_rating, age, team_id, contract_expiry)
                                       VALUES (?, ?, ?, ?, ?, ?)""",
                                    (name, normalized_pos, rating, age, team_id, contract_expiry)
                                )
                                players_added += 1
                        except Exception as e:
                            errors.append(f"Add_Players - '{name}': {str(e)}")

                    await db.commit()
                except Exception as e:
                    if 'Worksheet Add_Players' not in str(e):
                        errors.append(f"Add_Players sheet error: {str(e)}")

                # Import Current Lineups
                current_lineups_imported = 0
                try:
                    current_lineups_df = pd.read_excel(excel_file, sheet_name='Current_Lineups')

                    # Valid lineup positions with slot numbers
                    valid_lineup_positions = [
                        "LBP", "FB", "RBP", "LHB", "CHB", "RHB",
                        "LW", "C", "RW", "LHF", "CHF", "RHF",
                        "LFP", "FF", "RFP", "R", "RR", "RO",
                        "INT1", "INT2", "INT3", "INT4", "SUB"
                    ]

                    for _, row in current_lineups_df.iterrows():
                        try:
                            # Get team ID
                            cursor = await db.execute("SELECT team_id FROM teams WHERE team_name = ?", (str(row['Team_Name']),))
                            team = await cursor.fetchone()

                            # Get player ID
                            cursor = await db.execute("SELECT player_id FROM players WHERE name = ?", (str(row['Player_Name']),))
                            player = await cursor.fetchone()

                            position = str(row['Position']).strip().upper()

                            if team and player and position in valid_lineup_positions:
                                slot_number = valid_lineup_positions.index(position) + 1
                                await db.execute(
                                    """INSERT OR REPLACE INTO lineups (team_id, player_id, slot_number, position_name)
                                       VALUES (?, ?, ?, ?)""",
                                    (team[0], player[0], slot_number, position)
                                )
                                current_lineups_imported += 1
                            elif position not in valid_lineup_positions:
                                errors.append(f"Current lineup: Invalid position '{position}' for {row['Player_Name']}")
                        except Exception as e:
                            errors.append(f"Current lineup for {row['Team_Name']}: {str(e)}")
                    await db.commit()
                except Exception as e:
                    if 'Worksheet Current_Lineups' not in str(e):
                        errors.append(f"Current Lineups sheet error: {str(e)}")

                # Import Seasons
                seasons_imported = 0
                try:
                    seasons_df = pd.read_excel(excel_file, sheet_name='Seasons')
                    for _, row in seasons_df.iterrows():
                        try:
                            await db.execute(
                                """INSERT OR REPLACE INTO seasons
                                   (season_number, current_round, regular_rounds, total_rounds, round_name, status)
                                   VALUES (?, ?, ?, ?, ?, ?)""",
                                (int(row['Season']), int(row['Current_Round']), int(row['Regular_Rounds']),
                                 int(row['Total_Rounds']), str(row['Round_Name']), str(row['Status']))
                            )
                            seasons_imported += 1
                        except Exception as e:
                            errors.append(f"Season {row['Season']}: {str(e)}")
                    await db.commit()
                except Exception as e:
                    if 'Worksheet Seasons' not in str(e):
                        errors.append(f"Seasons sheet error: {str(e)}")

                # Import Injuries (calculate recovery_rounds from injury_round and return_round)
                injuries_imported = 0
                try:
                    injuries_df = pd.read_excel(excel_file, sheet_name='Injuries')

                    # Clear existing injuries before importing to avoid duplicates
                    await db.execute("DELETE FROM injuries")

                    for _, row in injuries_df.iterrows():
                        try:
                            # Find player by name
                            cursor = await db.execute("SELECT player_id FROM players WHERE name = ?", (str(row['Player_Name']),))
                            player = await cursor.fetchone()
                            if player:
                                injury_round = int(row['Injury_Round'])
                                return_round = int(row['Return_Round'])
                                recovery_rounds = return_round - injury_round
                                await db.execute(
                                    """INSERT INTO injuries
                                       (player_id, injury_type, injury_round, recovery_rounds, return_round, status)
                                       VALUES (?, ?, ?, ?, ?, ?)""",
                                    (player[0], str(row['Injury_Type']), injury_round,
                                     recovery_rounds, return_round, str(row['Status']))
                                )
                                injuries_imported += 1
                        except Exception as e:
                            errors.append(f"Injury for {row['Player_Name']}: {str(e)}")
                    await db.commit()
                except Exception as e:
                    if 'Worksheet Injuries' not in str(e):
                        errors.append(f"Injuries sheet error: {str(e)}")

                # Import Suspensions (calculate games_missed from suspension_round and return_round)
                suspensions_imported = 0
                try:
                    suspensions_df = pd.read_excel(excel_file, sheet_name='Suspensions')

                    # Clear existing suspensions before importing to avoid duplicates
                    await db.execute("DELETE FROM suspensions")

                    for _, row in suspensions_df.iterrows():
                        try:
                            # Find player by name
                            cursor = await db.execute("SELECT player_id FROM players WHERE name = ?", (str(row['Player_Name']),))
                            player = await cursor.fetchone()
                            if player:
                                suspension_round = int(row['Suspension_Round'])
                                return_round = int(row['Return_Round'])
                                games_missed = return_round - suspension_round
                                await db.execute(
                                    """INSERT INTO suspensions
                                       (player_id, suspension_round, games_missed, return_round, suspension_reason, status)
                                       VALUES (?, ?, ?, ?, ?, ?)""",
                                    (player[0], suspension_round, games_missed,
                                     return_round, str(row['Reason']), str(row['Status']))
                                )
                                suspensions_imported += 1
                        except Exception as e:
                            errors.append(f"Suspension for {row['Player_Name']}: {str(e)}")
                    await db.commit()
                except Exception as e:
                    if 'Worksheet Suspensions' not in str(e):
                        errors.append(f"Suspensions sheet error: {str(e)}")

                # Import Trades
                trades_imported = 0
                try:
                    trades_df = pd.read_excel(excel_file, sheet_name='Trades', dtype={'Created_By_User_ID': str, 'Responded_By_User_ID': str, 'Approved_By_User_ID': str})
                    for _, row in trades_df.iterrows():
                        try:
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

                                await db.execute(
                                    """INSERT OR REPLACE INTO trades
                                       (trade_id, initiating_team_id, receiving_team_id, initiating_players, receiving_players,
                                        status, created_at, responded_at, approved_at, created_by_user_id,
                                        responded_by_user_id, approved_by_user_id, original_trade_id)
                                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                    (int(row['Trade_ID']), init_team[0], recv_team[0], str(row['Initiating_Players']),
                                     str(row['Receiving_Players']), str(row['Status']), created_at, responded_at,
                                     approved_at, created_by, responded_by, approved_by, original_trade_id)
                                )
                                trades_imported += 1
                        except Exception as e:
                            errors.append(f"Trade {row['Trade_ID']}: {str(e)}")
                    await db.commit()
                except Exception as e:
                    if 'Worksheet Trades' not in str(e):
                        errors.append(f"Trades sheet error: {str(e)}")

                # Import Settings
                settings_imported = 0
                try:
                    settings_df = pd.read_excel(excel_file, sheet_name='Settings', dtype={'Setting_Value': str})
                    for _, row in settings_df.iterrows():
                        try:
                            setting_value = str(row['Setting_Value']) if pd.notna(row['Setting_Value']) and row['Setting_Value'] else None
                            await db.execute(
                                """INSERT OR REPLACE INTO settings (setting_key, setting_value)
                                   VALUES (?, ?)""",
                                (str(row['Setting_Key']), setting_value)
                            )
                            settings_imported += 1
                        except Exception as e:
                            errors.append(f"Setting {row['Setting_Key']}: {str(e)}")
                    await db.commit()
                except Exception as e:
                    if 'Worksheet Settings' not in str(e):
                        errors.append(f"Settings sheet error: {str(e)}")

                # Import Starting Lineups (convert from flattened format to JSON)
                starting_lineups_imported = 0
                try:
                    starting_lineups_df = pd.read_excel(excel_file, sheet_name='Starting_Lineups')

                    # Group by team and build JSON lineups
                    team_lineups = {}
                    for _, row in starting_lineups_df.iterrows():
                        try:
                            team_name = str(row['Team_Name'])
                            position = str(row['Position']).strip()
                            player_name = str(row['Player_Name'])

                            # Get player ID
                            cursor = await db.execute("SELECT player_id FROM players WHERE name = ?", (player_name,))
                            player = await cursor.fetchone()

                            if player:
                                if team_name not in team_lineups:
                                    team_lineups[team_name] = {}
                                team_lineups[team_name][position] = player[0]
                        except Exception as e:
                            errors.append(f"Starting lineup row error: {str(e)}")

                    # Insert/update starting lineups for each team
                    for team_name, lineup_dict in team_lineups.items():
                        try:
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
                        except Exception as e:
                            errors.append(f"Starting lineup for {team_name}: {str(e)}")

                    await db.commit()
                except Exception as e:
                    if 'Worksheet Starting_Lineups' not in str(e):
                        errors.append(f"Starting Lineups sheet error: {str(e)}")

                # Import Matches
                matches_imported = 0
                try:
                    matches_df = pd.read_excel(excel_file, sheet_name='Matches')
                    for _, row in matches_df.iterrows():
                        try:
                            # Get season ID
                            cursor = await db.execute("SELECT season_id FROM seasons WHERE season_number = ?", (int(row['Season']),))
                            season = await cursor.fetchone()

                            # Get home team ID
                            cursor = await db.execute("SELECT team_id FROM teams WHERE team_name = ?", (str(row['Home_Team']),))
                            home_team = await cursor.fetchone()

                            # Get away team ID
                            cursor = await db.execute("SELECT team_id FROM teams WHERE team_name = ?", (str(row['Away_Team']),))
                            away_team = await cursor.fetchone()

                            if season and home_team and away_team:
                                await db.execute(
                                    """INSERT OR REPLACE INTO matches
                                       (match_id, season_id, round_number, home_team_id, away_team_id, home_score, away_score, simulated)
                                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                                    (int(row['Match_ID']), season[0], int(row['Round']), home_team[0], away_team[0],
                                     int(row['Home_Score']), int(row['Away_Score']), int(row['Simulated']))
                                )
                                matches_imported += 1
                        except Exception as e:
                            errors.append(f"Match {row['Match_ID']}: {str(e)}")
                    await db.commit()
                except Exception as e:
                    if 'Worksheet Matches' not in str(e):
                        errors.append(f"Matches sheet error: {str(e)}")

                # Import Draft Picks
                draft_picks_imported = 0
                try:
                    draft_picks_df = pd.read_excel(excel_file, sheet_name='Draft_Picks')

                    # Clear existing draft picks before importing to avoid duplicates
                    await db.execute("DELETE FROM draft_picks")

                    for _, row in draft_picks_df.iterrows():
                        try:
                            # Get current team ID
                            cursor = await db.execute("SELECT team_id FROM teams WHERE team_name = ?", (str(row['Current_Team']),))
                            current_team = await cursor.fetchone()

                            # Get player ID if selected
                            player_id = None
                            if pd.notna(row['Player_Selected']) and row['Player_Selected']:
                                cursor = await db.execute("SELECT player_id FROM players WHERE name = ?", (str(row['Player_Selected']),))
                                player = await cursor.fetchone()
                                if player:
                                    player_id = player[0]

                            # Get pick_origin (handle empty/NaN values)
                            pick_origin = str(row['Pick_Origin']) if pd.notna(row['Pick_Origin']) and row['Pick_Origin'] else ''

                            # Handle NaN values for numeric fields
                            pick_id = int(row['Pick_ID']) if pd.notna(row['Pick_ID']) else None
                            round_number = int(row['Round']) if pd.notna(row['Round']) else None
                            pick_number = int(row['Pick']) if pd.notna(row['Pick']) else None
                            draft_name = str(row['Draft_Name']) if pd.notna(row['Draft_Name']) else ''

                            # Get original_team_id (same as current for imports, can be updated via trades)
                            original_team_id = current_team[0] if current_team else None

                            # Get season_number from draft_name if possible (format: "Season X National Draft")
                            season_number = None
                            if 'Season' in draft_name:
                                try:
                                    # Extract season number from draft name (e.g., "Season 9 National Draft")
                                    season_str = draft_name.split('Season')[1].split()[0]
                                    # Draft is for season_number + 1 (Season 9 Draft is for Season 10)
                                    season_number = int(season_str) + 1
                                except:
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
                                await db.execute(
                                    """INSERT INTO draft_picks
                                       (pick_id, draft_id, draft_name, season_number, round_number, pick_number,
                                        pick_origin, original_team_id, current_team_id, player_selected_id)
                                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                    (pick_id, draft_id, draft_name, season_number, round_number, pick_number,
                                     pick_origin, original_team_id, current_team[0], player_id)
                                )
                                draft_picks_imported += 1
                        except Exception as e:
                            errors.append(f"Draft Pick {row['Pick_ID']}: {str(e)}")
                    await db.commit()
                except Exception as e:
                    if 'Worksheet Draft_Picks' not in str(e):
                        errors.append(f"Draft Picks sheet error: {str(e)}")

                # Import Submitted Lineups
                submitted_lineups_imported = 0
                try:
                    submitted_lineups_df = pd.read_excel(excel_file, sheet_name='Submitted_Lineups')
                    for _, row in submitted_lineups_df.iterrows():
                        try:
                            # Get team ID
                            cursor = await db.execute("SELECT team_id FROM teams WHERE team_name = ?", (str(row['Team_Name']),))
                            team = await cursor.fetchone()

                            # Get season ID
                            cursor = await db.execute("SELECT season_id FROM seasons WHERE season_number = ?", (int(row['Season']),))
                            season = await cursor.fetchone()

                            if team and season:
                                await db.execute(
                                    """INSERT OR REPLACE INTO submitted_lineups
                                       (submission_id, team_id, season_id, round_number, player_ids, submitted_at)
                                       VALUES (?, ?, ?, ?, ?, ?)""",
                                    (int(row['Submission_ID']), team[0], season[0], int(row['Round']),
                                     str(row['Player_IDs']), str(row['Submitted_At']))
                                )
                                submitted_lineups_imported += 1
                        except Exception as e:
                            errors.append(f"Submitted Lineup {row['Submission_ID']}: {str(e)}")
                    await db.commit()
                except Exception as e:
                    if 'Worksheet Submitted_Lineups' not in str(e):
                        errors.append(f"Submitted Lineups sheet error: {str(e)}")

                # Import Compensation Chart (2D table format with individual ages/OVRs)
                compensation_chart_imported = 0
                try:
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

                        try:
                            age = int(float(age_str))  # Convert through float first to handle "19.0" format
                        except:
                            continue

                        # Process each OVR column
                        for ovr_col in ovr_cols:
                            band_value = row[ovr_col]
                            if pd.isna(band_value) or band_value == '':
                                continue

                            try:
                                band = int(float(band_value))  # Convert through float first
                                # Column name might be int or string
                                try:
                                    ovr = int(ovr_col)
                                except:
                                    ovr = int(float(str(ovr_col)))  # Handle string column names
                                cell_map[(age, ovr)] = band
                            except Exception as e:
                                # Log parsing errors for debugging
                                errors.append(f"Compensation Chart cell parsing error at age {age_str}, OVR {ovr_col}: {str(e)}")
                                continue

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

                    await db.commit()
                except Exception as e:
                    if 'Worksheet Compensation_Chart' not in str(e):
                        errors.append(f"Compensation Chart sheet error: {str(e)}")

                # Import Contract Config
                contract_config_imported = 0
                try:
                    contract_config_df = pd.read_excel(excel_file, sheet_name='Contract_Config')

                    # Clear existing contract config
                    await db.execute("DELETE FROM contract_config")

                    for _, row in contract_config_df.iterrows():
                        try:
                            min_age = int(row['Min_Age'])
                            max_age = int(row['Max_Age']) if pd.notna(row['Max_Age']) and row['Max_Age'] else None
                            contract_years = int(row['Contract_Years'])

                            await db.execute(
                                """INSERT INTO contract_config (min_age, max_age, contract_years)
                                   VALUES (?, ?, ?)""",
                                (min_age, max_age, contract_years)
                            )
                            contract_config_imported += 1
                        except Exception as e:
                            errors.append(f"Contract Config row: {str(e)}")
                    await db.commit()
                except Exception as e:
                    if 'Worksheet Contract_Config' not in str(e):
                        errors.append(f"Contract Config sheet error: {str(e)}")

                # Import Free Agency Periods
                free_agency_periods_imported = 0
                try:
                    free_agency_periods_df = pd.read_excel(excel_file, sheet_name='Free_Agency_Periods')

                    # Clear existing free agency periods
                    await db.execute("DELETE FROM free_agency_periods")

                    for _, row in free_agency_periods_df.iterrows():
                        try:
                            period_id = int(row['Period_ID']) if pd.notna(row['Period_ID']) and row['Period_ID'] else None
                            season_number = int(row['Season_Number'])
                            status = str(row['Status'])
                            auction_points = int(row['Auction_Points']) if pd.notna(row['Auction_Points']) else 300
                            started_at = str(row['Started_At']) if pd.notna(row['Started_At']) and row['Started_At'] else None
                            bidding_ended_at = str(row['Bidding_Ended_At']) if pd.notna(row['Bidding_Ended_At']) and row['Bidding_Ended_At'] else None
                            matching_ended_at = str(row['Matching_Ended_At']) if pd.notna(row['Matching_Ended_At']) and row['Matching_Ended_At'] else None

                            if period_id:
                                await db.execute(
                                    """INSERT INTO free_agency_periods (period_id, season_number, status, auction_points, started_at, bidding_ended_at, matching_ended_at)
                                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                                    (period_id, season_number, status, auction_points, started_at, bidding_ended_at, matching_ended_at)
                                )
                            else:
                                await db.execute(
                                    """INSERT INTO free_agency_periods (season_number, status, auction_points, started_at, bidding_ended_at, matching_ended_at)
                                       VALUES (?, ?, ?, ?, ?, ?)""",
                                    (season_number, status, auction_points, started_at, bidding_ended_at, matching_ended_at)
                                )
                            free_agency_periods_imported += 1
                        except Exception as e:
                            errors.append(f"Free Agency Periods row: {str(e)}")
                    await db.commit()
                except Exception as e:
                    if 'Worksheet Free_Agency_Periods' not in str(e):
                        errors.append(f"Free Agency Periods sheet error: {str(e)}")

            # Build response
            response = "✅ **Import Complete!**\n\n"
            response += f"**Teams:** {teams_added} added, {teams_updated} updated\n"
            response += f"**Players:** {players_added} added, {players_updated} updated\n"
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
            if draft_picks_imported > 0:
                response += f"**Draft Picks:** {draft_picks_imported} imported\n"
            if trades_imported > 0:
                response += f"**Trades:** {trades_imported} imported\n"
            if matches_imported > 0:
                response += f"**Matches:** {matches_imported} imported\n"
            if submitted_lineups_imported > 0:
                response += f"**Submitted Lineups:** {submitted_lineups_imported} imported\n"
            if settings_imported > 0:
                response += f"**Settings:** {settings_imported} imported\n"
            if compensation_chart_imported > 0:
                response += f"**Compensation Chart:** {compensation_chart_imported} entries imported\n"
            if contract_config_imported > 0:
                response += f"**Contract Config:** {contract_config_imported} entries imported\n"
            if free_agency_periods_imported > 0:
                response += f"**Free Agency Periods:** {free_agency_periods_imported} imported\n"

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
            await interaction.followup.send(f"❌ Error importing file: {e}", ephemeral=True)

    @app_commands.command(name="assignrookiecontracts", description="[ADMIN] Assign contract_expiry to drafted rookies")
    @app_commands.describe(
        draft_name="Name of the draft (e.g., 'Season 9 National Draft')"
    )
    async def assign_rookie_contracts(self, interaction: discord.Interaction, draft_name: str):
        await interaction.response.defer(ephemeral=True)

        # Check if user has admin role
        if not any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles):
            await interaction.followup.send("❌ You don't have permission to use this command.", ephemeral=True)
            return

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get draft information
                cursor = await db.execute(
                    "SELECT draft_id, season_number, rookie_contract_years FROM drafts WHERE draft_name = ?",
                    (draft_name,)
                )
                draft_result = await cursor.fetchone()
                if not draft_result:
                    await interaction.followup.send(f"❌ Draft '{draft_name}' not found!")
                    return

                draft_id, season_number, rookie_contract_years = draft_result

                if rookie_contract_years is None:
                    rookie_contract_years = 3  # Default

                # Calculate contract expiry: season + years - 1
                contract_expiry = season_number + rookie_contract_years - 1

                # Get all drafted players from this draft
                cursor = await db.execute(
                    """SELECT player_selected_id FROM draft_picks
                       WHERE draft_id = ? AND player_selected_id IS NOT NULL""",
                    (draft_id,)
                )
                drafted_players = await cursor.fetchall()

                if not drafted_players:
                    await interaction.followup.send(f"❌ No players have been drafted in '{draft_name}'!")
                    return

                # Assign contract_expiry to all drafted players
                players_updated = 0
                for (player_id,) in drafted_players:
                    await db.execute(
                        "UPDATE players SET contract_expiry = ? WHERE player_id = ?",
                        (contract_expiry, player_id)
                    )
                    players_updated += 1

                await db.commit()

                await interaction.followup.send(
                    f"✅ **Rookie Contracts Assigned!**\n\n"
                    f"**Draft:** {draft_name}\n"
                    f"**Season:** {season_number}\n"
                    f"**Contract Length:** {rookie_contract_years} years\n"
                    f"**Contract Expiry:** Season {contract_expiry}\n"
                    f"**Players Updated:** {players_updated}\n\n"
                    f"All drafted rookies now have contracts expiring after Season {contract_expiry}."
                )

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


async def setup(bot):
    await bot.add_cog(AdminCommands(bot))