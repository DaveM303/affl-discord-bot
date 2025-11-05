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

                # Get players whose contracts expire this season
                cursor = await db.execute(
                    """SELECT p.player_id, p.name, p.position, p.overall_rating, p.age, t.team_name
                       FROM players p
                       JOIN teams t ON p.team_id = t.team_id
                       WHERE p.contract_expiry = ?
                       ORDER BY p.name""",
                    (current_season - 1,)
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
        """Get compensation band based on player age and OVR from compensation_chart table"""
        cursor = await db.execute(
            """SELECT compensation_band FROM compensation_chart
               WHERE min_age <= ? AND (max_age >= ? OR max_age IS NULL)
               AND min_ovr <= ? AND (max_ovr >= ? OR max_ovr IS NULL)
               LIMIT 1""",
            (age, age, ovr, ovr)
        )
        result = await cursor.fetchone()
        return result[0] if result else None  # None means no compensation

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
                        (current_season - 1, team)
                    )
                else:
                    # Get all free agents
                    cursor = await db.execute(
                        """SELECT p.player_id, p.name, p.position, p.overall_rating, p.age, t.team_name, t.emoji_id
                           FROM players p
                           JOIN teams t ON p.team_id = t.team_id
                           WHERE p.contract_expiry = ?
                           ORDER BY t.team_name, p.overall_rating DESC, p.name""",
                        (current_season - 1,)
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
                    await interaction.followup.send("❌ No active bidding period! Contact an admin to start free agency.")
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

                # Verify player is a free agent
                if contract_expiry != current_season - 1:
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

                # Calculate user's remaining points
                cursor = await db.execute(
                    """SELECT COALESCE(SUM(bid_amount), 0) FROM free_agency_bids
                       WHERE period_id = ? AND team_id = ? AND status = 'active'
                       AND player_id != ?""",
                    (period_id, user_team_id, player_id)
                )
                spent_points = (await cursor.fetchone())[0]

                # Check if user already has a bid on this player
                cursor = await db.execute(
                    """SELECT bid_amount FROM free_agency_bids
                       WHERE period_id = ? AND team_id = ? AND player_id = ?""",
                    (period_id, user_team_id, player_id)
                )
                existing_bid = await cursor.fetchone()
                if existing_bid:
                    spent_points -= existing_bid[0]  # Don't count the old bid

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

                # Get emoji
                emoji_str = ""
                if emoji_id:
                    try:
                        emoji = self.bot.get_emoji(int(emoji_id))
                        if emoji:
                            emoji_str = f"{emoji} "
                    except:
                        pass

                # Calculate new remaining points
                new_remaining = remaining_points - amount

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

                if period_status != 'bidding':
                    await interaction.followup.send(f"❌ Bidding period has ended! Current status: {period_status}")
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
                view = AuctionsMenuView(self.bot, period_id, user_team_id, user_team_name, bids, remaining_points, max_points, current_season)
                embed = view.create_embed()
                await interaction.followup.send(embed=embed, view=view)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}")

    @app_commands.command(name="freeagencyperiod", description="[ADMIN] Control free agency bidding and matching periods")
    @app_commands.describe(action="The action to perform")
    @app_commands.choices(action=[
        app_commands.Choice(name="Start Bidding Period", value="start_bidding"),
        app_commands.Choice(name="Start Matching Period", value="start_matching"),
        app_commands.Choice(name="End Matching Period", value="end_matching")
    ])
    async def free_agency_period(self, interaction: discord.Interaction, action: str):
        # Check if user has admin role
        if not any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles):
            await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        if action == "start_bidding":
            await self.start_bidding_period(interaction)
        elif action == "start_matching":
            await self.start_matching_period(interaction)
        elif action == "end_matching":
            await self.end_matching_period(interaction)

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
                    await interaction.followup.send(f"❌ Free agency period already exists for Season {current_season} (status: {existing[1]})")
                    return

                # Get free agents (only those with a team)
                cursor = await db.execute(
                    """SELECT COUNT(*) FROM players
                       WHERE contract_expiry = ? AND team_id IS NOT NULL""",
                    (current_season - 1,)
                )
                fa_count = (await cursor.fetchone())[0]

                if fa_count == 0:
                    await interaction.followup.send(f"❌ No free agents found for Season {current_season}!")
                    return

                # Create period
                cursor = await db.execute(
                    """INSERT INTO free_agency_periods (season_number, status, auction_points)
                       VALUES (?, 'bidding', 300)""",
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
                    (current_season - 1,)
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
                                embed = view.create_embed()
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
                    new_contract_expiry = current_season + contract_years - 1

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

                        # Get all compensation results for this period, grouped by band
                        cursor = await db.execute(
                            """SELECT result_id, original_team_id, compensation_band, player_id
                               FROM free_agency_results
                               WHERE period_id = ? AND compensation_band IS NOT NULL
                               ORDER BY compensation_band, original_team_id""",
                            (period_id,)
                        )
                        comp_results = await cursor.fetchall()

                        # Process compensation picks by band to ensure correct ordering
                        # Bands 1,3,5 go after natural picks; Bands 2,4 go at end of round
                        for result_id, team_id, comp_band, player_id in comp_results:
                            # Get player name for pick origin description
                            cursor = await db.execute("SELECT name FROM players WHERE player_id = ?", (player_id,))
                            player_name = (await cursor.fetchone())[0]

                            # Determine round and insertion logic based on compensation band
                            if comp_band in [1, 3, 5]:
                                # After team's natural pick in the round
                                round_num = {1: 1, 3: 2, 5: 3}[comp_band]

                                # Get team name to identify their natural pick
                                cursor = await db.execute("SELECT team_name FROM teams WHERE team_id = ?", (team_id,))
                                team_name_result = await cursor.fetchone()
                                if team_name_result:
                                    team_name = team_name_result[0]
                                    natural_pick_origin = f"{team_name} R{round_num}"

                                    # Find the team's natural pick in this round
                                    cursor = await db.execute(
                                        """SELECT pick_number FROM draft_picks
                                           WHERE draft_id = ? AND original_team_id = ? AND round_number = ?
                                           AND pick_origin = ?
                                           ORDER BY pick_number LIMIT 1""",
                                        (draft_id, team_id, round_num, natural_pick_origin)
                                    )
                                    natural_pick = await cursor.fetchone()
                                else:
                                    natural_pick = None

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
                                new_pick_num = (await cursor.fetchone())[0] + 1

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

            # Add all players from this team with team emoji
            for name, pos, age, ovr in players:
                all_players.append(f"{emoji_str}**{name}** ({pos}, {age}, {ovr})")

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

        # Calculate max points (300)
        self.max_points = 300

        # Add toggle buttons for each player
        for player_id, name, pos, age, ovr, winning_team_id, bidding_team, emoji_id, bid in player_bids:
            self.matches[player_id] = False  # Default to not matching

        self.update_buttons()

    def update_buttons(self):
        """Update all buttons based on current state"""
        self.clear_items()

        # Add toggle buttons for each player (limit to first 20)
        for i, (player_id, name, pos, age, ovr, winning_team_id, bidding_team, emoji_id, bid) in enumerate(self.player_bids[:20]):
            is_matched = self.matches.get(player_id, False)
            button = discord.ui.Button(
                label=f"{name}: {'✅ Match' if is_matched else '❌ Let Go'}",
                style=discord.ButtonStyle.success if is_matched else discord.ButtonStyle.danger,
                custom_id=f"toggle_{player_id}",
                row=min(i // 5, 3)
            )
            button.callback = self.create_toggle_callback(player_id)
            self.add_item(button)

        # Add confirm button on last row
        confirm_button = discord.ui.Button(
            label="Confirm Matches",
            style=discord.ButtonStyle.primary,
            custom_id="confirm",
            row=4
        )
        confirm_button.callback = self.confirm_callback
        self.add_item(confirm_button)

    def create_embed(self):
        """Create the embed showing current matching status"""
        embed = discord.Embed(
            title=f"🤝 Free Agency Matching - {self.team_name}",
            description="Your free agents have received bids. Choose which to match:",
            color=discord.Color.orange()
        )

        # Calculate points needed if all current matches go through
        total_cost = sum(bid for player_id, _, _, _, _, _, _, _, bid in self.player_bids if self.matches.get(player_id, False))
        remaining = self.max_points - total_cost

        embed.add_field(
            name="Auction Points",
            value=f"**Cost if matched:** {total_cost} pts\n**Remaining:** {remaining} pts",
            inline=False
        )

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
            player_lines.append(
                f"{status} **{name}** ({pos}, {age}, {ovr})\n"
                f"    └─ {emoji_str}{bidding_team} bid **{bid} pts**"
            )

        embed.add_field(
            name=f"Players ({len(self.player_bids)})",
            value="\n\n".join(player_lines),
            inline=False
        )

        embed.set_footer(text="Toggle each player, then click Confirm Matches when ready")
        return embed

    def create_toggle_callback(self, player_id):
        async def callback(interaction: discord.Interaction):
            # Toggle the match status
            self.matches[player_id] = not self.matches.get(player_id, False)

            # Update buttons and embed
            self.update_buttons()
            embed = self.create_embed()
            await interaction.response.edit_message(embed=embed, view=self)

        return callback

    async def confirm_callback(self, interaction: discord.Interaction):
        """Confirm the matching decisions"""
        try:
            # Calculate total cost
            total_cost = sum(bid for player_id, _, _, _, _, _, _, _, bid in self.player_bids if self.matches.get(player_id, False))

            if total_cost > self.max_points:
                await interaction.response.send_message(
                    f"❌ Insufficient points! You need {total_cost} pts but only have {self.max_points} pts available.",
                    ephemeral=True
                )
                return

            # Update database with matches
            async with aiosqlite.connect(DB_PATH) as db:
                for player_id in self.matches:
                    if self.matches[player_id]:
                        await db.execute(
                            """UPDATE free_agency_results
                               SET matched = 1
                               WHERE period_id = ? AND player_id = ?""",
                            (self.period_id, player_id)
                        )
                await db.commit()

            # Disable all buttons
            for item in self.children:
                item.disabled = True

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
            embed.set_footer(text="Waiting for admin to end matching period...")

            await interaction.response.edit_message(embed=embed, view=self)

        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)


class AuctionsMenuView(discord.ui.View):
    def __init__(self, bot, period_id, team_id, team_name, bids, remaining_points, max_points, season_number):
        super().__init__(timeout=180)
        self.bot = bot
        self.period_id = period_id
        self.team_id = team_id
        self.team_name = team_name
        self.bids = bids
        self.remaining_points = remaining_points
        self.max_points = max_points
        self.season_number = season_number

        # Add withdraw buttons for each bid (max 25 buttons, but we'll limit to reasonable amount)
        for i, bid in enumerate(bids[:20]):  # Limit to 20 bids max
            bid_id, player_id, amount, player_name, pos, age, ovr, team_name_player, emoji_id = bid
            button = discord.ui.Button(
                label=f"Withdraw: {player_name} ({amount}pts)",
                style=discord.ButtonStyle.danger,
                custom_id=f"withdraw_{bid_id}",
                row=min(i // 5, 3)  # Distribute across 4 rows (5 buttons per row)
            )
            button.callback = self.create_withdraw_callback(bid_id, player_name, amount)
            self.add_item(button)

        # Add refresh button on last row
        refresh_button = discord.ui.Button(
            label="Refresh",
            style=discord.ButtonStyle.secondary,
            custom_id="refresh",
            row=4
        )
        refresh_button.callback = self.refresh_callback
        self.add_item(refresh_button)

    def create_embed(self):
        embed = discord.Embed(
            title=f"Free Agency Auction - Season {self.season_number}",
            description=f"**Your Team:** {self.team_name}",
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

        embed.set_footer(text="Use the buttons below to withdraw bids • Click Refresh to update")
        return embed

    def create_withdraw_callback(self, bid_id, player_name, amount):
        async def callback(interaction: discord.Interaction):
            try:
                async with aiosqlite.connect(DB_PATH) as db:
                    # Delete the bid
                    await db.execute(
                        "DELETE FROM free_agency_bids WHERE bid_id = ?",
                        (bid_id,)
                    )
                    await db.commit()

                # Update view
                self.bids = [b for b in self.bids if b[0] != bid_id]
                self.remaining_points += amount

                embed = self.create_embed()
                await interaction.response.edit_message(embed=embed, view=self)

                # Send confirmation
                await interaction.followup.send(
                    f"✅ Withdrawn bid on **{player_name}** ({amount} points refunded)",
                    ephemeral=True
                )

            except Exception as e:
                await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)

        return callback

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

            # Recreate view with new buttons
            new_view = AuctionsMenuView(
                self.bot, self.period_id, self.team_id, self.team_name,
                self.bids, self.remaining_points, self.max_points, self.season_number
            )
            embed = new_view.create_embed()
            await interaction.response.edit_message(embed=embed, view=new_view)

        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)


async def setup(bot):
    await bot.add_cog(FreeAgencyCommands(bot))
