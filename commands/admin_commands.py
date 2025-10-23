import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
import pandas as pd
import io
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
        lineups_channel="Channel where all lineup submissions are posted"
    )
    async def config(
        self,
        interaction: discord.Interaction,
        lineups_channel: discord.TextChannel = None
    ):
        # If no parameters provided, show current settings
        if lineups_channel is None:
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "SELECT setting_key, setting_value FROM settings WHERE setting_key = 'lineups_channel_id'"
                )
                result = await cursor.fetchone()

            embed = discord.Embed(title="⚙️ Bot Configuration", color=discord.Color.blue())

            if result and result[1]:
                channel = interaction.guild.get_channel(int(result[1]))
                channel_display = channel.mention if channel else f"<#{result[1]}> (channel not found)"
            else:
                channel_display = "*Not set*"

            embed.add_field(name="Lineups Channel", value=channel_display, inline=False)
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
            if duplicate:
                await interaction.response.send_message(
                    f"⚠️ Warning: A player named **{duplicate[1]}** already exists in the system (ID: {duplicate[0]}). Consider using a unique name.",
                    ephemeral=True
                )
                return

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
            await interaction.response.send_message(
                f"✅ Added **{name}** ({normalized_pos}, {rating} OVR, {age}yo) {team_text}!"
            )

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
        team="New team (optional, use 'delisted' to release)"
    )
    @app_commands.autocomplete(position=position_autocomplete, name=player_name_autocomplete)
    async def update_player(
        self,
        interaction: discord.Interaction,
        name: str,
        new_name: str = None,
        ovr: int = None,
        age: int = None,
        position: str = None,
        team: str = None
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
                """SELECT player_id, name, overall_rating, age, position, team_id
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

            player_id, player_name, old_rating, old_age, old_position, old_team_id = player

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

            if new_name is not None:
                # Check for duplicate names
                cursor = await db.execute(
                    "SELECT player_id, name FROM players WHERE LOWER(name) = LOWER(?) AND player_id != ?",
                    (new_name, player_id)
                )
                duplicate = await cursor.fetchone()
                if duplicate:
                    await interaction.response.send_message(
                        f"⚠️ Warning: A player named **{duplicate[1]}** already exists in the system (ID: {duplicate[0]}). Consider using a unique name.",
                        ephemeral=True
                    )
                    return

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

            await interaction.response.send_message(response)

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
                
                # Export Players with Lineup Positions
                cursor = await db.execute(
                    """SELECT p.name as Name, t.team_name as Team, p.age as Age, 
                              p.position as Pos, p.overall_rating as OVR, l.position_name as Lineup_Pos
                       FROM players p
                       LEFT JOIN teams t ON p.team_id = t.team_id
                       LEFT JOIN lineups l ON p.player_id = l.player_id
                       ORDER BY p.name"""
                )
                players = await cursor.fetchall()
                players_df = pd.DataFrame(players, columns=['Name', 'Team', 'Age', 'Pos', 'OVR', 'Lineup_Pos'])
                players_df['Team'] = players_df['Team'].fillna('')
                players_df['Lineup_Pos'] = players_df['Lineup_Pos'].fillna('')
                
                # Export Lineups as read-only summary view (formatted by team)
                cursor = await db.execute(
                    """SELECT t.team_name as Team_Name, l.position_name as Position,
                              p.name as Player_Name, p.position as Player_Position,
                              p.overall_rating as OVR
                       FROM lineups l
                       JOIN teams t ON l.team_id = t.team_id
                       JOIN players p ON l.player_id = p.player_id
                       ORDER BY t.team_name, l.slot_number"""
                )
                lineups = await cursor.fetchall()
                lineups_df = pd.DataFrame(lineups, columns=['Team_Name', 'Position', 'Player_Name', 'Player_Position', 'OVR'])

                # Export Seasons
                cursor = await db.execute(
                    """SELECT season_number as Season, current_round as Current_Round,
                              regular_rounds as Regular_Rounds, total_rounds as Total_Rounds,
                              round_name as Round_Name, status as Status
                       FROM seasons ORDER BY season_number"""
                )
                seasons = await cursor.fetchall()
                seasons_df = pd.DataFrame(seasons, columns=['Season', 'Current_Round', 'Regular_Rounds', 'Total_Rounds', 'Round_Name', 'Status'])

                # Export Injuries
                cursor = await db.execute(
                    """SELECT p.name as Player_Name, t.team_name as Team,
                              i.injury_type as Injury_Type, i.injury_round as Injury_Round,
                              i.recovery_rounds as Recovery_Rounds, i.return_round as Return_Round,
                              i.status as Status
                       FROM injuries i
                       JOIN players p ON i.player_id = p.player_id
                       LEFT JOIN teams t ON p.team_id = t.team_id
                       ORDER BY i.status, i.return_round"""
                )
                injuries = await cursor.fetchall()
                injuries_df = pd.DataFrame(injuries, columns=['Player_Name', 'Team', 'Injury_Type', 'Injury_Round', 'Recovery_Rounds', 'Return_Round', 'Status'])
                injuries_df['Team'] = injuries_df['Team'].fillna('Delisted')

            # Create Excel file in memory
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                teams_df.to_excel(writer, sheet_name='Teams', index=False)
                players_df.to_excel(writer, sheet_name='Players', index=False)
                lineups_df.to_excel(writer, sheet_name='Lineups', index=False)
                seasons_df.to_excel(writer, sheet_name='Seasons', index=False)
                injuries_df.to_excel(writer, sheet_name='Injuries', index=False)
                
                # Add instructions sheet
                instructions = pd.DataFrame({
                    'IMPORTANT INSTRUCTIONS': [
                        '1. BEFORE editing the Teams sheet:',
                        '   - Select the entire Role_ID column',
                        '   - Right-click > Format Cells > Text',
                        '   - Then edit your role IDs',
                        '',
                        '2. This prevents Excel from converting long numbers to scientific notation',
                        '',
                        '3. To import: Use /importdata command and attach this file',
                        '',
                        '4. Teams sheet columns:',
                        '   - Team_Name: Your team name',
                        '   - Role_ID: Discord role ID (copy from Server Settings > Roles)',
                        '   - Emoji_ID: Server emoji ID (right-click emoji > Copy Link > ID at end)',
                        '',
                        '5. Players sheet columns:',
                        '   - Name: Player name',
                        '   - Team: Team name (leave blank for delisted players)',
                        '   - Age: Player age',
                        '   - Pos: Position (MID, KEY FWD, RUCK, etc.)',
                        '   - OVR: Overall rating (1-100)',
                        '   - Lineup_Pos: Field position (FB, CHB, LW, etc. - leave blank for bench)',
                        '',
                        '6. Lineups sheet (read-only summary):',
                        '   - Shows team lineups formatted by team for easy viewing',
                        '   - Edit lineup positions in the Players sheet instead',
                    ]
                })
                instructions.to_excel(writer, sheet_name='README', index=False)
            output.seek(0)
            
            # Send file
            file = discord.File(output, filename='league_data.xlsx')
            await interaction.followup.send(
                f"✅ Exported {len(teams_df)} teams, {len(players_df)} players, {len(lineups_df)} lineup positions, {len(seasons_df)} seasons, and {len(injuries_df)} injuries",
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
                
                # Import Players
                try:
                    players_df = pd.read_excel(excel_file, sheet_name='Players')
                    
                    # Get team mapping
                    cursor = await db.execute("SELECT team_id, team_name FROM teams")
                    teams = await cursor.fetchall()
                    team_map = {name.lower(): id for id, name in teams}
                    
                    # Valid lineup positions
                    valid_lineup_positions = [
                        "LBP", "FB", "RBP", "LHB", "CHB", "RHB",
                        "LW", "C", "RW", "LHF", "CHF", "RHF",
                        "LFP", "FF", "RFP", "R", "RR", "RO",
                        "INT1", "INT2", "INT3", "INT4", "SUB"
                    ]
                    
                    for _, row in players_df.iterrows():
                        try:
                            name = str(row['Name']).strip()
                            position = str(row['Pos']).strip()
                            rating = int(row['OVR'])
                            age = int(row['Age'])
                            
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
                            
                            # Get lineup position if provided
                            lineup_pos = None
                            if 'Lineup_Pos' in players_df.columns and pd.notna(row['Lineup_Pos']) and str(row['Lineup_Pos']).strip():
                                lineup_pos = str(row['Lineup_Pos']).strip().upper()
                                if lineup_pos not in valid_lineup_positions:
                                    errors.append(f"Player '{name}': Invalid lineup position '{lineup_pos}'")
                                    lineup_pos = None
                            
                            # Check if player exists
                            cursor = await db.execute("SELECT player_id FROM players WHERE name = ?", (name,))
                            existing = await cursor.fetchone()

                            # Check for case-insensitive duplicates (different from exact match)
                            cursor = await db.execute("SELECT player_id, name FROM players WHERE LOWER(name) = LOWER(?) AND name != ?", (name, name))
                            duplicate = await cursor.fetchone()
                            if duplicate:
                                duplicate_warnings.append(f"'{name}' is similar to existing player '{duplicate[1]}' (ID: {duplicate[0]})")

                            if existing:
                                player_id = existing[0]
                                await db.execute(
                                    """UPDATE players 
                                       SET position = ?, overall_rating = ?, age = ?, team_id = ?
                                       WHERE name = ?""",
                                    (normalized_pos, rating, age, team_id, name)
                                )
                                players_updated += 1
                            else:
                                await db.execute(
                                    """INSERT INTO players (name, position, overall_rating, age, team_id)
                                       VALUES (?, ?, ?, ?, ?)""",
                                    (name, normalized_pos, rating, age, team_id)
                                )
                                # Get the newly inserted player_id
                                cursor = await db.execute("SELECT last_insert_rowid()")
                                player_id = (await cursor.fetchone())[0]
                                players_added += 1
                            
                            # Handle lineup position
                            if lineup_pos and team_id:
                                # Get slot number for this position
                                slot_number = valid_lineup_positions.index(lineup_pos) + 1
                                
                                # Remove player from any existing lineup position
                                await db.execute(
                                    "DELETE FROM lineups WHERE player_id = ?",
                                    (player_id,)
                                )
                                
                                # Add to new position
                                await db.execute(
                                    """INSERT OR REPLACE INTO lineups (team_id, player_id, slot_number, position_name)
                                       VALUES (?, ?, ?, ?)""",
                                    (team_id, player_id, slot_number, lineup_pos)
                                )
                            elif not lineup_pos:
                                # If no lineup position specified, remove from lineup
                                await db.execute(
                                    "DELETE FROM lineups WHERE player_id = ?",
                                    (player_id,)
                                )
                                
                        except Exception as e:
                            errors.append(f"Player '{name}': {str(e)}")
                    
                    await db.commit()
                except Exception as e:
                    errors.append(f"Players sheet error: {str(e)}")
            
            # Build response
            response = "✅ **Import Complete!**\n\n"
            response += f"**Teams:** {teams_added} added, {teams_updated} updated\n"
            response += f"**Players:** {players_added} added, {players_updated} updated\n"

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

async def setup(bot):
    await bot.add_cog(AdminCommands(bot))