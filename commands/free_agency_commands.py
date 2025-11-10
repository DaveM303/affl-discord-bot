import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
from config import DB_PATH, ADMIN_ROLE_ID
import json
from datetime import datetime

class FreeAgencyCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def team_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for team names"""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute("SELECT team_name FROM teams ORDER BY team_name")
                teams = await cursor.fetchall()
                return [
                    app_commands.Choice(name=team[0], value=team[0])
                    for team in teams
                    if current.lower() in team[0].lower()
                ][:25]
        except:
            return []

    async def free_agent_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for free agents"""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get current season (active or offseason)
                cursor = await db.execute(
                    """SELECT season_number FROM seasons
                       ORDER BY
                           CASE status
                               WHEN 'active' THEN 1
                               WHEN 'offseason' THEN 2
                               ELSE 3
                           END,
                           season_number DESC
                       LIMIT 1"""
                )
                season_result = await cursor.fetchone()
                if not season_result:
                    return []
                current_season = season_result[0]

                # Get players whose contracts expired (contract_expiry = current season during offseason)
                cursor = await db.execute(
                    """SELECT p.player_id, p.name, p.position, p.overall_rating, p.age, t.team_name
                       FROM players p
                       JOIN teams t ON p.team_id = t.team_id
                       WHERE p.contract_expiry = ?
                       ORDER BY p.name""",
                    (current_season,)
                )
                free_agents = await cursor.fetchall()

                choices = []
                for player_id, name, pos, ovr, age, team in free_agents:
                    display = f"{name} ({pos}, {age}, {ovr}) - {team}"
                    if current.lower() in display.lower():
                        choices.append(app_commands.Choice(name=display, value=str(player_id)))

                return choices[:25]
        except:
            return []

    async def get_auctions_log_channel(self, db):
        """Get the auctions log channel from settings"""
        cursor = await db.execute(
            "SELECT setting_value FROM settings WHERE setting_key = 'auctions_log_channel_id'"
        )
        result = await cursor.fetchone()
        if result and result[0]:
            try:
                return self.bot.get_channel(int(result[0]))
            except:
                return None
        return None

    async def get_bot_logs_channel(self, db):
        """Get the bot logs channel from settings"""
        cursor = await db.execute(
            "SELECT setting_value FROM settings WHERE setting_key = 'bot_logs_channel_id'"
        )
        result = await cursor.fetchone()
        if result and result[0]:
            try:
                return self.bot.get_channel(int(result[0]))
            except:
                return None
        return None

    async def get_contract_years_for_age(self, db, age):
        """Get contract years based on player age from contract_config table"""
        cursor = await db.execute(
            """SELECT contract_years FROM contract_config
               WHERE min_age <= ? AND (max_age >= ? OR max_age IS NULL)
               LIMIT 1""",
            (age, age)
        )
        result = await cursor.fetchone()
        return result[0] if result else 2  # Default to 2 years if not found

    async def get_compensation_band(self, db, age, ovr):
        """Get compensation band based on player age and OVR from compensation_chart table
        NULL max values mean single-value ranges (e.g., max_ovr IS NULL means only min_ovr)"""
        cursor = await db.execute(
            """SELECT compensation_band FROM compensation_chart
               WHERE min_age <= ? AND COALESCE(max_age, min_age) >= ?
               AND min_ovr <= ? AND COALESCE(max_ovr, min_ovr) >= ?
               ORDER BY compensation_band ASC
               LIMIT 1""",
            (age, age, ovr, ovr)
        )
        result = await cursor.fetchone()
        return result[0] if result else None  # None means no compensation

    async def calculate_free_resign_allowance(self, db, team_id, current_season):
        """Calculate how many free re-signs a team gets based on their free agents
        Formula: 0.5 per Band 1 player + 0.25 per Band 2 player, rounded to nearest (0.5 rounds down)"""
        # Get all free agents for this team
        cursor = await db.execute(
            """SELECT p.player_id, p.age, p.overall_rating
               FROM players p
               WHERE p.team_id = ? AND p.contract_expiry = ?""",
            (team_id, current_season)
        )
        free_agents = await cursor.fetchall()

        credits = 0.0
        for player_id, age, ovr in free_agents:
            band = await self.get_compensation_band(db, age, ovr)
            if band == 1:
                credits += 0.5
            elif band == 2:
                credits += 0.25

        # Round to nearest whole number, with 0.5 rounding down
        # Examples: 0.5→0, 0.51→1, 1.25→1, 1.5→1, 1.75→2, 2.5→2
        import math
        if credits % 1 == 0.5:
            # Exactly 0.5, round down
            return int(credits)
        else:
            # Otherwise, round to nearest
            return round(credits)

    async def process_free_resigns(self, db, period_id, current_season):
        """Process all confirmed free re-signs and assign contracts"""
        # Get all confirmed free re-signs
        cursor = await db.execute(
            """SELECT r.player_id, p.age
               FROM free_agency_resigns r
               JOIN players p ON r.player_id = p.player_id
               WHERE r.period_id = ? AND r.confirmed = 1""",
            (period_id,)
        )
        resigns = await cursor.fetchall()

        for player_id, age in resigns:
            # Get contract length based on age
            contract_years = await self.get_contract_years_for_age(db, age)

            # Calculate new contract expiry
            # current_season is the season that just ended (Offseason 9 means Season 9 just ended)
            # Adding contract_years gives us the last season they'll play under the new contract
            new_contract_expiry = current_season + contract_years

            # Update player's contract
            await db.execute(
                "UPDATE players SET contract_expiry = ? WHERE player_id = ?",
                (new_contract_expiry, player_id)
            )

        await db.commit()

    async def log_free_resign_results(self, db, period_id, current_season):
        """Log free re-sign results to auctions channel"""
        log_channel = await self.get_auctions_log_channel(db)
        if not log_channel:
            return

        # Get all confirmed free re-signs
        cursor = await db.execute(
            """SELECT p.name, p.position, p.age, p.overall_rating, t.team_name, t.emoji_id, p.contract_expiry
               FROM free_agency_resigns r
               JOIN players p ON r.player_id = p.player_id
               JOIN teams t ON p.team_id = t.team_id
               WHERE r.period_id = ? AND r.confirmed = 1
               ORDER BY t.team_name, p.name""",
            (period_id,)
        )
        resigns = await cursor.fetchall()

        if not resigns:
            return

        # Build embed
        embed = discord.Embed(
            title="📝 Free Re-Signs Completed",
            description=f"**Season {current_season}** - {len(resigns)} player{'s' if len(resigns) != 1 else ''} re-signed for free",
            color=discord.Color.blue()
        )

        # Group by team
        teams_dict = {}
        for name, pos, age, ovr, team_name, emoji_id, contract_expiry in resigns:
            if team_name not in teams_dict:
                teams_dict[team_name] = {
                    'emoji_id': emoji_id,
                    'players': []
                }
            teams_dict[team_name]['players'].append((name, pos, age, ovr, contract_expiry))

        # Add field for each team
        for team_name in sorted(teams_dict.keys()):
            team_data = teams_dict[team_name]

            # Get emoji
            emoji_str = ""
            if team_data['emoji_id']:
                try:
                    emoji = self.bot.get_emoji(int(team_data['emoji_id']))
                    if emoji:
                        emoji_str = f"{emoji} "
                except:
                    pass

            player_lines = []
            for name, pos, age, ovr, contract_expiry in team_data['players']:
                contract_years = contract_expiry - current_season
                player_lines.append(f"**{name}** ({pos}, {age}, {ovr}) - {contract_years}yr contract")

            embed.add_field(
                name=f"{emoji_str}{team_name}",
                value="\n".join(player_lines),
                inline=False
            )

        try:
            await log_channel.send(embed=embed)
        except Exception as e:
            print(f"Error logging free re-sign results: {e}")

    async def log_winning_bids(self, db, period_id, current_season):
        """Log winning bids to auctions channel"""
        log_channel = await self.get_auctions_log_channel(db)
        if not log_channel:
            print("No auctions log channel configured")
            return

        # Get all winning bids
        cursor = await db.execute(
            """SELECT p.name, p.position, p.age, p.overall_rating,
                      orig_team.team_name as original_team, orig_team.emoji_id as orig_emoji,
                      bid_team.team_name as bidding_team, bid_team.emoji_id as bid_emoji,
                      r.winning_bid
               FROM free_agency_results r
               JOIN players p ON r.player_id = p.player_id
               JOIN teams orig_team ON r.original_team_id = orig_team.team_id
               LEFT JOIN teams bid_team ON r.winning_team_id = bid_team.team_id
               WHERE r.period_id = ? AND r.winning_team_id IS NOT NULL
               ORDER BY r.winning_bid DESC, p.name""",
            (period_id,)
        )
        winning_bids = await cursor.fetchall()

        if not winning_bids:
            return

        # Build embed
        embed = discord.Embed(
            title=f"Season {current_season} Auctions - Matching Period",
            description="**Winning bids:**",
            color=discord.Color.gold()
        )

        # Calculate team points for match checking
        cursor = await db.execute(
            """SELECT team_id FROM teams"""
        )
        all_teams = await cursor.fetchall()

        team_points = {}
        for (team_id,) in all_teams:
            # Calculate spent points
            cursor = await db.execute(
                """SELECT COALESCE(SUM(bid_amount), 0) FROM free_agency_bids
                   WHERE period_id = ? AND team_id = ? AND status = 'active'""",
                (period_id, team_id)
            )
            spent = (await cursor.fetchone())[0]
            team_points[team_id] = 300 - spent

        player_lines = []
        for name, pos, age, ovr, orig_team, orig_emoji, bid_team, bid_emoji, winning_bid in winning_bids:
            # Get emojis
            orig_emoji_str = ""
            if orig_emoji:
                try:
                    emoji = self.bot.get_emoji(int(orig_emoji))
                    if emoji:
                        orig_emoji_str = f"{emoji} "
                except:
                    pass

            bid_emoji_str = ""
            if bid_emoji:
                try:
                    emoji = self.bot.get_emoji(int(bid_emoji))
                    if emoji:
                        bid_emoji_str = f"{emoji} "
                except:
                    pass

            # Check if RFA (can match at 80%)
            is_rfa = age <= 25
            match_cost = round(winning_bid * 0.8) if is_rfa else winning_bid

            # Determine if team can match
            cursor = await db.execute(
                "SELECT team_id FROM teams WHERE team_name = ?",
                (orig_team,)
            )
            orig_team_id = (await cursor.fetchone())[0]
            remaining_points = team_points.get(orig_team_id, 0)

            if remaining_points >= match_cost:
                match_text = f"({orig_emoji_str}can match {match_cost}pts)"
            else:
                match_text = f"({orig_emoji_str}can't afford to match)"

            rfa_tag = " [RFA]" if is_rfa else ""
            player_lines.append(
                f"{orig_emoji_str}**{name}**{rfa_tag} ({pos}, {age}, {ovr})\n"
                f"Winning bid: {bid_emoji_str}{winning_bid}pts {match_text}"
            )

        # Add all bids as one long field (no splitting needed - much more compact now)
        if player_lines:
            embed.description += "\n\n" + "\n\n".join(player_lines)

        try:
            await log_channel.send(embed=embed)
        except Exception as e:
            print(f"Error logging winning bids: {e}")

    async def log_final_movements(self, db, period_id, current_season):
        """Log final player movements and compensation picks to auctions channel"""
        log_channel = await self.get_auctions_log_channel(db)
        if not log_channel:
            print("No auctions log channel configured for final movements")
            return

        # Get only players who moved clubs (not matched, has new team)
        cursor = await db.execute(
            """SELECT p.name, p.position, p.age, p.overall_rating,
                      orig_team.team_name as original_team, orig_team.emoji_id as orig_emoji,
                      new_team.team_name as new_team, new_team.emoji_id as new_emoji,
                      r.compensation_band, r.compensation_pick_id
               FROM free_agency_results r
               JOIN players p ON r.player_id = p.player_id
               JOIN teams orig_team ON r.original_team_id = orig_team.team_id
               LEFT JOIN teams new_team ON r.winning_team_id = new_team.team_id
               WHERE r.period_id = ? AND r.matched = 0 AND r.winning_team_id IS NOT NULL
               ORDER BY p.name""",
            (period_id,)
        )
        transfers = await cursor.fetchall()

        if not transfers:
            # No movements to log
            return

        # Build embed
        embed = discord.Embed(
            title=f"Season {current_season} Free Agency Player Movements",
            color=discord.Color.green()
        )

        # Build movement lines
        movement_lines = []
        for name, pos, age, ovr, orig_team, orig_emoji, new_team, new_emoji, comp_band, comp_pick_id in transfers:
            # Get emojis
            orig_emoji_str = ""
            if orig_emoji:
                try:
                    emoji = self.bot.get_emoji(int(orig_emoji))
                    if emoji:
                        orig_emoji_str = f"{emoji} "
                except:
                    pass

            new_emoji_str = ""
            if new_emoji:
                try:
                    emoji = self.bot.get_emoji(int(new_emoji))
                    if emoji:
                        new_emoji_str = f"{emoji} "
                except:
                    pass

            # Build player line
            player_line = f"{orig_emoji_str}**{name}** ({pos}, {age}, {ovr}) → {new_emoji_str}{new_team}"

            # Add compensation line if applicable
            if comp_band and comp_pick_id:
                # Get pick number from draft_picks table
                cursor = await db.execute(
                    "SELECT pick_number FROM draft_picks WHERE pick_id = ?",
                    (comp_pick_id,)
                )
                pick_result = await cursor.fetchone()
                if pick_result:
                    pick_num = pick_result[0]
                    comp_line = f"└ {orig_emoji_str}receive Band {comp_band} compensation (pick {pick_num})"
                    player_line += f"\n{comp_line}"

            movement_lines.append(player_line)

        # Split into fields if needed
        if movement_lines:
            movement_chunks = self._split_field_content(movement_lines, "", max_length=1000)
            for i, (field_name, value) in enumerate(movement_chunks):
                # Use blank field name for all chunks to keep it clean
                embed.add_field(name="\u200b" if i > 0 else "", value=value, inline=False)

        try:
            print(f"Attempting to log final movements to channel {log_channel.id}")
            await log_channel.send(embed=embed)
            print("Final movements logged successfully")
        except Exception as e:
            print(f"Error logging final movements: {e}")
            import traceback
            traceback.print_exc()

    def _split_field_content(self, lines, field_name, max_length=1000):
        """Split content into multiple fields if it exceeds Discord's limit"""
        chunks = []
        current_chunk = []
        current_length = 0

        for line in lines:
            line_length = len(line) + 1  # +1 for newline
            if current_length + line_length > max_length and current_chunk:  # Leave buffer
                # Add current chunk
                chunk_num = len(chunks) + 1
                name = f"{field_name} (Part {chunk_num})" if chunks else field_name
                chunks.append((name, "\n".join(current_chunk)))
                current_chunk = []
                current_length = 0

            current_chunk.append(line)
            current_length += line_length

        # Add remaining
        if current_chunk:
            chunk_num = len(chunks) + 1
            name = f"{field_name} (Part {chunk_num})" if chunks else field_name
            chunks.append((name, "\n".join(current_chunk)))

        return chunks

    @app_commands.command(name="viewfreeagents", description="View players whose contracts expire this season")
    @app_commands.describe(team="Filter by team (optional)")
    @app_commands.autocomplete(team=team_autocomplete)
    async def view_free_agents(self, interaction: discord.Interaction, team: str = None):
        await interaction.response.defer(ephemeral=True)

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get current season (active or offseason)
                cursor = await db.execute(
                    """SELECT season_number FROM seasons
                       ORDER BY
                           CASE status
                               WHEN 'active' THEN 1
                               WHEN 'offseason' THEN 2
                               ELSE 3
                           END,
                           season_number DESC
                       LIMIT 1"""
                )
                season_result = await cursor.fetchone()
                if not season_result:
                    await interaction.followup.send("❌ No active season found!")
                    return

                current_season = season_result[0]

                # Build query based on team filter
                if team:
                    # Verify team exists
                    cursor = await db.execute(
                        "SELECT team_id FROM teams WHERE LOWER(team_name) = LOWER(?)",
                        (team,)
                    )
                    team_result = await cursor.fetchone()
                    if not team_result:
                        await interaction.followup.send(f"❌ Team '{team}' not found!")
                        return

                    # Get free agents for specific team
                    cursor = await db.execute(
                        """SELECT p.player_id, p.name, p.position, p.overall_rating, p.age, t.team_name, t.emoji_id
                           FROM players p
                           JOIN teams t ON p.team_id = t.team_id
                           WHERE p.contract_expiry = ? AND LOWER(t.team_name) = LOWER(?)
                           ORDER BY p.overall_rating DESC, p.name""",
                        (current_season, team)
                    )
                else:
                    # Get all free agents
                    cursor = await db.execute(
                        """SELECT p.player_id, p.name, p.position, p.overall_rating, p.age, t.team_name, t.emoji_id
                           FROM players p
                           JOIN teams t ON p.team_id = t.team_id
                           WHERE p.contract_expiry = ?
                           ORDER BY t.team_name, p.overall_rating DESC, p.name""",
                        (current_season,)
                    )

                free_agents = await cursor.fetchall()

                if not free_agents:
                    if team:
                        await interaction.followup.send(f"No free agents found for {team} in Season {current_season}.")
                    else:
                        await interaction.followup.send(f"No free agents found for Season {current_season}.")
                    return

                # Group by team
                teams_dict = {}
                for _, name, pos, ovr, age, team_name, emoji_id in free_agents:
                    if team_name not in teams_dict:
                        teams_dict[team_name] = {
                            'emoji_id': emoji_id,
                            'players': []
                        }
                    teams_dict[team_name]['players'].append((name, pos, age, ovr))

                # Create paginated view
                view = FreeAgentsView(self.bot, teams_dict, current_season, len(free_agents))
                embed = view.create_embed()
                await interaction.followup.send(embed=embed, view=view)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}")

    @app_commands.command(name="placebid", description="Place a bid on an opposition free agent")
    @app_commands.describe(
        player="The free agent to bid on",
        amount="Bid amount (1-300 points)"
    )
    @app_commands.autocomplete(player=free_agent_autocomplete)
    async def place_bid(self, interaction: discord.Interaction, player: str, amount: int):
        await interaction.response.defer(ephemeral=True)

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get current season (active or offseason)
                cursor = await db.execute(
                    """SELECT season_number FROM seasons
                       ORDER BY
                           CASE status
                               WHEN 'active' THEN 1
                               WHEN 'offseason' THEN 2
                               ELSE 3
                           END,
                           season_number DESC
                       LIMIT 1"""
                )
                season_result = await cursor.fetchone()
                if not season_result:
                    await interaction.followup.send("❌ No active season found!")
                    return
                current_season = season_result[0]

                # Check if there's an active bidding period
                cursor = await db.execute(
                    """SELECT period_id, auction_points FROM free_agency_periods
                       WHERE season_number = ? AND status = 'bidding'""",
                    (current_season,)
                )
                period_result = await cursor.fetchone()
                if not period_result:
                    await interaction.followup.send("❌ No active bidding period!")
                    return

                period_id, max_points = period_result

                # Get player details
                player_id = int(player)
                cursor = await db.execute(
                    """SELECT p.name, p.position, p.age, p.overall_rating, p.team_id, p.contract_expiry,
                              t.team_name, t.emoji_id
                       FROM players p
                       JOIN teams t ON p.team_id = t.team_id
                       WHERE p.player_id = ?""",
                    (player_id,)
                )
                player_data = await cursor.fetchone()
                if not player_data:
                    await interaction.followup.send("❌ Player not found!")
                    return

                player_name, pos, age, ovr, player_team_id, contract_expiry, team_name, emoji_id = player_data

                # Verify player is a free agent (contract expired = contract_expiry matches current season)
                if contract_expiry != current_season:
                    await interaction.followup.send(f"❌ {player_name} is not a free agent this season!")
                    return

                # Get user's team
                user_team_id = None
                for role in interaction.user.roles:
                    cursor = await db.execute(
                        "SELECT team_id FROM teams WHERE role_id = ?",
                        (str(role.id),)
                    )
                    team_result = await cursor.fetchone()
                    if team_result:
                        user_team_id = team_result[0]
                        break

                if not user_team_id:
                    await interaction.followup.send("❌ You don't have a team role!")
                    return

                # Check player is not on user's team
                if player_team_id == user_team_id:
                    await interaction.followup.send(f"❌ You cannot bid on your own players! Wait for the matching period.")
                    return

                # Validate bid amount
                if amount < 1 or amount > max_points:
                    await interaction.followup.send(f"❌ Bid amount must be between 1 and {max_points} points!")
                    return

                # Calculate user's remaining points (excluding current player)
                cursor = await db.execute(
                    """SELECT COALESCE(SUM(bid_amount), 0) FROM free_agency_bids
                       WHERE period_id = ? AND team_id = ? AND status = 'active'
                       AND player_id != ?""",
                    (period_id, user_team_id, player_id)
                )
                spent_points = (await cursor.fetchone())[0]

                # Check if user already has a bid on this player (for validation)
                cursor = await db.execute(
                    """SELECT bid_amount FROM free_agency_bids
                       WHERE period_id = ? AND team_id = ? AND player_id = ?""",
                    (period_id, user_team_id, player_id)
                )
                existing_bid = await cursor.fetchone()

                remaining_points = max_points - spent_points

                if amount > remaining_points:
                    await interaction.followup.send(
                        f"❌ Insufficient points!\n\n"
                        f"**Available:** {remaining_points} points\n"
                        f"**Bid Amount:** {amount} points\n\n"
                        f"Use `/auctionsmenu` to view your bids."
                    )
                    return

                # Place or update bid
                if existing_bid:
                    await db.execute(
                        """UPDATE free_agency_bids
                           SET bid_amount = ?, updated_at = CURRENT_TIMESTAMP
                           WHERE period_id = ? AND team_id = ? AND player_id = ?""",
                        (amount, period_id, user_team_id, player_id)
                    )
                    action = "Updated"
                else:
                    await db.execute(
                        """INSERT INTO free_agency_bids (period_id, team_id, player_id, bid_amount)
                           VALUES (?, ?, ?, ?)""",
                        (period_id, user_team_id, player_id, amount)
                    )
                    action = "Placed"

                await db.commit()

                # Log to bot logs channel
                log_channel = await self.get_bot_logs_channel(db)
                if log_channel:
                    # Get bidding team info
                    cursor = await db.execute("SELECT team_name, emoji_id FROM teams WHERE team_id = ?", (user_team_id,))
                    bidding_team_data = await cursor.fetchone()
                    bidding_team_name = bidding_team_data[0] if bidding_team_data else "Unknown Team"
                    bidding_emoji_id = bidding_team_data[1] if bidding_team_data and bidding_team_data[1] else None

                    bidding_emoji_str = ""
                    if bidding_emoji_id:
                        try:
                            emoji = self.bot.get_emoji(int(bidding_emoji_id))
                            if emoji:
                                bidding_emoji_str = f"{emoji} "
                        except:
                            pass

                    # Get player's original team emoji
                    original_emoji_str = ""
                    if emoji_id:
                        try:
                            emoji = self.bot.get_emoji(int(emoji_id))
                            if emoji:
                                original_emoji_str = f"{emoji} "
                        except:
                            pass

                    action_text = "updated their bid on" if existing_bid else "placed a bid on"
                    await log_channel.send(f"💰 {bidding_emoji_str}**{bidding_team_name}** {action_text} {original_emoji_str}**{player_name}**: {amount}pts")

                # Get emoji
                emoji_str = ""
                if emoji_id:
                    try:
                        emoji = self.bot.get_emoji(int(emoji_id))
                        if emoji:
                            emoji_str = f"{emoji} "
                    except:
                        pass

                # Calculate new remaining points after this bid
                # spent_points already excludes the old bid if it existed
                new_remaining = max_points - (spent_points + amount)

                embed = discord.Embed(
                    title=f"✅ Bid {action}!",
                    color=discord.Color.green()
                )
                embed.add_field(
                    name="Player",
                    value=f"{emoji_str}**{player_name}** ({pos}, {age}, {ovr}) - {team_name}",
                    inline=False
                )
                embed.add_field(
                    name="Bid",
                    value=f"{amount} points",
                    inline=True
                )
                embed.add_field(
                    name="Remaining",
                    value=f"{new_remaining} points",
                    inline=True
                )
                embed.set_footer(text="View all bids: /auctionsmenu")

                await interaction.followup.send(embed=embed)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}")

    @app_commands.command(name="auctionsmenu", description="View your bids and remaining auction points")
    async def auctions_menu(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get current season (active or offseason)
                cursor = await db.execute(
                    """SELECT season_number FROM seasons
                       ORDER BY
                           CASE status
                               WHEN 'active' THEN 1
                               WHEN 'offseason' THEN 2
                               ELSE 3
                           END,
                           season_number DESC
                       LIMIT 1"""
                )
                season_result = await cursor.fetchone()
                if not season_result:
                    await interaction.followup.send("❌ No active season found!")
                    return
                current_season = season_result[0]

                # Check if there's an active bidding period
                cursor = await db.execute(
                    """SELECT period_id, auction_points, status FROM free_agency_periods
                       WHERE season_number = ?""",
                    (current_season,)
                )
                period_result = await cursor.fetchone()
                if not period_result:
                    await interaction.followup.send("❌ No free agency period active!")
                    return

                period_id, max_points, period_status = period_result

                if period_status not in ('bidding', 'matching'):
                    await interaction.followup.send(f"❌ No active free agency period! Current status: {period_status}")
                    return

                # Get user's team
                user_team_id = None
                user_team_name = None
                for role in interaction.user.roles:
                    cursor = await db.execute(
                        "SELECT team_id, team_name FROM teams WHERE role_id = ?",
                        (str(role.id),)
                    )
                    team_result = await cursor.fetchone()
                    if team_result:
                        user_team_id, user_team_name = team_result
                        break

                if not user_team_id:
                    await interaction.followup.send("❌ You don't have a team role!")
                    return

                # Get user's bids
                cursor = await db.execute(
                    """SELECT b.bid_id, b.player_id, b.bid_amount, p.name, p.position, p.age, p.overall_rating,
                              t.team_name, t.emoji_id
                       FROM free_agency_bids b
                       JOIN players p ON b.player_id = p.player_id
                       JOIN teams t ON p.team_id = t.team_id
                       WHERE b.period_id = ? AND b.team_id = ? AND b.status = 'active'
                       ORDER BY b.bid_amount DESC, p.name""",
                    (period_id, user_team_id)
                )
                bids = await cursor.fetchall()

                # Calculate remaining points
                total_spent = sum(bid[2] for bid in bids)
                remaining_points = max_points - total_spent

                # Create view
                view = AuctionsMenuView(self.bot, period_id, user_team_id, user_team_name, bids, remaining_points, max_points, current_season, period_status)
                embed = view.create_embed()
                await interaction.followup.send(embed=embed, view=view)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}")

    async def period_action_autocomplete(self, interaction: discord.Interaction, current: str):
        """Dynamic autocomplete for free agency period actions based on current status"""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get current season
                cursor = await db.execute(
                    """SELECT season_number FROM seasons
                       ORDER BY
                           CASE status
                               WHEN 'active' THEN 1
                               WHEN 'offseason' THEN 2
                               ELSE 3
                           END,
                           season_number DESC
                       LIMIT 1"""
                )
                season_result = await cursor.fetchone()
                if not season_result:
                    return [app_commands.Choice(name="Check Status", value="check_status")]

                current_season = season_result[0]

                # Check if there's an existing period
                cursor = await db.execute(
                    "SELECT status FROM free_agency_periods WHERE season_number = ?",
                    (current_season,)
                )
                period = await cursor.fetchone()

                choices = [app_commands.Choice(name="Check Status", value="check_status")]

                if not period:
                    # No period - allow starting resign or bidding
                    choices.append(app_commands.Choice(name="Start Free Re-Sign Period", value="start_resign"))
                    choices.append(app_commands.Choice(name="Start Bidding Period", value="start_bidding"))
                else:
                    status = period[0]
                    if status == "resign":
                        choices.append(app_commands.Choice(name="Start Bidding Period", value="start_bidding"))
                    elif status == "bidding":
                        choices.append(app_commands.Choice(name="Start Matching Period", value="start_matching"))
                    elif status == "matching":
                        choices.append(app_commands.Choice(name="End Matching Period", value="end_matching"))

                return choices
        except:
            return [app_commands.Choice(name="Check Status", value="check_status")]

    @app_commands.command(name="freeagencyperiod", description="[ADMIN] Control free agency periods")
    @app_commands.describe(action="The action to perform")
    @app_commands.autocomplete(action=period_action_autocomplete)
    async def free_agency_period(self, interaction: discord.Interaction, action: str):
        # Check if user has admin role
        if not any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles):
            await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        if action == "check_status":
            await self.check_period_status(interaction)
        elif action == "start_resign":
            await self.start_resign_period(interaction)
        elif action == "start_bidding":
            await self.start_bidding_period(interaction)
        elif action == "start_matching":
            await self.start_matching_period(interaction)
        elif action == "end_matching":
            await self.end_matching_period(interaction)

    async def check_period_status(self, interaction: discord.Interaction):
        """Check the current free agency period status"""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get current season
                cursor = await db.execute(
                    """SELECT season_number FROM seasons
                       ORDER BY
                           CASE status
                               WHEN 'active' THEN 1
                               WHEN 'offseason' THEN 2
                               ELSE 3
                           END,
                           season_number DESC
                       LIMIT 1"""
                )
                season_result = await cursor.fetchone()
                if not season_result:
                    await interaction.followup.send("❌ No active season found!")
                    return
                current_season = season_result[0]

                # Check if there's an existing period
                cursor = await db.execute(
                    "SELECT period_id, status FROM free_agency_periods WHERE season_number = ?",
                    (current_season,)
                )
                period = await cursor.fetchone()

                if not period:
                    await interaction.followup.send(
                        f"📊 **Free Agency Status - Season {current_season}**\n\n"
                        f"**Status:** No active free agency period\n\n"
                        f"Use `/freeagencyperiod` to start a free re-sign or bidding period."
                    )
                    return

                period_id, status = period

                # Build status message based on period status
                if status == "resign":
                    # Get teams that haven't confirmed
                    cursor = await db.execute(
                        """SELECT DISTINCT t.team_name
                           FROM teams t
                           JOIN players p ON t.team_id = p.team_id
                           WHERE p.contract_expiry = ?
                           AND t.team_id NOT IN (
                               SELECT DISTINCT team_id
                               FROM free_agency_resigns
                               WHERE period_id = ? AND confirmed = 1
                           )
                           AND (
                               SELECT COUNT(*)
                               FROM players p2
                               WHERE p2.team_id = t.team_id
                               AND p2.contract_expiry = ?
                           ) > 0""",
                        (current_season, period_id, current_season)
                    )
                    unconfirmed_teams_raw = await cursor.fetchall()

                    # Filter to only teams with allowance > 0
                    unconfirmed_teams = []
                    for (team_name,) in unconfirmed_teams_raw:
                        cursor = await db.execute("SELECT team_id FROM teams WHERE team_name = ?", (team_name,))
                        team_result = await cursor.fetchone()
                        if team_result:
                            team_id = team_result[0]
                            allowance = await self.calculate_free_resign_allowance(db, team_id, current_season)
                            if allowance > 0:
                                unconfirmed_teams.append(team_name)

                    if unconfirmed_teams:
                        teams_list = "\n• ".join(unconfirmed_teams)
                        await interaction.followup.send(
                            f"📊 **Free Agency Status - Season {current_season}**\n\n"
                            f"**Status:** Free Re-Sign Period (Active)\n\n"
                            f"**Teams awaiting confirmation ({len(unconfirmed_teams)}):**\n• {teams_list}\n\n"
                            f"Once all teams confirm, you can start the bidding period."
                        )
                    else:
                        await interaction.followup.send(
                            f"📊 **Free Agency Status - Season {current_season}**\n\n"
                            f"**Status:** Free Re-Sign Period (Active)\n\n"
                            f"✅ All eligible teams have confirmed their free re-signs!\n\n"
                            f"You can now start the bidding period."
                        )

                elif status == "bidding":
                    # Get total bids
                    cursor = await db.execute(
                        "SELECT COUNT(*) FROM free_agency_bids WHERE period_id = ?",
                        (period_id,)
                    )
                    bid_count = (await cursor.fetchone())[0]

                    await interaction.followup.send(
                        f"📊 **Free Agency Status - Season {current_season}**\n\n"
                        f"**Status:** Bidding Period (Active)\n\n"
                        f"**Total Bids:** {bid_count}\n\n"
                        f"Teams can use `/placebid` to bid on opposition free agents.\n"
                        f"When ready, start the matching period."
                    )

                elif status == "matching":
                    # Get teams that haven't confirmed
                    cursor = await db.execute(
                        """SELECT DISTINCT t.team_name
                           FROM free_agency_results r
                           JOIN teams t ON r.original_team_id = t.team_id
                           WHERE r.period_id = ? AND r.winning_team_id IS NOT NULL
                           AND r.confirmed_at IS NULL""",
                        (period_id,)
                    )
                    unconfirmed_teams = [row[0] for row in await cursor.fetchall()]

                    if unconfirmed_teams:
                        teams_list = "\n• ".join(unconfirmed_teams)
                        await interaction.followup.send(
                            f"📊 **Free Agency Status - Season {current_season}**\n\n"
                            f"**Status:** Matching Period (Active)\n\n"
                            f"**Teams awaiting confirmation ({len(unconfirmed_teams)}):**\n• {teams_list}\n\n"
                            f"Once all teams confirm, you can end the matching period."
                        )
                    else:
                        await interaction.followup.send(
                            f"📊 **Free Agency Status - Season {current_season}**\n\n"
                            f"**Status:** Matching Period (Active)\n\n"
                            f"✅ All teams have confirmed their matching decisions!\n\n"
                            f"You can now end the matching period to finalize player movements."
                        )

                elif status == "completed":
                    await interaction.followup.send(
                        f"📊 **Free Agency Status - Season {current_season}**\n\n"
                        f"**Status:** Completed\n\n"
                        f"Free agency for this season has been completed."
                    )

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}")

    async def start_resign_period(self, interaction: discord.Interaction):
        """Start the free re-sign period for free agency"""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get current season (active or offseason)
                cursor = await db.execute(
                    """SELECT season_number FROM seasons
                       ORDER BY
                           CASE status
                               WHEN 'active' THEN 1
                               WHEN 'offseason' THEN 2
                               ELSE 3
                           END,
                           season_number DESC
                       LIMIT 1"""
                )
                season_result = await cursor.fetchone()
                if not season_result:
                    await interaction.followup.send("❌ No active season found!")
                    return
                current_season = season_result[0]

                # Check if period already exists
                cursor = await db.execute(
                    "SELECT period_id, status FROM free_agency_periods WHERE season_number = ?",
                    (current_season,)
                )
                existing = await cursor.fetchone()
                if existing:
                    await interaction.followup.send(f"❌ Free agency period already exists for Season {current_season} (status: {existing[1]})")
                    return

                # Get free agents (only those with a team)
                cursor = await db.execute(
                    """SELECT COUNT(*) FROM players
                       WHERE contract_expiry = ? AND team_id IS NOT NULL""",
                    (current_season,)
                )
                fa_count = (await cursor.fetchone())[0]

                if fa_count == 0:
                    await interaction.followup.send(f"❌ No free agents found for Season {current_season}!")
                    return

                # Create period with 'resign' status
                cursor = await db.execute(
                    """INSERT INTO free_agency_periods (season_number, status, auction_points, resign_started_at)
                       VALUES (?, 'resign', 300, CURRENT_TIMESTAMP)""",
                    (current_season,)
                )
                period_id = cursor.lastrowid
                await db.commit()

                # Get all teams with free agents and calculate their allowances
                cursor = await db.execute(
                    """SELECT DISTINCT t.team_id, t.team_name, t.channel_id
                       FROM teams t
                       JOIN players p ON t.team_id = p.team_id
                       WHERE p.contract_expiry = ?""",
                    (current_season,)
                )
                teams_with_fas = await cursor.fetchall()

                # Send notifications to eligible teams
                notifications_sent = 0
                debug_info = []  # For debugging

                for team_id, team_name, channel_id in teams_with_fas:
                    # Calculate how many free re-signs this team gets
                    allowance = await self.calculate_free_resign_allowance(db, team_id, current_season)
                    debug_info.append(f"{team_name}: allowance={allowance}, channel_id={channel_id}")

                    if allowance > 0:
                        if not channel_id:
                            debug_info.append(f"  → Skipped (no channel)")
                            continue

                        # Get the team's free agents
                        cursor = await db.execute(
                            """SELECT p.player_id, p.name, p.position, p.age, p.overall_rating
                               FROM players p
                               WHERE p.team_id = ? AND p.contract_expiry = ?
                               ORDER BY p.overall_rating DESC, p.name""",
                            (team_id, current_season)
                        )
                        free_agents = await cursor.fetchall()

                        # Build embed
                        embed = discord.Embed(
                            title="🔄 Free Re-Sign Period Started!",
                            description=f"You have **{allowance}** free re-sign{'s' if allowance != 1 else ''} available.",
                            color=discord.Color.blue()
                        )

                        # Add free agents list
                        fa_list = []
                        for player_id, name, pos, age, ovr in free_agents:
                            band = await self.get_compensation_band(db, age, ovr)
                            band_text = f"Band {band}" if band else "No comp"
                            fa_list.append(f"**{name}** ({pos}, {age}, {ovr}) - {band_text}")

                        embed.add_field(
                            name=f"Your Free Agents ({len(free_agents)})",
                            value="\n".join(fa_list) if fa_list else "None",
                            inline=False
                        )

                        embed.set_footer(text="Use the button below to select which players to re-sign for free.")

                        # Create view with button to open selection UI
                        view = FreeResignButtonView(self.bot, period_id, team_id, allowance)

                        try:
                            channel = self.bot.get_channel(int(channel_id))
                            if channel:
                                await channel.send(embed=embed, view=view)
                                notifications_sent += 1
                                debug_info.append(f"  → Sent notification")
                            else:
                                debug_info.append(f"  → Channel not found (ID: {channel_id})")
                        except Exception as e:
                            debug_info.append(f"  → Error: {e}")
                            print(f"Error sending notification to {team_name}: {e}")

                # Build response with debug info
                response = (
                    f"✅ **Free Re-Sign Period Started!**\n\n"
                    f"Season: {current_season}\n"
                    f"Free Agents: {fa_count}\n"
                    f"Notifications sent: {notifications_sent} teams with free re-sign allowances\n\n"
                    f"**Debug Info:**\n" + "\n".join(debug_info[:20]) + "\n\n"  # Limit to first 20 teams
                    f"Once all teams have confirmed their free re-signs, you can start the bidding period."
                )

                await interaction.followup.send(response)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}")

    async def start_bidding_period(self, interaction: discord.Interaction):
        """Start the bidding period for free agency"""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get current season (active or offseason)
                cursor = await db.execute(
                    """SELECT season_number FROM seasons
                       ORDER BY
                           CASE status
                               WHEN 'active' THEN 1
                               WHEN 'offseason' THEN 2
                               ELSE 3
                           END,
                           season_number DESC
                       LIMIT 1"""
                )
                season_result = await cursor.fetchone()
                if not season_result:
                    await interaction.followup.send("❌ No active season found!")
                    return
                current_season = season_result[0]

                # Check if period already exists
                cursor = await db.execute(
                    "SELECT period_id, status FROM free_agency_periods WHERE season_number = ?",
                    (current_season,)
                )
                existing = await cursor.fetchone()

                if existing:
                    period_id, status = existing

                    # If period is in 'resign' status, transition to 'bidding'
                    if status == 'resign':
                        # Check if all eligible teams have confirmed their free re-signs
                        cursor = await db.execute(
                            """SELECT DISTINCT t.team_id, t.team_name
                               FROM teams t
                               JOIN players p ON t.team_id = p.team_id
                               WHERE p.contract_expiry = ?""",
                            (current_season,)
                        )
                        teams_with_fas = await cursor.fetchall()

                        pending_teams = []
                        for team_id, team_name in teams_with_fas:
                            # Calculate allowance
                            allowance = await self.calculate_free_resign_allowance(db, team_id, current_season)

                            if allowance > 0:
                                # Check if they've confirmed
                                cursor = await db.execute(
                                    """SELECT COUNT(*) FROM free_agency_resigns
                                       WHERE period_id = ? AND team_id = ? AND confirmed = 1""",
                                    (period_id, team_id)
                                )
                                confirmed_count = (await cursor.fetchone())[0]

                                if confirmed_count == 0:
                                    pending_teams.append(team_name)

                        if pending_teams:
                            await interaction.followup.send(
                                f"❌ Cannot start bidding period yet!\n\n"
                                f"**Teams that haven't confirmed free re-signs:**\n" +
                                "\n".join(f"• {team}" for team in pending_teams)
                            )
                            return

                        # All teams confirmed - transition to bidding
                        # First, process the free re-signs
                        await self.process_free_resigns(db, period_id, current_season)

                        # Log free re-sign results
                        await self.log_free_resign_results(db, period_id, current_season)

                        # Update period status to bidding
                        await db.execute(
                            """UPDATE free_agency_periods
                               SET status = 'bidding', bidding_started_at = CURRENT_TIMESTAMP
                               WHERE period_id = ?""",
                            (period_id,)
                        )
                        await db.commit()

                        await interaction.followup.send(
                            f"✅ **Free Agency Bidding Period Started!**\n\n"
                            f"Season: {current_season}\n"
                            f"Free re-signs processed successfully!\n\n"
                            f"Teams can now use `/placebid` to bid on opposition free agents."
                        )
                        return
                    else:
                        await interaction.followup.send(f"❌ Free agency period already exists for Season {current_season} (status: {status})")
                        return

                # No existing period - create new one with bidding status
                # (This path is if they skip the resign period)
                # Get free agents (only those with a team)
                cursor = await db.execute(
                    """SELECT COUNT(*) FROM players
                       WHERE contract_expiry = ? AND team_id IS NOT NULL""",
                    (current_season,)
                )
                fa_count = (await cursor.fetchone())[0]

                if fa_count == 0:
                    await interaction.followup.send(f"❌ No free agents found for Season {current_season}!")
                    return

                # Create period
                cursor = await db.execute(
                    """INSERT INTO free_agency_periods (season_number, status, auction_points, bidding_started_at)
                       VALUES (?, 'bidding', 300, CURRENT_TIMESTAMP)""",
                    (current_season,)
                )
                period_id = cursor.lastrowid
                await db.commit()

                await interaction.followup.send(
                    f"✅ **Free Agency Bidding Period Started!**\n\n"
                    f"Season: {current_season}\n"
                    f"Free Agents: {fa_count}\n\n"
                    f"Teams can now use `/placebid` to bid on opposition free agents."
                )

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}")

    async def start_matching_period(self, interaction: discord.Interaction):
        """End bidding, calculate winners, and start matching period"""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get current season (active or offseason)
                cursor = await db.execute(
                    """SELECT season_number FROM seasons
                       ORDER BY
                           CASE status
                               WHEN 'active' THEN 1
                               WHEN 'offseason' THEN 2
                               ELSE 3
                           END,
                           season_number DESC
                       LIMIT 1"""
                )
                season_result = await cursor.fetchone()
                if not season_result:
                    await interaction.followup.send("❌ No active season found!")
                    return
                current_season = season_result[0]

                # Get period
                cursor = await db.execute(
                    "SELECT period_id, status FROM free_agency_periods WHERE season_number = ?",
                    (current_season,)
                )
                period_result = await cursor.fetchone()
                if not period_result:
                    await interaction.followup.send("❌ No free agency period found! Start bidding first.")
                    return

                period_id, status = period_result
                if status != 'bidding':
                    await interaction.followup.send(f"❌ Period is not in bidding status (current: {status})")
                    return

                # Get all free agents (only those with a team)
                cursor = await db.execute(
                    """SELECT player_id, team_id FROM players
                       WHERE contract_expiry = ? AND team_id IS NOT NULL""",
                    (current_season,)
                )
                free_agents = await cursor.fetchall()

                if not free_agents:
                    await interaction.followup.send(f"❌ No valid free agents found (all free agents must have a team)!")
                    return

                # Calculate winning bids for each player
                results_created = 0
                for player_id, original_team_id in free_agents:
                    # Get all bids for this player
                    cursor = await db.execute(
                        """SELECT b.team_id, b.bid_amount, lp.position
                           FROM free_agency_bids b
                           LEFT JOIN ladder_positions lp ON b.team_id = lp.team_id
                           LEFT JOIN seasons s ON lp.season_id = s.season_id AND s.season_number = ?
                           WHERE b.period_id = ? AND b.player_id = ? AND b.status = 'active'
                           ORDER BY b.bid_amount DESC, lp.position ASC""",
                        (current_season, period_id, player_id)
                    )
                    bids = await cursor.fetchall()

                    if bids:
                        # Winner is highest bid, tiebreaker by ladder position (lower is better)
                        winning_team_id, winning_bid, _ = bids[0]

                        # Create result
                        await db.execute(
                            """INSERT INTO free_agency_results
                               (period_id, player_id, original_team_id, winning_team_id, winning_bid, matched)
                               VALUES (?, ?, ?, ?, ?, 0)""",
                            (period_id, player_id, original_team_id, winning_team_id, winning_bid)
                        )
                        results_created += 1

                        # Mark other bids as outbid (they will get points refunded)
                        await db.execute(
                            """UPDATE free_agency_bids
                               SET status = 'outbid'
                               WHERE period_id = ? AND player_id = ? AND team_id != ?""",
                            (period_id, player_id, winning_team_id)
                        )

                        # Mark winning bid
                        await db.execute(
                            """UPDATE free_agency_bids
                               SET status = 'winning'
                               WHERE period_id = ? AND player_id = ? AND team_id = ?""",
                            (period_id, player_id, winning_team_id)
                        )
                    else:
                        # No bids - will be auto re-signed
                        await db.execute(
                            """INSERT INTO free_agency_results
                               (period_id, player_id, original_team_id, winning_team_id, winning_bid, matched)
                               VALUES (?, ?, ?, NULL, NULL, 0)""",
                            (period_id, player_id, original_team_id)
                        )

                # Update period status
                await db.execute(
                    """UPDATE free_agency_periods
                       SET status = 'matching', bidding_ended_at = CURRENT_TIMESTAMP
                       WHERE period_id = ?""",
                    (period_id,)
                )
                await db.commit()

                # Log winning bids
                await self.log_winning_bids(db, period_id, current_season)

                # Send matching interface to teams with winning bids on their players
                cursor = await db.execute(
                    """SELECT DISTINCT t.team_id, t.team_name, t.channel_id
                       FROM free_agency_results r
                       JOIN teams t ON r.original_team_id = t.team_id
                       WHERE r.period_id = ? AND r.winning_team_id IS NOT NULL
                       AND t.channel_id IS NOT NULL""",
                    (period_id,)
                )
                teams_with_losses = await cursor.fetchall()

                matching_messages_sent = 0
                for team_id, team_name, channel_id in teams_with_losses:
                    try:
                        # Get this team's players with bids
                        cursor = await db.execute(
                            """SELECT r.player_id, p.name, p.position, p.age, p.overall_rating,
                                      r.winning_team_id, t.team_name, t.emoji_id, r.winning_bid
                               FROM free_agency_results r
                               JOIN players p ON r.player_id = p.player_id
                               JOIN teams t ON r.winning_team_id = t.team_id
                               WHERE r.period_id = ? AND r.original_team_id = ?""",
                            (period_id, team_id)
                        )
                        player_bids = await cursor.fetchall()

                        if player_bids:
                            channel = self.bot.get_channel(int(channel_id))
                            if channel:
                                view = MatchingView(self.bot, period_id, team_id, team_name, player_bids, current_season)
                                embed = await view.create_embed()
                                await channel.send(embed=embed, view=view)
                                matching_messages_sent += 1
                    except Exception as e:
                        print(f"Error sending matching message to {team_name}: {e}")

                await interaction.followup.send(
                    f"✅ **Matching Period Started!**\n\n"
                    f"Winning bids calculated: {results_created}\n"
                    f"Matching interfaces sent: {matching_messages_sent} teams\n\n"
                    f"Teams can now match bids on their players."
                )

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}")

    async def end_matching_period(self, interaction: discord.Interaction):
        """Process matches, assign players, calculate compensation"""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get current season (active or offseason)
                cursor = await db.execute(
                    """SELECT season_number FROM seasons
                       ORDER BY
                           CASE status
                               WHEN 'active' THEN 1
                               WHEN 'offseason' THEN 2
                               ELSE 3
                           END,
                           season_number DESC
                       LIMIT 1"""
                )
                season_result = await cursor.fetchone()
                if not season_result:
                    await interaction.followup.send("❌ No active season found!")
                    return
                current_season = season_result[0]

                # Get period
                cursor = await db.execute(
                    "SELECT period_id, status FROM free_agency_periods WHERE season_number = ?",
                    (current_season,)
                )
                period_result = await cursor.fetchone()
                if not period_result:
                    await interaction.followup.send("❌ No free agency period found!")
                    return

                period_id, status = period_result
                if status != 'matching':
                    await interaction.followup.send(f"❌ Period is not in matching status (current: {status})")
                    return

                # Check if all teams have confirmed their matches
                # Get teams with winning bids on their players and check if they've confirmed
                cursor = await db.execute(
                    """SELECT DISTINCT t.team_id, t.team_name
                       FROM free_agency_results r
                       JOIN teams t ON r.original_team_id = t.team_id
                       WHERE r.period_id = ? AND r.winning_team_id IS NOT NULL""",
                    (period_id,)
                )
                teams_with_bids = await cursor.fetchall()

                # Check which teams haven't confirmed
                unconfirmed_teams = []
                for team_id, team_name in teams_with_bids:
                    # Check if this team has any unconfirmed results (confirmed_at IS NULL)
                    cursor = await db.execute(
                        """SELECT COUNT(*) FROM free_agency_results
                           WHERE period_id = ? AND original_team_id = ?
                           AND winning_team_id IS NOT NULL AND confirmed_at IS NULL""",
                        (period_id, team_id)
                    )
                    unconfirmed_count = (await cursor.fetchone())[0]

                    if unconfirmed_count > 0:
                        unconfirmed_teams.append(team_name)

                # If there are unconfirmed teams, don't allow ending the matching period
                if unconfirmed_teams:
                    team_list = "\n• ".join(unconfirmed_teams)
                    await interaction.followup.send(
                        f"❌ Cannot end matching period!\n\n"
                        f"The following teams have not confirmed their bid matches:\n• {team_list}\n\n"
                        f"All teams must confirm their matching decisions before the period can end."
                    )
                    return

                # Get all free agency results
                cursor = await db.execute(
                    """SELECT r.result_id, r.player_id, r.original_team_id, r.winning_team_id,
                              r.winning_bid, r.matched, p.name, p.age, p.overall_rating
                       FROM free_agency_results r
                       JOIN players p ON r.player_id = p.player_id
                       WHERE r.period_id = ?""",
                    (period_id,)
                )
                results = await cursor.fetchall()

                players_transferred = 0
                players_matched = 0
                players_resigned = 0
                compensation_picks = 0

                for result_id, player_id, original_team_id, winning_team_id, winning_bid, matched, player_name, age, ovr in results:
                    # Get new contract length based on age
                    contract_years = await self.get_contract_years_for_age(db, age)
                    # current_season is the season that just ended (Offseason 9 means Season 9 just ended)
                    # Adding contract_years gives us the last season they'll play under the new contract
                    new_contract_expiry = current_season + contract_years

                    if winning_team_id is None:
                        # No bids - auto re-sign with original team
                        await db.execute(
                            "UPDATE players SET contract_expiry = ? WHERE player_id = ?",
                            (new_contract_expiry, player_id)
                        )
                        players_resigned += 1

                    elif matched:
                        # Original team matched - player stays, winning bidder gets refund
                        await db.execute(
                            "UPDATE players SET contract_expiry = ? WHERE player_id = ?",
                            (new_contract_expiry, player_id)
                        )
                        players_matched += 1

                        # Note: Points are already not deducted for matched bids in the matching logic

                    else:
                        # Unmatched - transfer player to winning team
                        await db.execute(
                            "UPDATE players SET team_id = ?, contract_expiry = ? WHERE player_id = ?",
                            (winning_team_id, new_contract_expiry, player_id)
                        )
                        players_transferred += 1

                        # Calculate compensation for original team
                        compensation_band = await self.get_compensation_band(db, age, ovr)
                        if compensation_band:
                            # Store compensation band for later pick insertion
                            await db.execute(
                                "UPDATE free_agency_results SET compensation_band = ? WHERE result_id = ?",
                                (compensation_band, result_id)
                            )
                            compensation_picks += 1

                # Update period status
                await db.execute(
                    """UPDATE free_agency_periods
                       SET status = 'completed', matching_ended_at = CURRENT_TIMESTAMP
                       WHERE period_id = ?""",
                    (period_id,)
                )

                # Clear all bids for this period now that it's completed
                # This "refunds" all auction points for the next season
                await db.execute(
                    "DELETE FROM free_agency_bids WHERE period_id = ?",
                    (period_id,)
                )

                # Insert compensation picks into the current draft automatically
                picks_inserted = 0
                draft_name = None
                if compensation_picks > 0:
                    # Find the current draft
                    cursor = await db.execute(
                        "SELECT draft_id, draft_name, season_number FROM drafts WHERE status = 'current'"
                    )
                    draft = await cursor.fetchone()

                    if draft:
                        draft_id, draft_name, draft_season = draft

                        # Get all compensation results for this period
                        cursor = await db.execute(
                            """SELECT result_id, original_team_id, compensation_band, player_id
                               FROM free_agency_results
                               WHERE period_id = ? AND compensation_band IS NOT NULL
                               ORDER BY compensation_band, original_team_id""",
                            (period_id,)
                        )
                        comp_results = await cursor.fetchall()

                        # Process compensation picks in order by band (lower bands/rounds first)
                        # Each insertion renumbers all subsequent picks globally
                        for result_id, team_id, comp_band, player_id in comp_results:
                            # Get player name for pick origin description
                            cursor = await db.execute("SELECT name FROM players WHERE player_id = ?", (player_id,))
                            player_name = (await cursor.fetchone())[0]

                            # Determine round and insertion logic based on compensation band
                            if comp_band in [1, 3, 5]:
                                # After team's natural pick in the round
                                round_num = {1: 1, 3: 2, 5: 3}[comp_band]

                                # Get team name to identify their natural pick by origin
                                cursor = await db.execute("SELECT team_name FROM teams WHERE team_id = ?", (team_id,))
                                team_name_result = await cursor.fetchone()
                                if not team_name_result:
                                    continue  # Skip if team not found

                                team_name = team_name_result[0]
                                natural_pick_origin = f"{team_name} R{round_num}"

                                # Find the team's natural pick in this round by origin (unchanging identifier)
                                cursor = await db.execute(
                                    """SELECT pick_number FROM draft_picks
                                       WHERE draft_id = ? AND round_number = ? AND pick_origin = ?
                                       ORDER BY pick_number LIMIT 1""",
                                    (draft_id, round_num, natural_pick_origin)
                                )
                                natural_pick = await cursor.fetchone()

                                if natural_pick:
                                    natural_pick_num = natural_pick[0]
                                    new_pick_num = natural_pick_num + 1

                                    # Shift all picks after this position up by 1
                                    await db.execute(
                                        """UPDATE draft_picks
                                           SET pick_number = pick_number + 1
                                           WHERE draft_id = ? AND pick_number >= ?""",
                                        (draft_id, new_pick_num)
                                    )
                                else:
                                    # Fallback: append to end of round if natural pick not found
                                    cursor = await db.execute(
                                        """SELECT COALESCE(MAX(pick_number), 0) FROM draft_picks
                                           WHERE draft_id = ? AND round_number = ?""",
                                        (draft_id, round_num)
                                    )
                                    new_pick_num = (await cursor.fetchone())[0] + 1

                            elif comp_band in [2, 4]:
                                # End of round, reverse ladder order
                                round_num = {2: 1, 4: 2}[comp_band]

                                # Find the last pick in this round
                                cursor = await db.execute(
                                    """SELECT COALESCE(MAX(pick_number), 0) FROM draft_picks
                                       WHERE draft_id = ? AND round_number = ?""",
                                    (draft_id, round_num)
                                )
                                last_pick_in_round = (await cursor.fetchone())[0]
                                new_pick_num = last_pick_in_round + 1

                                # Shift all picks in subsequent rounds up by 1
                                await db.execute(
                                    """UPDATE draft_picks
                                       SET pick_number = pick_number + 1
                                       WHERE draft_id = ? AND pick_number >= ?""",
                                    (draft_id, new_pick_num)
                                )

                            else:
                                # Unknown band - skip
                                continue

                            # Insert the compensation pick with proper round and pick number
                            cursor = await db.execute(
                                """INSERT INTO draft_picks (draft_id, draft_name, season_number, round_number, pick_number, pick_origin, original_team_id, current_team_id)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                                (draft_id, draft_name, draft_season, round_num, new_pick_num,
                                 f"Compensation Band {comp_band} (lost {player_name})", team_id, team_id)
                            )
                            pick_id = cursor.lastrowid

                            # Update free_agency_results with the pick_id reference
                            await db.execute(
                                "UPDATE free_agency_results SET compensation_pick_id = ? WHERE result_id = ?",
                                (pick_id, result_id)
                            )
                            picks_inserted += 1

                await db.commit()

                # Log final movements and compensation
                await self.log_final_movements(db, period_id, current_season)

                # Build summary message with compensation pick details if any were awarded
                message = (
                    f"✅ **Free Agency Period Completed!**\n\n"
                    f"**Summary:**\n"
                    f"• {players_transferred} player{'s' if players_transferred != 1 else ''} transferred to new teams\n"
                    f"• {players_matched} player{'s' if players_matched != 1 else ''} matched by original teams\n"
                    f"• {players_resigned} player{'s' if players_resigned != 1 else ''} auto re-signed (no bids)\n"
                    f"• {compensation_picks} compensation pick{'s' if compensation_picks != 1 else ''} awarded"
                )

                # Add compensation pick details if any were awarded
                if compensation_picks > 0:
                    cursor = await db.execute(
                        """SELECT t.team_name, r.compensation_band, p.name
                           FROM free_agency_results r
                           JOIN teams t ON r.original_team_id = t.team_id
                           JOIN players p ON r.player_id = p.player_id
                           WHERE r.period_id = ? AND r.compensation_band IS NOT NULL
                           ORDER BY r.compensation_band, t.team_name""",
                        (period_id,)
                    )
                    comp_picks = await cursor.fetchall()

                    if draft_name:
                        message += f"\n\n**Compensation Picks (inserted into {draft_name}):**"
                    else:
                        message += "\n\n**Compensation Picks (no current draft found - picks not inserted):**"

                    for team_name, band, player_name in comp_picks:
                        message += f"\n• **{team_name}**: Band {band} pick (lost {player_name})"

                await interaction.followup.send(message)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}")

    @app_commands.command(name="debugcompensation", description="[ADMIN] Debug compensation chart ranges for a specific age/OVR")
    @app_commands.describe(age="Player age", ovr="Player OVR")
    async def debug_compensation(self, interaction: discord.Interaction, age: int, ovr: int):
        """Show all compensation ranges that match a specific age/OVR"""
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ This command requires administrator permissions.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get all ranges that match this age/OVR
                cursor = await db.execute(
                    """SELECT min_age, max_age, min_ovr, max_ovr, compensation_band
                       FROM compensation_chart
                       WHERE min_age <= ? AND COALESCE(max_age, min_age) >= ?
                       AND min_ovr <= ? AND COALESCE(max_ovr, min_ovr) >= ?
                       ORDER BY compensation_band ASC""",
                    (age, age, ovr, ovr)
                )
                matches = await cursor.fetchall()

                if not matches:
                    await interaction.followup.send(f"❌ No compensation ranges found for age {age}, OVR {ovr}")
                    return

                response = f"**Compensation Ranges for Age {age}, OVR {ovr}:**\n\n"
                for min_age, max_age, min_ovr, max_ovr, band in matches:
                    age_range = f"{min_age}" if max_age is None else f"{min_age}-{max_age}"
                    ovr_range = f"{min_ovr}" if max_ovr is None else f"{min_ovr}-{max_ovr}"
                    response += f"• **Band {band}**: Age {age_range}, OVR {ovr_range}\n"

                response += f"\n**Result:** Band {matches[0][4]} (best match)"
                await interaction.followup.send(response)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}")

    @app_commands.command(name="compensationtable", description="View the compensation chart for free agency")
    async def compensation_table(self, interaction: discord.Interaction):
        """Display the compensation chart as a visual table"""
        await interaction.response.defer(ephemeral=True)

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get compensation chart data
                cursor = await db.execute(
                    """SELECT min_age, max_age, min_ovr, max_ovr, compensation_band
                       FROM compensation_chart
                       ORDER BY compensation_band, min_age, min_ovr"""
                )
                compensation_data = await cursor.fetchall()

                if not compensation_data:
                    await interaction.followup.send("❌ No compensation chart data found! Use `/migratedb` to initialize.")
                    return

                # Build map of (age, ovr) -> band by expanding ranges
                comp_map = {}  # (age, ovr) -> band
                for min_age, max_age, min_ovr, max_ovr, band in compensation_data:
                    # Expand age range (if max_age is None, it's a single age)
                    age_end = max_age if max_age is not None else min_age
                    # Expand OVR range (if max_ovr is None, it's a single OVR)
                    ovr_end = max_ovr if max_ovr is not None else min_ovr

                    for age in range(min_age, age_end + 1):
                        for ovr in range(min_ovr, ovr_end + 1):
                            comp_map[(age, ovr)] = band

                # Build compact table - use 2-char columns to save space
                response_parts = []
                response_parts.append("**Compensation Chart** (lower = better)\n")

                # Split into OVR ranges: 70-79, 80-89, 90-99
                ovr_ranges = [
                    (70, 79, "70-79"),
                    (80, 89, "80-89"),
                    (90, 99, "90-99")
                ]

                for ovr_start, ovr_end, range_label in ovr_ranges:
                    table_lines = []

                    # Compact header - 2 chars per column, add space to align with data rows
                    header = " A│" + " ".join([f"{ovr:2}" for ovr in range(ovr_start, ovr_end + 1)])
                    table_lines.append(header)
                    table_lines.append("─" * len(header))

                    # Data rows - ages 19-33
                    for age in range(19, 34):
                        row_values = [f"{age:2}"]  # Keep both digits
                        for ovr in range(ovr_start, ovr_end + 1):
                            band = comp_map.get((age, ovr), None)
                            if band is not None:
                                row_values.append(f"{band:2}")
                            else:
                                row_values.append(" -")
                        table_lines.append("│".join(row_values))

                    response_parts.append(f"**{range_label}**```\n{chr(10).join(table_lines)}```")

                await interaction.followup.send("\n".join(response_parts))

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}")


class FreeAgentsView(discord.ui.View):
    """Paginated view for free agents list"""
    def __init__(self, bot, teams_dict, season_number, total_fa_count):
        super().__init__(timeout=180)
        self.bot = bot
        self.teams_dict = teams_dict
        self.season_number = season_number
        self.total_fa_count = total_fa_count
        self.teams_list = sorted(teams_dict.keys())
        self.current_page = 0
        self.teams_per_page = 5

        self.update_buttons()

    def update_buttons(self):
        """Update navigation buttons"""
        self.clear_items()

        total_pages = (len(self.teams_list) + self.teams_per_page - 1) // self.teams_per_page

        # Previous button
        prev_button = discord.ui.Button(
            label="◀ Previous",
            style=discord.ButtonStyle.secondary,
            disabled=self.current_page == 0,
            custom_id="prev"
        )
        prev_button.callback = self.prev_callback
        self.add_item(prev_button)

        # Page indicator
        page_button = discord.ui.Button(
            label=f"Page {self.current_page + 1}/{total_pages}",
            style=discord.ButtonStyle.secondary,
            disabled=True,
            custom_id="page"
        )
        self.add_item(page_button)

        # Next button
        next_button = discord.ui.Button(
            label="Next ▶",
            style=discord.ButtonStyle.secondary,
            disabled=self.current_page >= total_pages - 1,
            custom_id="next"
        )
        next_button.callback = self.next_callback
        self.add_item(next_button)

    def create_embed(self):
        """Create embed for current page"""
        embed = discord.Embed(
            title=f"Free Agents - Season {self.season_number}",
            color=discord.Color.blue()
        )

        # Get teams for current page
        start_idx = self.current_page * self.teams_per_page
        end_idx = start_idx + self.teams_per_page
        page_teams = self.teams_list[start_idx:end_idx]

        # Build player list with team emojis
        all_players = []
        for team_name in page_teams:
            team_data = self.teams_dict[team_name]
            players = team_data['players']

            # Get emoji
            emoji_str = ""
            if team_data['emoji_id']:
                try:
                    emoji = self.bot.get_emoji(int(team_data['emoji_id']))
                    if emoji:
                        emoji_str = f"{emoji} "
                except:
                    pass

            # Add all players from this team with team emoji and RFA status
            for name, pos, age, ovr in players:
                # Check if RFA (age <= 25)
                rfa_label = " [RFA]" if age <= 25 else ""
                all_players.append(f"{emoji_str}**{name}**{rfa_label} ({pos}, {age}, {ovr})")

        # Add as single field
        if all_players:
            embed.description = "\n".join(all_players)
        else:
            embed.description = "*No free agents on this page*"

        embed.set_footer(text=f"Total: {self.total_fa_count} free agents")
        return embed

    async def prev_callback(self, interaction: discord.Interaction):
        """Go to previous page"""
        self.current_page -= 1
        self.update_buttons()
        embed = self.create_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def next_callback(self, interaction: discord.Interaction):
        """Go to next page"""
        self.current_page += 1
        self.update_buttons()
        embed = self.create_embed()
        await interaction.response.edit_message(embed=embed, view=self)


class MatchingView(discord.ui.View):
    """Interactive UI for teams to match winning bids on their players"""
    def __init__(self, bot, period_id, team_id, team_name, player_bids, season_number):
        super().__init__(timeout=None)  # No timeout for matching
        self.bot = bot
        self.period_id = period_id
        self.team_id = team_id
        self.team_name = team_name
        self.player_bids = player_bids  # List of (player_id, name, pos, age, ovr, winning_team_id, team_name, emoji_id, bid)
        self.season_number = season_number
        self.matches = {}  # player_id -> bool (True = match, False = don't match)
        self.confirmed = False  # Track if matches have been confirmed

        # Calculate max points (300)
        self.max_points = 300

        # Add toggle buttons for each player
        for player_id, name, pos, age, ovr, winning_team_id, bidding_team, emoji_id, bid in player_bids:
            self.matches[player_id] = False  # Default to not matching

        self.update_buttons()

    def update_buttons(self):
        """Update all buttons based on current state"""
        self.clear_items()

        # Add dropdown to select players to match (limit to 25)
        if self.player_bids:
            options = []
            for player_id, name, pos, age, ovr, _, bidding_team, _, bid in self.player_bids[:25]:
                # Check if RFA and calculate cost
                is_rfa = age <= 25
                if is_rfa:
                    match_cost = round(bid * 0.8)
                    cost_label = f"{match_cost}pts (RFA discount)"
                else:
                    match_cost = bid
                    cost_label = f"{bid}pts"

                options.append(
                    discord.SelectOption(
                        label=f"{name} ({pos}, {age}, {ovr})",
                        description=f"Bid: {cost_label}",
                        value=str(player_id),
                        default=self.matches.get(player_id, False)
                    )
                )

            select = discord.ui.Select(
                placeholder="Select players to MATCH (unselected = let go)",
                options=options,
                min_values=0,
                max_values=len(options),
                custom_id="player_select",
                row=0
            )
            select.callback = self.select_callback
            self.add_item(select)

        # Add confirm button
        confirm_button = discord.ui.Button(
            label="Confirm Matches",
            style=discord.ButtonStyle.primary,
            custom_id="confirm",
            row=1
        )
        confirm_button.callback = self.confirm_callback
        self.add_item(confirm_button)

    async def create_embed(self):
        """Create the embed showing current matching status"""
        embed = discord.Embed(
            title=f"🤝 Free Agency Matching - {self.team_name}",
            description="Your free agents have received bids. Choose which to match:",
            color=discord.Color.orange()
        )

        # Calculate points needed if all current matches go through (with RFA discount)
        total_cost = 0
        for player_id, _, _, age, _, _, _, _, bid in self.player_bids:
            if self.matches.get(player_id, False):
                # Apply 20% discount for RFAs (age <= 25)
                if age <= 25:
                    match_cost = round(bid * 0.8)  # 20% discount
                else:
                    match_cost = bid
                total_cost += match_cost

        remaining = self.max_points - total_cost

        embed.add_field(
            name="Auction Points",
            value=f"**Cost if matched:** {total_cost} pts\n**Remaining:** {remaining} pts",
            inline=False
        )

        # Get compensation bands for all players
        async with aiosqlite.connect(DB_PATH) as db:
            compensation_bands = {}
            for player_id, name, pos, age, ovr, winning_team_id, bidding_team, emoji_id, bid in self.player_bids:
                cursor = await db.execute(
                    """SELECT compensation_band FROM compensation_chart
                       WHERE min_age <= ? AND COALESCE(max_age, min_age) >= ?
                       AND min_ovr <= ? AND COALESCE(max_ovr, min_ovr) >= ?
                       ORDER BY compensation_band ASC
                       LIMIT 1""",
                    (age, age, ovr, ovr)
                )
                result = await cursor.fetchone()
                compensation_bands[player_id] = result[0] if result else None

        # List each player with current match status
        player_lines = []
        for player_id, name, pos, age, ovr, winning_team_id, bidding_team, emoji_id, bid in self.player_bids:
            # Get emoji
            emoji_str = ""
            if emoji_id:
                try:
                    emoji = self.bot.get_emoji(int(emoji_id))
                    if emoji:
                        emoji_str = f"{emoji} "
                except:
                    pass

            is_matched = self.matches.get(player_id, False)
            status = "✅ MATCH" if is_matched else "❌ LET GO"

            # Check if RFA (age <= 25) and calculate match cost
            is_rfa = age <= 25
            if is_rfa:
                match_cost = round(bid * 0.8)  # 20% discount
                rfa_label = " [RFA]"
                cost_display = f"**{match_cost} pts** (20% discount from {bid} pts)"
            else:
                match_cost = bid
                rfa_label = ""
                cost_display = f"**{bid} pts**"

            # Get compensation band label
            comp_band = compensation_bands.get(player_id)
            if comp_band:
                comp_label = f" • If let go: **Band {comp_band}** compensation"
            else:
                comp_label = " • If let go: **No compensation**"

            player_lines.append(
                f"{status} **{name}**{rfa_label} ({pos}, {age}, {ovr})\n"
                f"    └─ {emoji_str}{bidding_team} bid {cost_display}{comp_label}"
            )

        embed.add_field(
            name=f"Players ({len(self.player_bids)})",
            value="\n\n".join(player_lines),
            inline=False
        )

        embed.set_footer(text="Use the dropdown to select players to match, then click Confirm")
        return embed

    async def select_callback(self, interaction: discord.Interaction):
        """Handle player selection from dropdown"""
        # Check if period is still active
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT status FROM free_agency_periods WHERE period_id = ?",
                (self.period_id,)
            )
            period_status = await cursor.fetchone()
            if not period_status or period_status[0] != 'matching':
                await interaction.response.send_message(
                    "❌ The matching period has ended! Matches are no longer editable.",
                    ephemeral=True
                )
                return

        # Get selected player IDs
        selected_ids = {int(value) for value in interaction.data['values']}

        # Update matches dict - selected = match, not selected = let go
        for player_id, _, _, _, _, _, _, _, _ in self.player_bids:
            self.matches[player_id] = player_id in selected_ids

        # Update the view
        self.update_buttons()
        embed = await self.create_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def confirm_callback(self, interaction: discord.Interaction):
        """Confirm the matching decisions"""
        try:
            # Check if period is still active
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "SELECT status FROM free_agency_periods WHERE period_id = ?",
                    (self.period_id,)
                )
                period_status = await cursor.fetchone()
                if not period_status or period_status[0] != 'matching':
                    await interaction.response.send_message(
                        "❌ The matching period has ended! Matches are no longer editable.",
                        ephemeral=True
                    )
                    return

            # Calculate total cost (with RFA discount)
            total_cost = 0
            for player_id, _, _, age, _, _, _, _, bid in self.player_bids:
                if self.matches.get(player_id, False):
                    # Apply 20% discount for RFAs (age <= 25)
                    if age <= 25:
                        match_cost = round(bid * 0.8)
                    else:
                        match_cost = bid
                    total_cost += match_cost

            if total_cost > self.max_points:
                await interaction.response.send_message(
                    f"❌ Insufficient points! You need {total_cost} pts but only have {self.max_points} pts available.",
                    ephemeral=True
                )
                return

            # Update database with matches
            async with aiosqlite.connect(DB_PATH) as db:
                for player_id in self.matches:
                    # Update ALL players - set matched = 1 if True, matched = 0 if False
                    # Also set confirmed_at timestamp to track that this team has confirmed
                    await db.execute(
                        """UPDATE free_agency_results
                           SET matched = ?, confirmed_at = CURRENT_TIMESTAMP
                           WHERE period_id = ? AND player_id = ?""",
                        (1 if self.matches[player_id] else 0, self.period_id, player_id)
                    )
                await db.commit()

                # Log to bot logs channel
                log_channel = await self.bot.get_cog('FreeAgencyCommands').get_bot_logs_channel(db)
                if log_channel:
                    # Get team info
                    cursor = await db.execute("SELECT team_name, emoji_id FROM teams WHERE team_id = ?", (self.team_id,))
                    team_data = await cursor.fetchone()
                    team_name = team_data[0] if team_data else "Unknown Team"
                    emoji_id = team_data[1] if team_data and team_data[1] else None

                    emoji_str = ""
                    if emoji_id:
                        try:
                            emoji = self.bot.get_emoji(int(emoji_id))
                            if emoji:
                                emoji_str = f"{emoji} "
                        except:
                            pass

                    matched_count = sum(1 for m in self.matches.values() if m)
                    let_go_count = len(self.matches) - matched_count

                    await log_channel.send(
                        f"✅ {emoji_str}**{team_name}** confirmed matches: "
                        f"{matched_count} matched ({total_cost}pts), {let_go_count} let go"
                    )

            # Mark as confirmed
            self.confirmed = True

            # Replace buttons with just "Edit Matches" button
            self.clear_items()
            edit_button = discord.ui.Button(
                label="Edit Matches",
                style=discord.ButtonStyle.secondary,
                custom_id="edit_matches",
                row=0
            )
            edit_button.callback = self.edit_matches_callback
            self.add_item(edit_button)

            embed = discord.Embed(
                title=f"✅ Matches Confirmed - {self.team_name}",
                description="Your matching decisions have been recorded.",
                color=discord.Color.green()
            )

            matched_count = sum(1 for m in self.matches.values() if m)
            let_go_count = len(self.matches) - matched_count

            embed.add_field(
                name="Summary",
                value=f"**Matched:** {matched_count} player{'s' if matched_count != 1 else ''} ({total_cost} pts)\n"
                      f"**Let Go:** {let_go_count} player{'s' if let_go_count != 1 else ''}",
                inline=False
            )
            embed.set_footer(text="Click 'Edit Matches' to make changes • Waiting for admin to end matching period...")

            await interaction.response.edit_message(embed=embed, view=self)

        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)

    async def edit_matches_callback(self, interaction: discord.Interaction):
        """Allow editing matches after confirmation"""
        # Check if period is still active
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT status FROM free_agency_periods WHERE period_id = ?",
                (self.period_id,)
            )
            period_status = await cursor.fetchone()
            if not period_status or period_status[0] != 'matching':
                await interaction.response.send_message(
                    "❌ The matching period has ended! Matches are no longer editable.",
                    ephemeral=True
                )
                return

        self.confirmed = False
        self.update_buttons()
        embed = await self.create_embed()
        await interaction.response.edit_message(embed=embed, view=self)


class AuctionsMenuView(discord.ui.View):
    def __init__(self, bot, period_id, team_id, team_name, bids, remaining_points, max_points, season_number, period_status):
        super().__init__(timeout=180)
        self.bot = bot
        self.period_id = period_id
        self.team_id = team_id
        self.team_name = team_name
        self.bids = bids
        self.remaining_points = remaining_points
        self.max_points = max_points
        self.season_number = season_number
        self.period_status = period_status

        self.update_buttons()

    def update_buttons(self):
        """Update all buttons based on current state"""
        self.clear_items()

        # Add dropdown to withdraw bids (only during bidding period and if there are bids)
        if self.period_status == 'bidding' and self.bids:
            options = []
            for bid_id, player_id, amount, player_name, pos, age, ovr, team_name_player, emoji_id in self.bids[:25]:
                options.append(
                    discord.SelectOption(
                        label=f"{player_name} ({pos}, {age}, {ovr})",
                        description=f"Bid: {amount}pts",
                        value=str(bid_id)
                    )
                )

            select = discord.ui.Select(
                placeholder="Select bids to withdraw",
                options=options,
                min_values=1,
                max_values=len(options),
                custom_id="withdraw_select",
                row=0
            )
            select.callback = self.withdraw_callback
            self.add_item(select)

        # Add free re-signs button (only during resign period)
        if self.period_status == 'resign':
            resign_button = discord.ui.Button(
                label="Free Re-Signs",
                style=discord.ButtonStyle.primary,
                custom_id="free_resigns",
                row=1
            )
            resign_button.callback = self.free_resigns_callback
            self.add_item(resign_button)
        elif self.period_status in ['bidding', 'matching']:
            # Greyed out during other periods
            resign_button = discord.ui.Button(
                label="Free Re-Signs",
                style=discord.ButtonStyle.secondary,
                custom_id="free_resigns_disabled",
                disabled=True,
                row=1
            )
            self.add_item(resign_button)

        # Add matching button (only during matching period)
        if self.period_status == 'matching':
            matching_button = discord.ui.Button(
                label="Manage Bid Matches",
                style=discord.ButtonStyle.primary,
                custom_id="manage_matches",
                row=2
            )
            matching_button.callback = self.manage_matches_callback
            self.add_item(matching_button)
        elif self.period_status in ['bidding', 'resign']:
            # Greyed out matching button during bidding/resign
            matching_button = discord.ui.Button(
                label="Manage Bid Matches",
                style=discord.ButtonStyle.secondary,
                custom_id="manage_matches_disabled",
                disabled=True,
                row=2
            )
            self.add_item(matching_button)

        # Add refresh button on last row
        refresh_button = discord.ui.Button(
            label="Refresh",
            style=discord.ButtonStyle.secondary,
            custom_id="refresh",
            row=3
        )
        refresh_button.callback = self.refresh_callback
        self.add_item(refresh_button)

    def create_embed(self):
        embed = discord.Embed(
            title=f"Free Agency Auction - Season {self.season_number}",
            description=f"**Your Team:** {self.team_name}\n**Status:** {self.period_status.title()}",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="Auction Points",
            value=f"**Remaining:** {self.remaining_points} / {self.max_points}",
            inline=False
        )

        if self.bids:
            bid_lines = []
            for bid_id, player_id, amount, player_name, pos, age, ovr, team_name_player, emoji_id in self.bids:
                # Get emoji
                emoji_str = ""
                if emoji_id:
                    try:
                        emoji = self.bot.get_emoji(int(emoji_id))
                        if emoji:
                            emoji_str = f"{emoji} "
                    except:
                        pass

                bid_lines.append(f"• {emoji_str}**{player_name}** ({pos}, {age}, {ovr}) - {team_name_player} - **{amount} pts**")

            embed.add_field(
                name=f"Your Active Bids ({len(self.bids)})",
                value="\n".join(bid_lines),
                inline=False
            )
        else:
            embed.add_field(
                name="Your Active Bids",
                value="*No active bids*",
                inline=False
            )

        if self.period_status == 'bidding':
            footer_text = "Use the dropdown to withdraw bids • Click Refresh to update"
        else:
            footer_text = "Click 'Manage Bid Matches' to respond to bids on your players • Click Refresh to update"

        embed.set_footer(text=footer_text)
        return embed

    async def withdraw_callback(self, interaction: discord.Interaction):
        """Handle withdrawing multiple bids from dropdown"""
        try:
            selected_bid_ids = [int(value) for value in interaction.data['values']]

            async with aiosqlite.connect(DB_PATH) as db:
                # Delete the selected bids
                for bid_id in selected_bid_ids:
                    await db.execute(
                        "DELETE FROM free_agency_bids WHERE bid_id = ?",
                        (bid_id,)
                    )
                await db.commit()

                # Log to bot logs channel
                log_channel = await self.bot.get_cog('FreeAgencyCommands').get_bot_logs_channel(db)
                if log_channel:
                    # Get team info
                    cursor = await db.execute("SELECT team_name, emoji_id FROM teams WHERE team_id = ?", (self.team_id,))
                    team_data = await cursor.fetchone()
                    team_name = team_data[0] if team_data else "Unknown Team"
                    emoji_id = team_data[1] if team_data and team_data[1] else None

                    emoji_str = ""
                    if emoji_id:
                        try:
                            emoji = self.bot.get_emoji(int(emoji_id))
                            if emoji:
                                emoji_str = f"{emoji} "
                        except:
                            pass

                    # Get withdrawn bids for logging
                    withdrawn_bids_temp = [b for b in self.bids if b[0] in selected_bid_ids]
                    player_names_log = ", ".join(b[3] for b in withdrawn_bids_temp)

                    await log_channel.send(f"🚫 {emoji_str}**{team_name}** withdrew bid(s): {player_names_log}")

            # Update view
            withdrawn_bids = [b for b in self.bids if b[0] in selected_bid_ids]
            refund_amount = sum(b[2] for b in withdrawn_bids)
            self.bids = [b for b in self.bids if b[0] not in selected_bid_ids]
            self.remaining_points += refund_amount

            self.update_buttons()
            embed = self.create_embed()
            await interaction.response.edit_message(embed=embed, view=self)

            # Send confirmation
            player_names = ", ".join(b[3] for b in withdrawn_bids)
            await interaction.followup.send(
                f"✅ Withdrawn {len(selected_bid_ids)} bid(s): {player_names} ({refund_amount} points refunded)",
                ephemeral=True
            )

        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)

    async def free_resigns_callback(self, interaction: discord.Interaction):
        """Open the free re-sign selection interface"""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Calculate allowance for this team
                free_agency_cog = self.bot.get_cog('FreeAgencyCommands')
                allowance = await free_agency_cog.calculate_free_resign_allowance(db, self.team_id, self.season_number)

                if allowance == 0:
                    await interaction.response.send_message(
                        "❌ Your team has no free re-sign allowance (need Band 1 or Band 2 free agents).",
                        ephemeral=True
                    )
                    return

                # Get team's free agents
                cursor = await db.execute(
                    """SELECT p.player_id, p.name, p.position, p.age, p.overall_rating
                       FROM players p
                       WHERE p.team_id = ? AND p.contract_expiry = ?
                       ORDER BY p.overall_rating DESC, p.name""",
                    (self.team_id, self.season_number)
                )
                free_agents = await cursor.fetchall()

                # Get existing selections (if any)
                cursor = await db.execute(
                    """SELECT player_id, confirmed FROM free_agency_resigns
                       WHERE period_id = ? AND team_id = ?""",
                    (self.period_id, self.team_id)
                )
                existing_selections = await cursor.fetchall()
                selected_players = [p[0] for p in existing_selections]
                is_confirmed = any(p[1] for p in existing_selections) if existing_selections else False

                # Create the selection view
                view = FreeResignSelectionView(
                    self.bot, self.period_id, self.team_id, allowance,
                    free_agents, selected_players, is_confirmed, self.season_number
                )
                embed = view.create_embed()
                await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)

    async def manage_matches_callback(self, interaction: discord.Interaction):
        """Open the bid matching interface"""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Check if period is still in matching status
                cursor = await db.execute(
                    "SELECT status FROM free_agency_periods WHERE period_id = ?",
                    (self.period_id,)
                )
                period_status = await cursor.fetchone()
                if not period_status or period_status[0] != 'matching':
                    await interaction.response.send_message(
                        "❌ The matching period has ended! Matches are no longer editable.",
                        ephemeral=True
                    )
                    return

                # Get winning bids on this team's players
                cursor = await db.execute(
                    """SELECT p.player_id, p.name, p.position, p.age, p.overall_rating,
                              t.team_id, t.name, t.emoji_id, b.bid_amount
                       FROM players p
                       JOIN free_agency_bids b ON p.player_id = b.player_id
                       JOIN teams t ON b.team_id = t.team_id
                       WHERE p.team_id = ? AND b.period_id = ? AND b.status = 'winning'
                       ORDER BY b.bid_amount DESC""",
                    (self.team_id, self.period_id)
                )
                player_bids = await cursor.fetchall()

                if not player_bids:
                    await interaction.response.send_message(
                        "❌ No winning bids on your players to match!",
                        ephemeral=True
                    )
                    return

                # Create matching view
                matching_view = MatchingView(self.bot, self.period_id, self.team_id, self.team_name, player_bids, self.season_number)
                embed = await matching_view.create_embed()
                await interaction.response.send_message(embed=embed, view=matching_view, ephemeral=True)

        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)

    async def refresh_callback(self, interaction: discord.Interaction):
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Re-fetch bids
                cursor = await db.execute(
                    """SELECT b.bid_id, b.player_id, b.bid_amount, p.name, p.position, p.age, p.overall_rating,
                              t.team_name, t.emoji_id
                       FROM free_agency_bids b
                       JOIN players p ON b.player_id = p.player_id
                       JOIN teams t ON p.team_id = t.team_id
                       WHERE b.period_id = ? AND b.team_id = ? AND b.status = 'active'
                       ORDER BY b.bid_amount DESC, p.name""",
                    (self.period_id, self.team_id)
                )
                self.bids = await cursor.fetchall()

                # Recalculate points
                total_spent = sum(bid[2] for bid in self.bids)
                self.remaining_points = self.max_points - total_spent

                # Re-fetch period status
                cursor = await db.execute(
                    "SELECT status FROM free_agency_periods WHERE period_id = ?",
                    (self.period_id,)
                )
                status_result = await cursor.fetchone()
                if status_result:
                    self.period_status = status_result[0]

            # Recreate view with new buttons
            new_view = AuctionsMenuView(
                self.bot, self.period_id, self.team_id, self.team_name,
                self.bids, self.remaining_points, self.max_points, self.season_number, self.period_status
            )
            embed = new_view.create_embed()
            await interaction.response.edit_message(embed=embed, view=new_view)

        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)


class FreeResignButtonView(discord.ui.View):
    """Simple view with a button to open the free re-sign selection interface"""
    def __init__(self, bot, period_id, team_id, allowance):
        super().__init__(timeout=None)
        self.bot = bot
        self.period_id = period_id
        self.team_id = team_id
        self.allowance = allowance

    @discord.ui.button(label="Select Free Re-Signs", style=discord.ButtonStyle.primary, custom_id="open_resign_ui")
    async def open_resign_ui(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Open the free re-sign selection interface"""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get current season
                cursor = await db.execute(
                    """SELECT season_number FROM seasons
                       ORDER BY
                           CASE status
                               WHEN 'active' THEN 1
                               WHEN 'offseason' THEN 2
                               ELSE 3
                           END,
                           season_number DESC
                       LIMIT 1"""
                )
                season_result = await cursor.fetchone()
                current_season = season_result[0] if season_result else None

                # Get current period_id dynamically (don't rely on stored value)
                cursor = await db.execute(
                    "SELECT period_id FROM free_agency_periods WHERE season_number = ? AND status = 'resign'",
                    (current_season,)
                )
                period_result = await cursor.fetchone()
                if not period_result:
                    await interaction.response.send_message("❌ No active free re-sign period!", ephemeral=True)
                    return

                current_period_id = period_result[0]

                # Calculate allowance dynamically
                free_agency_cog = self.bot.get_cog('FreeAgencyCommands')
                current_allowance = await free_agency_cog.calculate_free_resign_allowance(db, self.team_id, current_season)

                # Get team's free agents
                cursor = await db.execute(
                    """SELECT p.player_id, p.name, p.position, p.age, p.overall_rating
                       FROM players p
                       WHERE p.team_id = ? AND p.contract_expiry = ?
                       ORDER BY p.overall_rating DESC, p.name""",
                    (self.team_id, current_season)
                )
                free_agents = await cursor.fetchall()

                # Get existing selections (if any) - use current period_id
                cursor = await db.execute(
                    """SELECT player_id, confirmed FROM free_agency_resigns
                       WHERE period_id = ? AND team_id = ?""",
                    (current_period_id, self.team_id)
                )
                existing_selections = await cursor.fetchall()
                selected_players = [p[0] for p in existing_selections]
                is_confirmed = any(p[1] for p in existing_selections) if existing_selections else False

                # Create the selection view with current period_id
                view = FreeResignSelectionView(
                    self.bot, current_period_id, self.team_id, current_allowance,
                    free_agents, selected_players, is_confirmed, current_season
                )
                embed = view.create_embed()
                await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)


class FreeResignSelectionView(discord.ui.View):
    """Interactive UI for teams to select which players to re-sign for free"""
    def __init__(self, bot, period_id, team_id, allowance, free_agents, selected_players, is_confirmed, season_number):
        super().__init__(timeout=180)
        self.bot = bot
        self.period_id = period_id
        self.team_id = team_id
        self.allowance = allowance
        self.free_agents = free_agents
        self.selected_players = selected_players
        self.is_confirmed = is_confirmed
        self.season_number = season_number

        # Add player selection dropdown
        self.add_player_dropdown()

        # Add confirm/edit buttons
        if is_confirmed:
            self.add_item(discord.ui.Button(label=f"✓ Confirmed ({len(selected_players)}/{allowance})", style=discord.ButtonStyle.success, disabled=True))
            edit_button = discord.ui.Button(label="Edit Selections", style=discord.ButtonStyle.secondary, custom_id="edit_resigns")
            edit_button.callback = self.edit_selections
            self.add_item(edit_button)
        else:
            confirm_button = discord.ui.Button(
                label=f"Confirm Re-Signs ({len(selected_players)}/{allowance})",
                style=discord.ButtonStyle.primary,
                custom_id="confirm_resigns",
                disabled=(len(selected_players) != allowance and len(selected_players) != 0)
            )
            confirm_button.callback = self.confirm_selections
            self.add_item(confirm_button)

    def add_player_dropdown(self):
        """Add dropdown for player selection"""
        options = []
        for player_id, name, pos, age, ovr in self.free_agents:
            is_selected = player_id in self.selected_players
            label = f"{name} ({pos}, {age}, {ovr})"
            if is_selected:
                label = f"✓ {label}"
            options.append(discord.SelectOption(
                label=label[:100],  # Discord limit
                value=str(player_id),
                default=is_selected
            ))

        if options:
            select = discord.ui.Select(
                placeholder=f"Select players to re-sign (max {self.allowance})",
                options=options,
                min_values=0,
                max_values=min(self.allowance, len(options)),
                custom_id="player_select",
                disabled=self.is_confirmed
            )
            select.callback = self.on_player_select
            self.add_item(select)

    async def on_player_select(self, interaction: discord.Interaction):
        """Handle player selection changes"""
        selected_ids = [int(val) for val in interaction.data['values']]
        self.selected_players = selected_ids

        # Save selections to database (unconfirmed)
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Delete old selections
                await db.execute(
                    "DELETE FROM free_agency_resigns WHERE period_id = ? AND team_id = ?",
                    (self.period_id, self.team_id)
                )

                # Insert new selections
                for player_id in selected_ids:
                    await db.execute(
                        """INSERT INTO free_agency_resigns (period_id, team_id, player_id, confirmed)
                           VALUES (?, ?, ?, 0)""",
                        (self.period_id, self.team_id, player_id)
                    )

                await db.commit()

            # Recreate view with updated selections
            new_view = FreeResignSelectionView(
                self.bot, self.period_id, self.team_id, self.allowance,
                self.free_agents, self.selected_players, False, self.season_number
            )
            embed = new_view.create_embed()
            await interaction.response.edit_message(embed=embed, view=new_view)

        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)

    async def confirm_selections(self, interaction: discord.Interaction):
        """Confirm the selected players"""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Mark selections as confirmed
                await db.execute(
                    """UPDATE free_agency_resigns
                       SET confirmed = 1, confirmed_at = CURRENT_TIMESTAMP
                       WHERE period_id = ? AND team_id = ?""",
                    (self.period_id, self.team_id)
                )
                await db.commit()

                # Log to bot logs channel
                log_channel = await self.bot.get_cog('FreeAgencyCommands').get_bot_logs_channel(db)
                if log_channel:
                    # Get team and player names
                    cursor = await db.execute("SELECT team_name, emoji_id FROM teams WHERE team_id = ?", (self.team_id,))
                    team_data = await cursor.fetchone()
                    team_name = team_data[0] if team_data else "Unknown Team"
                    emoji_id = team_data[1] if team_data and team_data[1] else None

                    emoji_str = ""
                    if emoji_id:
                        try:
                            emoji = self.bot.get_emoji(int(emoji_id))
                            if emoji:
                                emoji_str = f"{emoji} "
                        except:
                            pass

                    if self.selected_players:
                        player_names = []
                        for player_id in self.selected_players:
                            cursor = await db.execute("SELECT name FROM players WHERE player_id = ?", (player_id,))
                            player = await cursor.fetchone()
                            if player:
                                player_names.append(player[0])

                        players_str = ", ".join(player_names)
                        await log_channel.send(f"✅ {emoji_str}**{team_name}** confirmed free re-signs: {players_str}")
                    else:
                        await log_channel.send(f"✅ {emoji_str}**{team_name}** confirmed 0 free re-signs")

            self.is_confirmed = True

            # Recreate view with confirmed state
            new_view = FreeResignSelectionView(
                self.bot, self.period_id, self.team_id, self.allowance,
                self.free_agents, self.selected_players, True, self.season_number
            )
            embed = new_view.create_embed()
            await interaction.response.edit_message(embed=embed, view=new_view)
            await interaction.followup.send("✅ Free re-signs confirmed!", ephemeral=True)

        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)

    async def edit_selections(self, interaction: discord.Interaction):
        """Allow editing of confirmed selections"""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Unconfirm selections
                await db.execute(
                    """UPDATE free_agency_resigns
                       SET confirmed = 0, confirmed_at = NULL
                       WHERE period_id = ? AND team_id = ?""",
                    (self.period_id, self.team_id)
                )
                await db.commit()

            self.is_confirmed = False

            # Recreate view in unconfirmed state
            new_view = FreeResignSelectionView(
                self.bot, self.period_id, self.team_id, self.allowance,
                self.free_agents, self.selected_players, False, self.season_number
            )
            embed = new_view.create_embed()
            await interaction.response.edit_message(embed=embed, view=new_view)

        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)

    def create_embed(self):
        """Create the embed showing current selections"""
        embed = discord.Embed(
            title="🔄 Select Free Re-Signs",
            description=f"You can re-sign **{self.allowance}** player{'s' if self.allowance != 1 else ''} for free.",
            color=discord.Color.green() if self.is_confirmed else discord.Color.blue()
        )

        if self.selected_players:
            # Show selected players
            selected_list = []
            for player_id, name, pos, age, ovr in self.free_agents:
                if player_id in self.selected_players:
                    selected_list.append(f"• **{name}** ({pos}, {age}, {ovr})")

            embed.add_field(
                name=f"Selected Players ({len(self.selected_players)}/{self.allowance})",
                value="\n".join(selected_list) if selected_list else "None",
                inline=False
            )
        else:
            embed.add_field(
                name="No Selections",
                value="Use the dropdown above to select players.",
                inline=False
            )

        if self.is_confirmed:
            embed.set_footer(text="✓ Confirmed - Use 'Edit Selections' to make changes")
        else:
            embed.set_footer(text="Select players from the dropdown, then click 'Confirm Re-Signs'")

        return embed


async def setup(bot):
    await bot.add_cog(FreeAgencyCommands(bot))
