import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from config import DB_PATH

class PlayerCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    
    def get_team_emoji(self, emoji_id: str) -> str:
        """Get server emoji by ID, return empty string if not found."""
        if not emoji_id:
            return ""
        try:
            # Server emojis are accessed via bot.get_emoji(id)
            emoji = self.bot.get_emoji(int(emoji_id))
            return str(emoji) if emoji else ""
        except:
            return ""

    @app_commands.command(name="player", description="Look up player information")
    @app_commands.describe(
        name1="First player name to search",
        name2="Second player name (optional)",
        name3="Third player name (optional)",
        name4="Fourth player name (optional)",
        name5="Fifth player name (optional)"
    )
    async def player_lookup(
        self,
        interaction: discord.Interaction,
        name1: str,
        name2: str = None,
        name3: str = None,
        name4: str = None,
        name5: str = None
    ):
        # Collect all non-empty search terms
        search_terms = [name for name in [name1, name2, name3, name4, name5] if name]

        async with aiosqlite.connect(DB_PATH) as db:
            all_players = []

            # Search for each term
            for search_term in search_terms:
                cursor = await db.execute(
                    """SELECT p.name, p.position, p.overall_rating, p.age, t.team_name, t.emoji_id
                       FROM players p
                       LEFT JOIN teams t ON p.team_id = t.team_id
                       WHERE p.name LIKE ?
                       ORDER BY p.overall_rating DESC""",
                    (f"%{search_term}%",)
                )
                results = await cursor.fetchall()
                all_players.extend(results)

            # Remove duplicates while preserving order (in case searches overlap)
            seen = set()
            unique_players = []
            for player in all_players:
                player_name = player[0]
                if player_name not in seen:
                    seen.add(player_name)
                    unique_players.append(player)

            if not unique_players:
                search_display = "', '".join(search_terms)
                await interaction.response.send_message(
                    f"No players found matching '{search_display}'",
                    ephemeral=True
                )
                return

            # Show list view for all results (single or multiple)
            player_list = []
            for p_name, pos, rating, age, team, emoji_id in unique_players:
                # Build team display
                if team:
                    emoji = self.get_team_emoji(emoji_id)
                    team_prefix = f"{emoji} " if emoji else ""
                else:
                    team_prefix = ""

                player_list.append(
                    f"{team_prefix}**{p_name}** - {pos} ({rating} OVR, {age}yo)"
                )

            embed = discord.Embed(
                description="\n".join(player_list),
                color=discord.Color.green()
            )

            await interaction.response.send_message(embed=embed)

    async def team_name_autocomplete(
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

    @app_commands.command(name="roster", description="View a team's roster")
    @app_commands.describe(team_name="Name of the team (leave empty for your team)")
    @app_commands.autocomplete(team_name=team_name_autocomplete)
    async def roster(self, interaction: discord.Interaction, team_name: str = None):
        async with aiosqlite.connect(DB_PATH) as db:
            # If no team specified, get user's team
            if not team_name:
                # Get all teams with their roles
                cursor = await db.execute("SELECT team_name, role_id, emoji_id FROM teams WHERE role_id IS NOT NULL")
                teams = await cursor.fetchall()

                # Check which team role the user has
                user_team = None
                user_emoji_id = None
                for t_name, role_id, e_id in teams:
                    role = interaction.guild.get_role(int(role_id))
                    if role and role in interaction.user.roles:
                        user_team = t_name
                        user_emoji_id = e_id
                        break

                if not user_team:
                    await interaction.response.send_message(
                        "❌ You don't have a team role! Specify a team name to view their roster.",
                        ephemeral=True
                    )
                    return

                # Get team ID for user's team
                cursor = await db.execute(
                    "SELECT team_id FROM teams WHERE team_name = ?",
                    (user_team,)
                )
                result = await cursor.fetchone()
                team_id = result[0]
                t_name = user_team
                emoji_id = user_emoji_id
            else:
                # Get team info by name (exact match due to autocomplete)
                cursor = await db.execute(
                    """SELECT team_id, team_name, emoji_id FROM teams WHERE team_name = ?""",
                    (team_name,)
                )
                team = await cursor.fetchone()

                if not team:
                    await interaction.response.send_message(
                        f"❌ Team '{team_name}' not found. Please select from the autocomplete suggestions.",
                        ephemeral=True
                    )
                    return

                team_id, t_name, emoji_id = team
            
            # Build team title
            emoji = self.get_team_emoji(emoji_id)
            team_title = f"{emoji} {t_name}" if emoji else t_name
            
            # Get roster
            cursor = await db.execute(
                """SELECT name, position, overall_rating, age
                   FROM players WHERE team_id = ?
                   ORDER BY position, overall_rating DESC""",
                (team_id,)
            )
            players = await cursor.fetchall()

            embed = discord.Embed(title=f"{team_title}", color=discord.Color.blue())

            if players:
                # Group by position (dynamically)
                from collections import defaultdict
                positions = defaultdict(list)

                for name, pos, rating, age in players:
                    positions[pos].append(f"**{name}** - {rating} OVR, {age}yo")

                # Import position display order
                from positions import POSITION_DISPLAY_ORDER

                # Display positions in specified order
                for pos in POSITION_DISPLAY_ORDER:
                    if pos not in positions:
                        continue
                    player_list = positions[pos]
                    embed.add_field(
                        name=f"{pos} ({len(player_list)})",
                        value="\n".join(player_list),
                        inline=False
                    )

                # Add roster size to footer
                embed.set_footer(text=f"{len(players)} players")
            else:
                embed.add_field(name="Roster", value="No players on this team", inline=False)
                embed.set_footer(text="0 players")

            await interaction.response.send_message(embed=embed)

    @app_commands.command(name="filterplayers", description="Search for players with filters")
    @app_commands.describe(
        min_rating="Minimum overall rating",
        max_rating="Maximum overall rating",
        min_age="Minimum age",
        max_age="Maximum age",
        position1="First position filter (optional)",
        position2="Second position filter (optional)",
        position3="Third position filter (optional)",
        team_name="Team name (or 'free agents')",
        limit="Max results to show (default 100)"
    )
    @app_commands.autocomplete(
        team_name=team_name_autocomplete,
        position1=position_autocomplete,
        position2=position_autocomplete,
        position3=position_autocomplete
    )
    async def search_players(
        self,
        interaction: discord.Interaction,
        min_rating: int = None,
        max_rating: int = None,
        min_age: int = None,
        max_age: int = None,
        position1: str = None,
        position2: str = None,
        position3: str = None,
        team_name: str = None,
        limit: int = 100
    ):
        async with aiosqlite.connect(DB_PATH) as db:
            # Build the query dynamically based on filters
            query = """SELECT p.name, p.position, p.overall_rating, p.age, t.team_name, t.emoji_id
                       FROM players p
                       LEFT JOIN teams t ON p.team_id = t.team_id
                       WHERE 1=1"""
            params = []
            
            if min_rating is not None:
                query += " AND p.overall_rating >= ?"
                params.append(min_rating)
            
            if max_rating is not None:
                query += " AND p.overall_rating <= ?"
                params.append(max_rating)
            
            if min_age is not None:
                query += " AND p.age >= ?"
                params.append(min_age)
            
            if max_age is not None:
                query += " AND p.age <= ?"
                params.append(max_age)

            # Handle multiple position filters
            positions_to_filter = [p for p in [position1, position2, position3] if p]
            if positions_to_filter:
                from positions import validate_position
                normalized_positions = []

                for pos in positions_to_filter:
                    is_valid, normalized_pos = validate_position(pos)
                    if not is_valid:
                        await interaction.response.send_message(
                            f"❌ Invalid position '{pos}'",
                            ephemeral=True
                        )
                        return
                    normalized_positions.append(normalized_pos)

                # Use IN clause for multiple positions
                placeholders = ", ".join(["?"] * len(normalized_positions))
                query += f" AND p.position IN ({placeholders})"
                params.extend(normalized_positions)
            
            if team_name is not None:
                if team_name.lower() in ['free agent', 'free agents', 'fa']:
                    query += " AND p.team_id IS NULL"
                else:
                    query += " AND t.team_name = ?"
                    params.append(team_name)
            
            query += " ORDER BY p.overall_rating DESC LIMIT ?"
            params.append(limit)
            
            cursor = await db.execute(query, params)
            players = await cursor.fetchall()
            
            if not players:
                await interaction.response.send_message(
                    "No players found matching those filters!"
                )
                return
            
            # Build filter description
            filters = []
            if min_rating: filters.append(f"Rating ≥{min_rating}")
            if max_rating: filters.append(f"Rating ≤{max_rating}")
            if min_age: filters.append(f"Age ≥{min_age}")
            if max_age: filters.append(f"Age ≤{max_age}")
            if positions_to_filter:
                positions_display = ", ".join(positions_to_filter)
                filters.append(f"Positions: {positions_display}")
            if team_name: filters.append(f"Team: {team_name}")
            
            filter_text = " | ".join(filters) if filters else "No filters"
            
            # Create paginated view
            view = SearchPlayersView(players, filter_text, self)
            embed = view.create_embed()
            
            await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="teamlist", description="View all teams in the league")
    async def team_list(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                """SELECT team_name, role_id, emoji_id FROM teams ORDER BY team_name"""
            )
            teams = await cursor.fetchall()
            
            if not teams:
                await interaction.response.send_message("No teams in the league yet!")
                return
            
            embed = discord.Embed(title="League Teams", color=discord.Color.gold())
            
            team_list = ""
            for name, role_id, emoji_id in teams:
                emoji = self.get_team_emoji(emoji_id)
                team_display = f"{emoji} {name}" if emoji else f"**{name}**"
                
                # Try to get role mention
                role_mention = ""
                if role_id:
                    role = interaction.guild.get_role(int(role_id))
                    if role:
                        role_mention = f" - {role.mention}"
                team_list += f"{team_display}{role_mention}\n"
            
            embed.description = team_list
            await interaction.response.send_message(embed=embed)


class SearchPlayersView(discord.ui.View):
    def __init__(self, players, filter_text, cog, players_per_page=15):
        super().__init__(timeout=180)  # 3 minute timeout
        self.players = players
        self.filter_text = filter_text
        self.cog = cog
        self.players_per_page = players_per_page
        self.current_page = 0
        self.total_pages = (len(players) + players_per_page - 1) // players_per_page
        
        # Update button states
        self.update_buttons()
    
    def update_buttons(self):
        """Update button states based on current page"""
        self.clear_items()
        
        # Only add navigation buttons if there are multiple pages
        if self.total_pages > 1:
            # Previous button
            prev_button = discord.ui.Button(
                label="◀ Previous",
                style=discord.ButtonStyle.primary,
                disabled=(self.current_page == 0)
            )
            prev_button.callback = self.previous_page
            self.add_item(prev_button)
            
            # Page indicator button (disabled, just for display)
            page_button = discord.ui.Button(
                label=f"Page {self.current_page + 1}/{self.total_pages}",
                style=discord.ButtonStyle.secondary,
                disabled=True
            )
            self.add_item(page_button)
            
            # Next button
            next_button = discord.ui.Button(
                label="Next ▶",
                style=discord.ButtonStyle.primary,
                disabled=(self.current_page >= self.total_pages - 1)
            )
            next_button.callback = self.next_page
            self.add_item(next_button)
    
    def create_embed(self):
        """Create embed for current page"""
        # Calculate slice indices
        start_idx = self.current_page * self.players_per_page
        end_idx = start_idx + self.players_per_page
        page_players = self.players[start_idx:end_idx]
        
        # Build player list with emojis
        player_lines = []
        for name, pos, rating, age, team, emoji_id in page_players:
            if team:
                emoji = self.cog.get_team_emoji(emoji_id)
                team_prefix = f"{emoji} " if emoji else ""
            else:
                team_prefix = ""
            player_lines.append(f"{team_prefix}**{name}** - {pos} ({rating} OVR, {age}yo)")
        
        player_text = "\n".join(player_lines)
        
        embed = discord.Embed(
            title=f"Player Search Results ({len(self.players)} found)",
            description=player_text,
            color=discord.Color.purple()
        )
        
        embed.add_field(name="Filters", value=self.filter_text, inline=False)
        
        if self.total_pages > 1:
            embed.set_footer(text=f"Page {self.current_page + 1}/{self.total_pages} • Showing {start_idx + 1}-{min(end_idx, len(self.players))} of {len(self.players)}")
        else:
            embed.set_footer(text=f"Showing all {len(self.players)} results")
        
        return embed
    
    async def previous_page(self, interaction: discord.Interaction):
        """Go to previous page"""
        if self.current_page > 0:
            self.current_page -= 1
            self.update_buttons()
            embed = self.create_embed()
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.defer()
    
    async def next_page(self, interaction: discord.Interaction):
        """Go to next page"""
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            self.update_buttons()
            embed = self.create_embed()
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.defer()


async def setup(bot):
    await bot.add_cog(PlayerCommands(bot))