import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
from config import DB_PATH
from utils import (
    get_current_season, is_admin_user, calculate_contract_expiry, get_team_emoji, get_team_emoji_str, get_user_team,
    fetch_teams_for_dropdown, build_team_options,
)
from commands.lineup_commands import clear_departed_players_from_lineups
from compensation_image import render_compensation_chart_image


DEFAULT_AUCTION_POINTS = 300


async def get_fa_period(db):
    """Read the current free agency period from the settings table.

    Returns (status, season_number, auction_points). status/season_number are
    None when no period has ever been started.
    """
    cursor = await db.execute(
        """SELECT setting_key, setting_value FROM settings
           WHERE setting_key IN ('fa_period_status', 'fa_period_season', 'fa_period_auction_points')"""
    )
    rows = await cursor.fetchall()
    values = {key: value for key, value in rows}

    status = values.get('fa_period_status') or None

    season_number = None
    if values.get('fa_period_season') not in (None, ''):
        try:
            season_number = int(values['fa_period_season'])
        except (TypeError, ValueError):
            season_number = None

    auction_points = DEFAULT_AUCTION_POINTS
    if values.get('fa_period_auction_points') not in (None, ''):
        try:
            auction_points = int(values['fa_period_auction_points'])
        except (TypeError, ValueError):
            auction_points = DEFAULT_AUCTION_POINTS

    return status, season_number, auction_points


async def get_fa_period_for_season(db, season_number):
    """Read the current free agency period, but only if it belongs to the given
    season. Returns (status, auction_points) or (None, DEFAULT_AUCTION_POINTS)."""
    status, period_season, auction_points = await get_fa_period(db)
    if status is None or period_season != season_number:
        return None, DEFAULT_AUCTION_POINTS
    return status, auction_points


async def set_fa_period(db, status, season_number, auction_points=DEFAULT_AUCTION_POINTS):
    """Write the current free agency period to the settings table."""
    await db.execute(
        "INSERT OR REPLACE INTO settings (setting_key, setting_value) VALUES (?, ?)",
        ("fa_period_status", str(status))
    )
    await db.execute(
        "INSERT OR REPLACE INTO settings (setting_key, setting_value) VALUES (?, ?)",
        ("fa_period_season", str(season_number))
    )
    await db.execute(
        "INSERT OR REPLACE INTO settings (setting_key, setting_value) VALUES (?, ?)",
        ("fa_period_auction_points", str(auction_points))
    )


async def set_fa_period_status(db, status):
    """Update only the status of the current free agency period."""
    await db.execute(
        "INSERT OR REPLACE INTO settings (setting_key, setting_value) VALUES (?, ?)",
        ("fa_period_status", str(status))
    )


class FreeAgencyCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        """Called when the cog is loaded - re-register persistent views"""
        await self.register_persistent_views()

    async def register_persistent_views(self):
        """Re-register all persistent views on bot startup"""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get current season
                current_season = await get_current_season(db)
                if current_season is None:
                    print("No active season found for view registration")
                    return

                # Check for active free agency period
                status, _ = await get_fa_period_for_season(db, current_season)

                if status in ('resign', 'matching'):
                    # The resign and matching phase-start notifications
                    # carry a persistent FreeAgencyNotificationView (one
                    # "Open Free Agency Hub" button, custom_id
                    # "fa_notification_open_hub") - re-register one
                    # instance per team that has a free agent this season,
                    # so already-posted notification messages keep working
                    # after a bot restart. The bidding-phase notification
                    # is a single buttonless post to the shared auctions
                    # channel (see send_bidding_notifications), so there's
                    # nothing to re-register during that phase.
                    cursor = await db.execute(
                        """SELECT DISTINCT t.team_id
                           FROM teams t
                           JOIN players p ON t.team_id = p.team_id
                           WHERE p.contract_expiry = ?""",
                        (current_season,)
                    )
                    teams_with_fas = await cursor.fetchall()

                    for (team_id,) in teams_with_fas:
                        view = FreeAgencyNotificationView(self.bot, current_season, team_id)
                        self.bot.add_view(view)

                    print(f"Re-registered {len(teams_with_fas)} free agency notification views")

        except Exception as e:
            print(f"Error registering persistent views: {e}")
            import traceback
            traceback.print_exc()

    async def free_agent_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for free agents"""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get current season (active or offseason)
                current_season = await get_current_season(db)
                if current_season is None:
                    return []

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
        except Exception:
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
            except Exception:
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
            except Exception:
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

        # Load the compensation chart once and match bands in Python instead of
        # issuing one query per free agent
        cursor = await db.execute(
            """SELECT min_age, max_age, min_ovr, max_ovr, compensation_band
               FROM compensation_chart
               ORDER BY compensation_band ASC"""
        )
        chart_rows = await cursor.fetchall()

        def band_for(age, ovr):
            for min_age, max_age, min_ovr, max_ovr, band in chart_rows:
                if min_age <= age <= (max_age if max_age is not None else min_age) \
                        and min_ovr <= ovr <= (max_ovr if max_ovr is not None else min_ovr):
                    return band
            return None

        credits = 0.0
        for player_id, age, ovr in free_agents:
            band = band_for(age, ovr)
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

    async def process_free_resigns(self, db, season_number):
        """Process all confirmed free re-signs and assign contracts"""
        # Get all confirmed free re-signs
        cursor = await db.execute(
            """SELECT r.player_id, p.age
               FROM free_agency_resigns r
               JOIN players p ON r.player_id = p.player_id
               WHERE r.season_number = ? AND r.confirmed = 1""",
            (season_number,)
        )
        resigns = await cursor.fetchall()

        for player_id, age in resigns:
            # Get contract length based on age
            contract_years = await self.get_contract_years_for_age(db, age)

            # Calculate new contract expiry
            # season_number is the season that just ended (Offseason 9 means Season 9 just ended)
            # Adding contract_years gives us the last season they'll play under the new contract
            new_contract_expiry = calculate_contract_expiry(season_number, contract_years)

            # Update player's contract
            await db.execute(
                "UPDATE players SET contract_expiry = ? WHERE player_id = ?",
                (new_contract_expiry, player_id)
            )

        await db.commit()

    async def log_free_resign_results(self, db, season_number):
        """Log free re-sign results to auctions channel"""
        log_channel = await self.get_auctions_log_channel(db)
        if not log_channel:
            return

        # Get all confirmed free re-signs
        cursor = await db.execute(
            """SELECT p.name, p.position, p.age, p.overall_rating, t.emoji_id, p.contract_expiry
               FROM free_agency_resigns r
               JOIN players p ON r.player_id = p.player_id
               JOIN teams t ON p.team_id = t.team_id
               WHERE r.season_number = ? AND r.confirmed = 1
               ORDER BY t.team_name, p.name""",
            (season_number,)
        )
        resigns = await cursor.fetchall()

        if not resigns:
            return

        # Build embed
        embed = discord.Embed(
            title=f"Season {season_number} Auctions - Free Re-Signs",
            color=discord.Color.blue()
        )

        # Build list of all re-signs with emoji in front of player name
        player_lines = []
        for name, pos, age, ovr, emoji_id, contract_expiry in resigns:
            # Get emoji
            emoji_str = get_team_emoji_str(self.bot, emoji_id)

            contract_years = contract_expiry - season_number
            player_lines.append(f"{emoji_str}**{name}** ({pos}, {age}, {ovr}) - **{contract_years} years**")

        # Split into fields if needed (max 1024 chars per field)
        if player_lines:
            current_field = []
            current_length = 0
            for line in player_lines:
                line_length = len(line) + 1  # +1 for newline
                if current_length + line_length > 1024:
                    # Add current field and start new one
                    embed.add_field(name="\u200b", value="\n".join(current_field), inline=False)
                    current_field = [line]
                    current_length = line_length
                else:
                    current_field.append(line)
                    current_length += line_length

            # Add final field
            if current_field:
                embed.add_field(name="\u200b", value="\n".join(current_field), inline=False)

        try:
            await log_channel.send(embed=embed)
        except Exception as e:
            print(f"Error logging free re-sign results: {e}")

    async def log_winning_bids(self, db, season_number):
        """Log winning bids to auctions channel"""
        try:
            log_channel = await self.get_auctions_log_channel(db)
            if not log_channel:
                print("No auctions log channel configured")
                return
        except Exception as e:
            print(f"Error getting auctions log channel: {e}")
            return

        # Get this period's auction points allowance
        _, auction_points = await get_fa_period_for_season(db, season_number)

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
               WHERE r.season_number = ? AND r.winning_team_id IS NOT NULL
               ORDER BY r.winning_bid DESC, p.name""",
            (season_number,)
        )
        winning_bids = await cursor.fetchall()

        print(f"log_winning_bids: Found {len(winning_bids)} winning bids")

        if not winning_bids:
            print("log_winning_bids: No winning bids found, returning early")
            return

        # Calculate team points for match checking
        cursor = await db.execute(
            """SELECT team_id FROM teams"""
        )
        all_teams = await cursor.fetchall()

        team_points = {}
        for (team_id,) in all_teams:
            # Calculate spent points (winning bids on OTHER teams' players only)
            cursor = await db.execute(
                """SELECT COALESCE(SUM(b.bid_amount), 0)
                   FROM free_agency_bids b
                   JOIN players p ON b.player_id = p.player_id
                   WHERE b.season_number = ? AND b.team_id = ? AND b.status = 'winning'
                   AND p.team_id != ?""",
                (season_number, team_id, team_id)
            )
            spent = (await cursor.fetchone())[0]
            team_points[team_id] = auction_points - spent

        player_lines = []
        for name, pos, age, ovr, orig_team, orig_emoji, bid_team, bid_emoji, winning_bid in winning_bids:
            # Get emojis
            orig_emoji_str = get_team_emoji_str(self.bot, orig_emoji)

            bid_emoji_str = get_team_emoji_str(self.bot, bid_emoji)

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

        # Split into multiple embeds if needed to avoid description 4096 character limit
        if player_lines:
            embeds = []
            current_embed = discord.Embed(
                title=f"Season {season_number} Auctions - Matching Period",
                description="**Winning bids:**",
                color=discord.Color.gold()
            )
            current_lines = []
            base_description = "**Winning bids:**"

            for line in player_lines:
                # Check if adding this line would exceed the description limit (4096 chars)
                # Use 3900 to be safe and account for formatting
                test_content = base_description + "\n\n" + "\n\n".join(current_lines + [line])
                if len(test_content) > 3900 and current_lines:
                    # Finalize current embed
                    current_embed.description = base_description + "\n\n" + "\n\n".join(current_lines)
                    embeds.append(current_embed)

                    # Start new embed
                    current_embed = discord.Embed(
                        title=f"Season {season_number} Auctions - Matching Period (cont.)",
                        description="**Winning bids (continued):**",
                        color=discord.Color.gold()
                    )
                    current_lines = [line]
                    base_description = "**Winning bids (continued):**"
                else:
                    current_lines.append(line)

            # Add the last embed
            if current_lines:
                current_embed.description = base_description + "\n\n" + "\n\n".join(current_lines)
                embeds.append(current_embed)

            # Send all embeds
            try:
                for embed in embeds:
                    await log_channel.send(embed=embed)
                print(f"Successfully sent {len(embeds)} matching period notification(s) to auctions log channel")
            except Exception as e:
                print(f"Error logging winning bids: {e}")
                import traceback
                traceback.print_exc()

    async def log_final_movements(self, db, season_number):
        """Log final player movements and compensation picks to auctions channel"""
        log_channel = await self.get_auctions_log_channel(db)
        if not log_channel:
            print("No auctions log channel configured for final movements")
            return

        # Get only players who moved clubs (not matched, has new team)
        cursor = await db.execute(
            """SELECT p.name, p.position, p.age, p.overall_rating,
                      orig_team.emoji_id as orig_emoji,
                      new_team.emoji_id as new_emoji,
                      r.compensation_band, r.compensation_pick_id
               FROM free_agency_results r
               JOIN players p ON r.player_id = p.player_id
               JOIN teams orig_team ON r.original_team_id = orig_team.team_id
               LEFT JOIN teams new_team ON r.winning_team_id = new_team.team_id
               WHERE r.season_number = ? AND r.matched = 0 AND r.winning_team_id IS NOT NULL
               ORDER BY p.name""",
            (season_number,)
        )
        transfers = await cursor.fetchall()

        if not transfers:
            # No movements to log
            return

        # Build embed
        embed = discord.Embed(
            title=f"Season {season_number} Free Agency Player Movements",
            color=discord.Color.green()
        )

        # Build movement lines
        movement_lines = []
        for name, pos, age, ovr, orig_emoji, new_emoji, comp_band, comp_pick_id in transfers:
            # Get emojis
            orig_emoji_str = get_team_emoji_str(self.bot, orig_emoji)

            new_emoji_str = ""
            if new_emoji:
                emoji = get_team_emoji(self.bot, new_emoji)
                if emoji:
                    new_emoji_str = f" → {emoji}"

            # Build player line (no team names)
            player_line = f"{orig_emoji_str}**{name}** ({pos}, {age}, {ovr}){new_emoji_str}"

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
                    comp_line = f"└─ {orig_emoji_str}Compensation: **Pick {pick_num}** (Band {comp_band})"
                    player_line += f"\n{comp_line}"
            elif comp_band:
                # Has band but no pick (shouldn't happen)
                comp_line = f"└─ {orig_emoji_str}Compensation: **Band {comp_band}**"
                player_line += f"\n{comp_line}"

            movement_lines.append(player_line)
            movement_lines.append("")  # Add blank line between players

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

    async def fetch_free_agents_grouped(self, db, current_season, team_id=None):
        """Free agents (players.contract_expiry == current_season), optionally
        filtered to one team_id, grouped into {team_name: {emoji_id, players:
        [(name, pos, age, ovr), ...]}} - shared by /freeagencyhub's "View Free
        Agents" button (was previously the standalone /viewfreeagents command)."""
        if team_id is not None:
            cursor = await db.execute(
                """SELECT p.player_id, p.name, p.position, p.overall_rating, p.age, t.team_name, t.emoji_id
                   FROM players p
                   JOIN teams t ON p.team_id = t.team_id
                   WHERE p.contract_expiry = ? AND t.team_id = ?
                   ORDER BY p.overall_rating DESC, p.name""",
                (current_season, team_id)
            )
        else:
            cursor = await db.execute(
                """SELECT p.player_id, p.name, p.position, p.overall_rating, p.age, t.team_name, t.emoji_id
                   FROM players p
                   JOIN teams t ON p.team_id = t.team_id
                   WHERE p.contract_expiry = ?
                   ORDER BY t.team_name, p.overall_rating DESC, p.name""",
                (current_season,)
            )
        free_agents = await cursor.fetchall()

        teams_dict = {}
        for _, name, pos, ovr, age, team_name, emoji_id in free_agents:
            if team_name not in teams_dict:
                teams_dict[team_name] = {'emoji_id': emoji_id, 'players': []}
            teams_dict[team_name]['players'].append((name, pos, age, ovr))
        return teams_dict, len(free_agents)

    async def send_bidding_notifications(self, db, current_season):
        """Posts the live-bidding-phase announcement ONCE to the shared
        auctions channel (settings key 'auctions_log_channel_id'), not
        per-team channels like resign/matching - bidding is a league-wide
        event (every club can bid on every other club's free agents), so
        one shared post fits better than N near-identical per-team ones.
        No button on this one (unlike the resign/matching notifications) -
        a persistent view registered without a message_id is dispatched
        by custom_id alone, so multiple registered instances collide; the
        per-team notifications get away with it because each is
        functionally identical from any team's perspective once resolved,
        but a single shared message has no "the" team to default to, so
        it's simpler to just leave the button off and let teams use
        /freeagencyhub or /placebid directly. Shared by both
        start_bidding_period paths (transitioning from 'resign', and
        starting bidding directly with no resign phase). Returns
        (notifications_sent, skipped_teams) to match the existing
        resign/matching helpers' return shape - notifications_sent is 0
        or 1, skipped_teams explains why if 0."""
        log_channel = await self.get_auctions_log_channel(db)
        if not log_channel:
            return 0, ["No auctions channel configured (use /config to set one)"]

        embed = FreeAgencyNotificationView.create_bidding_embed(current_season)

        try:
            await log_channel.send(embed=embed)
            return 1, []
        except Exception as e:
            print(f"Error posting bidding notification to auctions channel: {e}")
            return 0, [f"Failed to post to auctions channel: {e}"]

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
                current_season = await get_current_season(db)
                if current_season is None:
                    await interaction.followup.send("❌ No active season found!")
                    return

                # Check if there's an active bidding period
                period_status, max_points = await get_fa_period_for_season(db, current_season)
                if period_status != 'bidding':
                    await interaction.followup.send("❌ No active bidding period!")
                    return

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
                user_team_id, _ = await get_user_team(db, interaction.user)

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
                       WHERE season_number = ? AND team_id = ? AND status = 'active'
                       AND player_id != ?""",
                    (current_season, user_team_id, player_id)
                )
                spent_points = (await cursor.fetchone())[0]

                # Check if user already has a bid on this player (for validation)
                cursor = await db.execute(
                    """SELECT bid_amount FROM free_agency_bids
                       WHERE season_number = ? AND team_id = ? AND player_id = ?""",
                    (current_season, user_team_id, player_id)
                )
                existing_bid = await cursor.fetchone()

                remaining_points = max_points - spent_points

                if amount > remaining_points:
                    await interaction.followup.send(
                        f"❌ Insufficient points!\n\n"
                        f"**Available:** {remaining_points} points\n"
                        f"**Bid Amount:** {amount} points\n\n"
                        f"Use `/freeagencyhub` to view your bids."
                    )
                    return

                # Place or update bid
                if existing_bid:
                    await db.execute(
                        """UPDATE free_agency_bids
                           SET bid_amount = ?, updated_at = CURRENT_TIMESTAMP
                           WHERE season_number = ? AND team_id = ? AND player_id = ?""",
                        (amount, current_season, user_team_id, player_id)
                    )
                    action = "Updated"
                else:
                    await db.execute(
                        """INSERT INTO free_agency_bids (season_number, team_id, player_id, bid_amount)
                           VALUES (?, ?, ?, ?)""",
                        (current_season, user_team_id, player_id, amount)
                    )
                    action = "Placed"

                await db.commit()

                # Log to bot logs channel (best-effort - must not mask the successful bid above)
                try:
                    log_channel = await self.get_bot_logs_channel(db)
                    if log_channel:
                        # Get bidding team info
                        cursor = await db.execute("SELECT team_name, emoji_id FROM teams WHERE team_id = ?", (user_team_id,))
                        bidding_team_data = await cursor.fetchone()
                        bidding_team_name = bidding_team_data[0] if bidding_team_data else "Unknown Team"
                        bidding_emoji_id = bidding_team_data[1] if bidding_team_data and bidding_team_data[1] else None

                        bidding_emoji_str = get_team_emoji_str(self.bot, bidding_emoji_id)

                        # Get player's original team emoji
                        original_emoji_str = get_team_emoji_str(self.bot, emoji_id)

                        action_text = "updated their bid on" if existing_bid else "placed a bid on"
                        await log_channel.send(f"💰 {bidding_emoji_str}**{bidding_team_name}** {action_text} {original_emoji_str}**{player_name}**: {amount}pts ({interaction.user.mention})")
                except Exception as e:
                    print(f"Failed to log bid to bot logs channel: {e}")

                # Get emoji
                emoji_str = get_team_emoji_str(self.bot, emoji_id)

                # Calculate new remaining points after this bid
                # spent_points already excludes the old bid if it existed
                new_remaining = max_points - (spent_points + amount)

                embed = discord.Embed(
                    title=f"✅ Bid {action}!",
                    color=discord.Color.green()
                )
                embed.add_field(
                    name="Player",
                    value=f"{emoji_str}**{player_name}** ({pos}, {age}, {ovr})",
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
                embed.set_footer(text="View all bids: /freeagencyhub")

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}")
            return

        # Sent outside the try block so a failure here can't be mistaken for the bid itself failing
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="freeagencyhub", description="Your free agency dashboard - re-signs, bids, matching, and league info")
    async def free_agency_hub(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                current_season = await get_current_season(db)
                if current_season is None:
                    await interaction.followup.send("❌ No active season found!")
                    return

                user_team_id, user_team_name = await get_user_team(db, interaction.user)

                user_team_emoji_id = None
                if user_team_id is not None:
                    cursor = await db.execute("SELECT emoji_id FROM teams WHERE team_id = ?", (user_team_id,))
                    row = await cursor.fetchone()
                    user_team_emoji_id = row[0] if row else None

                view = FreeAgencyHubView(self.bot, user_team_id, user_team_name, current_season, emoji_id=user_team_emoji_id)
                embed = await view.build(db)
                await interaction.followup.send(embed=embed, view=view)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}")

    async def period_action_autocomplete(self, interaction: discord.Interaction, current: str):
        """Dynamic autocomplete for free agency period actions based on current status"""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get current season
                current_season = await get_current_season(db)
                if current_season is None:
                    return [app_commands.Choice(name="Check Status", value="check_status")]

                # Check if there's an existing period
                status, _ = await get_fa_period_for_season(db, current_season)

                choices = [app_commands.Choice(name="Check Status", value="check_status")]

                if not status:
                    # No period - allow starting resign or bidding
                    choices.append(app_commands.Choice(name="Start Free Re-Sign Period", value="start_resign"))
                    choices.append(app_commands.Choice(name="Start Bidding Period", value="start_bidding"))
                else:
                    if status == "resign":
                        choices.append(app_commands.Choice(name="Resend Free Re-Sign Notifications", value="resend_resigns"))
                        choices.append(app_commands.Choice(name="Start Bidding Period", value="start_bidding"))
                    elif status == "bidding":
                        choices.append(app_commands.Choice(name="Start Matching Period", value="start_matching"))
                    elif status == "matching":
                        choices.append(app_commands.Choice(name="Resend Winning Bids Summary", value="resend_winning_bids"))
                        choices.append(app_commands.Choice(name="Resend Matching Notifications", value="resend_matching_notifications"))
                        choices.append(app_commands.Choice(name="End Matching Period", value="end_matching"))

                return choices
        except Exception:
            return [app_commands.Choice(name="Check Status", value="check_status")]

    @app_commands.command(name="freeagencyperiod", description="[ADMIN] Control free agency periods")
    @app_commands.describe(action="The action to perform")
    @app_commands.autocomplete(action=period_action_autocomplete)
    async def free_agency_period(self, interaction: discord.Interaction, action: str):
        # Check if user has admin role
        if not await is_admin_user(interaction):
            await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        if action == "check_status":
            await self.check_period_status(interaction)
        elif action == "resend_resigns":
            await self.resend_free_resigns(interaction)
        elif action == "start_resign":
            await self.start_resign_period(interaction)
        elif action == "start_bidding":
            await self.start_bidding_period(interaction)
        elif action == "start_matching":
            await self.start_matching_period(interaction)
        elif action == "resend_winning_bids":
            await self.resend_winning_bids_summary(interaction)
        elif action == "resend_matching_notifications":
            await self.resend_matching_notifications(interaction)
        elif action == "end_matching":
            await self.end_matching_period(interaction)

    async def check_period_status(self, interaction: discord.Interaction):
        """Check the current free agency period status"""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get current season
                current_season = await get_current_season(db)
                if current_season is None:
                    await interaction.followup.send("❌ No active season found!")
                    return

                # Check if there's an existing period
                status, _ = await get_fa_period_for_season(db, current_season)

                if not status:
                    await interaction.followup.send(
                        f"📊 **Free Agency Status - Season {current_season}**\n\n"
                        f"**Status:** No active free agency period\n\n"
                        f"Use `/freeagencyperiod` to start a free re-sign or bidding period."
                    )
                    return

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
                               WHERE season_number = ? AND confirmed = 1
                           )
                           AND (
                               SELECT COUNT(*)
                               FROM players p2
                               WHERE p2.team_id = t.team_id
                               AND p2.contract_expiry = ?
                           ) > 0""",
                        (current_season, current_season, current_season)
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
                        "SELECT COUNT(*) FROM free_agency_bids WHERE season_number = ?",
                        (current_season,)
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
                           WHERE r.season_number = ? AND r.winning_team_id IS NOT NULL
                           AND r.confirmed_at IS NULL""",
                        (current_season,)
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

    async def resend_free_resigns(self, interaction: discord.Interaction):
        """Resend free re-sign notifications without affecting existing data"""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get current season
                current_season = await get_current_season(db)
                if current_season is None:
                    await interaction.followup.send("❌ No active season found!")
                    return

                # Check if there's an active resign period
                status, _ = await get_fa_period_for_season(db, current_season)
                if status != 'resign':
                    await interaction.followup.send("❌ No active free re-sign period!")
                    return

                # Get all teams with free agents and calculate their allowances
                cursor = await db.execute(
                    """SELECT DISTINCT t.team_id, t.team_name, t.channel_id, t.emoji_id
                       FROM teams t
                       JOIN players p ON t.team_id = p.team_id
                       WHERE p.contract_expiry = ?""",
                    (current_season,)
                )
                teams_with_fas = await cursor.fetchall()

                # Resend to EVERY team with a free agent, not just ones with
                # a nonzero re-sign allowance - same reasoning as
                # start_resign_period's initial notification.
                notifications_sent = 0

                for team_id, team_name, channel_id, emoji_id in teams_with_fas:
                    allowance = await self.calculate_free_resign_allowance(db, team_id, current_season)

                    if not channel_id:
                        continue

                    cursor = await db.execute(
                        """SELECT p.player_id, p.name, p.position, p.age, p.overall_rating
                           FROM players p
                           WHERE p.team_id = ? AND p.contract_expiry = ?
                           ORDER BY p.overall_rating DESC, p.name""",
                        (team_id, current_season)
                    )
                    free_agents = await cursor.fetchall()

                    band_by_player_id = {}
                    for player_id, name, pos, age, ovr in free_agents:
                        band_by_player_id[player_id] = await self.get_compensation_band(db, age, ovr)

                    embed = FreeAgencyNotificationView.create_resign_embed(
                        self.bot, emoji_id, allowance, free_agents, band_by_player_id
                    )

                    try:
                        channel = self.bot.get_channel(int(channel_id))
                        if channel:
                            view = FreeAgencyNotificationView(self.bot, current_season, team_id)
                            await channel.send(embed=embed, view=view)
                            notifications_sent += 1
                    except Exception as e:
                        print(f"Error resending notification to {team_name}: {e}")

                await interaction.followup.send(f"✅ Resent free re-sign notifications to {notifications_sent} teams.")

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}")

    async def start_resign_period(self, interaction: discord.Interaction):
        """Start the free re-sign period for free agency"""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get current season (active or offseason)
                current_season = await get_current_season(db)
                if current_season is None:
                    await interaction.followup.send("❌ No active season found!")
                    return

                # Check if period already exists
                existing_status, _ = await get_fa_period_for_season(db, current_season)
                if existing_status:
                    await interaction.followup.send(f"❌ Free agency period already exists for Season {current_season} (status: {existing_status})")
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
                await set_fa_period(db, 'resign', current_season, DEFAULT_AUCTION_POINTS)
                await db.commit()

                # Get all teams with free agents and calculate their allowances
                cursor = await db.execute(
                    """SELECT DISTINCT t.team_id, t.team_name, t.channel_id, t.emoji_id
                       FROM teams t
                       JOIN players p ON t.team_id = p.team_id
                       WHERE p.contract_expiry = ?""",
                    (current_season,)
                )
                teams_with_fas = await cursor.fetchall()

                # Send notifications to EVERY team with a free agent, not
                # just ones with a nonzero re-sign allowance - a team with
                # 0 allowance still needs to know it's the resign phase and
                # which of its players are free agents (they're about to
                # become bid targets for opposition clubs either way).
                notifications_sent = 0
                skipped_teams = []  # Teams eligible but not notified, with a reason

                for team_id, team_name, channel_id, emoji_id in teams_with_fas:
                    allowance = await self.calculate_free_resign_allowance(db, team_id, current_season)

                    if not channel_id:
                        skipped_teams.append(f"{team_name}: no channel configured")
                        continue

                    cursor = await db.execute(
                        """SELECT p.player_id, p.name, p.position, p.age, p.overall_rating
                           FROM players p
                           WHERE p.team_id = ? AND p.contract_expiry = ?
                           ORDER BY p.overall_rating DESC, p.name""",
                        (team_id, current_season)
                    )
                    free_agents = await cursor.fetchall()

                    band_by_player_id = {}
                    for player_id, name, pos, age, ovr in free_agents:
                        band_by_player_id[player_id] = await self.get_compensation_band(db, age, ovr)

                    embed = FreeAgencyNotificationView.create_resign_embed(
                        self.bot, emoji_id, allowance, free_agents, band_by_player_id
                    )

                    try:
                        channel = self.bot.get_channel(int(channel_id))
                        if channel:
                            view = FreeAgencyNotificationView(self.bot, current_season, team_id)
                            await channel.send(embed=embed, view=view)
                            notifications_sent += 1
                        else:
                            skipped_teams.append(f"{team_name}: channel not found (ID: {channel_id})")
                    except Exception as e:
                        skipped_teams.append(f"{team_name}: {e}")
                        print(f"Error sending notification to {team_name}: {e}")

                # Build response
                response = (
                    f"✅ **Free Re-Sign Period Started!**\n\n"
                    f"Season: {current_season}\n"
                    f"Free Agents: {fa_count}\n"
                    f"Notifications sent: {notifications_sent} teams with free agents\n\n"
                )
                if skipped_teams:
                    response += f"⚠️ **Not notified:**\n" + "\n".join(skipped_teams[:20]) + "\n\n"
                response += f"Once all teams have confirmed their free re-signs, you can start the bidding period."

                await interaction.followup.send(response)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}")

    async def start_bidding_period(self, interaction: discord.Interaction):
        """Start the bidding period for free agency"""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get current season (active or offseason)
                current_season = await get_current_season(db)
                if current_season is None:
                    await interaction.followup.send("❌ No active season found!")
                    return

                # Check if period already exists
                status, _ = await get_fa_period_for_season(db, current_season)

                if status:
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
                                       WHERE season_number = ? AND team_id = ? AND confirmed = 1""",
                                    (current_season, team_id)
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
                        await self.process_free_resigns(db, current_season)

                        # Log free re-sign results
                        await self.log_free_resign_results(db, current_season)

                        # Update period status to bidding
                        await set_fa_period_status(db, 'bidding')
                        await db.commit()

                        bidding_notifications_sent, bidding_skipped = await self.send_bidding_notifications(db, current_season)

                        response = (
                            f"✅ **Free Agency Bidding Period Started!**\n\n"
                            f"Season: {current_season}\n"
                            f"Free re-signs processed successfully!\n"
                            f"Auctions channel announcement: {'posted' if bidding_notifications_sent else 'FAILED'}\n\n"
                        )
                        if bidding_skipped:
                            response += "⚠️ **Issue:**\n" + "\n".join(bidding_skipped[:20]) + "\n\n"
                        response += "Teams can now use `/placebid` to bid on opposition free agents."

                        await interaction.followup.send(response)
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
                await set_fa_period(db, 'bidding', current_season, DEFAULT_AUCTION_POINTS)
                await db.commit()

                bidding_notifications_sent, bidding_skipped = await self.send_bidding_notifications(db, current_season)

                response = (
                    f"✅ **Free Agency Bidding Period Started!**\n\n"
                    f"Season: {current_season}\n"
                    f"Free Agents: {fa_count}\n"
                    f"Auctions channel announcement: {'posted' if bidding_notifications_sent else 'FAILED'}\n\n"
                )
                if bidding_skipped:
                    response += "⚠️ **Issue:**\n" + "\n".join(bidding_skipped[:20]) + "\n\n"
                response += "Teams can now use `/placebid` to bid on opposition free agents."

                await interaction.followup.send(response)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}")

    async def start_matching_period(self, interaction: discord.Interaction):
        """End bidding, calculate winners, and start matching period"""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get current season (active or offseason)
                current_season = await get_current_season(db)
                if current_season is None:
                    await interaction.followup.send("❌ No active season found!")
                    return

                # Get period
                status, auction_points = await get_fa_period_for_season(db, current_season)
                if not status:
                    await interaction.followup.send("❌ No free agency period found! Start bidding first.")
                    return

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
                           WHERE b.season_number = ? AND b.player_id = ? AND b.status = 'active'
                           ORDER BY b.bid_amount DESC, lp.position ASC""",
                        (current_season, current_season, player_id)
                    )
                    bids = await cursor.fetchall()

                    if bids:
                        # Winner is highest bid, tiebreaker by ladder position (lower is better)
                        winning_team_id, winning_bid, _ = bids[0]

                        # Create result
                        await db.execute(
                            """INSERT INTO free_agency_results
                               (season_number, player_id, original_team_id, winning_team_id, winning_bid, matched)
                               VALUES (?, ?, ?, ?, ?, 0)""",
                            (current_season, player_id, original_team_id, winning_team_id, winning_bid)
                        )
                        results_created += 1

                        # Mark other bids as outbid (they will get points refunded)
                        await db.execute(
                            """UPDATE free_agency_bids
                               SET status = 'outbid'
                               WHERE season_number = ? AND player_id = ? AND team_id != ?""",
                            (current_season, player_id, winning_team_id)
                        )

                        # Mark winning bid
                        await db.execute(
                            """UPDATE free_agency_bids
                               SET status = 'winning'
                               WHERE season_number = ? AND player_id = ? AND team_id = ?""",
                            (current_season, player_id, winning_team_id)
                        )
                    else:
                        # No bids - will be auto re-signed
                        await db.execute(
                            """INSERT INTO free_agency_results
                               (season_number, player_id, original_team_id, winning_team_id, winning_bid, matched)
                               VALUES (?, ?, ?, NULL, NULL, 0)""",
                            (current_season, player_id, original_team_id)
                        )

                # Update period status
                await set_fa_period_status(db, 'matching')
                await db.commit()

                # Log winning bids
                await self.log_winning_bids(db, current_season)

                # Send a matching notification to EVERY team with a free
                # agent this season, not just teams who got a bid - a team
                # with no bids still gets told "none of your free agents
                # received a bid" rather than nothing at all.
                cursor = await db.execute(
                    """SELECT DISTINCT t.team_id, t.team_name, t.channel_id, t.emoji_id
                       FROM free_agency_results r
                       JOIN teams t ON r.original_team_id = t.team_id
                       WHERE r.season_number = ?
                       AND t.channel_id IS NOT NULL""",
                    (current_season,)
                )
                teams_with_fas = await cursor.fetchall()

                matching_messages_sent = 0
                for team_id, team_name, channel_id, emoji_id in teams_with_fas:
                    try:
                        # Get this team's players with bids (empty if none
                        # of their free agents received one)
                        cursor = await db.execute(
                            """SELECT r.player_id, p.name, p.position, p.age, p.overall_rating,
                                      r.winning_team_id, t.team_name, t.emoji_id, r.winning_bid
                               FROM free_agency_results r
                               JOIN players p ON r.player_id = p.player_id
                               JOIN teams t ON r.winning_team_id = t.team_id
                               WHERE r.season_number = ? AND r.original_team_id = ?""",
                            (current_season, team_id)
                        )
                        player_bids = await cursor.fetchall()

                        # Calculate remaining points for this team (auction_points - winning bids on other teams' players)
                        cursor = await db.execute(
                            """SELECT COALESCE(SUM(b.bid_amount), 0)
                               FROM free_agency_bids b
                               JOIN players p ON b.player_id = p.player_id
                               WHERE b.season_number = ? AND b.team_id = ? AND b.status = 'winning'
                               AND p.team_id != ?""",
                            (current_season, team_id, team_id)
                        )
                        winning_bid_total = (await cursor.fetchone())[0]
                        remaining_points = auction_points - winning_bid_total

                        band_by_player_id = {}
                        for player_id, name, pos, age, ovr, *_ in player_bids:
                            band_by_player_id[player_id] = await self.get_compensation_band(db, age, ovr)

                        # This team's OWN bids placed on opposition free
                        # agents this period (won or lost) - r.original_team_id
                        # is the player's team AT THE TIME the bid was
                        # resolved, so this stays correct even once
                        # winning bids move players between teams.
                        cursor = await db.execute(
                            """SELECT r.player_id, p.name, p.position, p.age, p.overall_rating,
                                      r.original_team_id, ot.team_name, ot.emoji_id, b.bid_amount, b.status
                               FROM free_agency_bids b
                               JOIN players p ON b.player_id = p.player_id
                               JOIN free_agency_results r ON r.season_number = b.season_number AND r.player_id = b.player_id
                               JOIN teams ot ON r.original_team_id = ot.team_id
                               WHERE b.season_number = ? AND b.team_id = ? AND b.status IN ('winning', 'outbid')
                               ORDER BY b.status, p.name""",
                            (current_season, team_id)
                        )
                        placed_bids = await cursor.fetchall()

                        channel = self.bot.get_channel(int(channel_id))
                        if channel:
                            view = FreeAgencyNotificationView(self.bot, current_season, team_id)
                            embed = await FreeAgencyNotificationView.create_matching_embed(
                                self.bot, current_season, team_id, team_name, emoji_id, player_bids, remaining_points,
                                band_by_player_id, placed_bids
                            )
                            await channel.send(embed=embed, view=view)
                            matching_messages_sent += 1
                    except Exception as e:
                        print(f"Error sending matching message to {team_name}: {e}")

                await interaction.followup.send(
                    f"✅ **Matching Period Started!**\n\n"
                    f"Winning bids calculated: {results_created}\n"
                    f"Notifications sent: {matching_messages_sent} teams with free agents\n\n"
                    f"Teams can now match bids on their players."
                )

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}")

    async def resend_winning_bids_summary(self, interaction: discord.Interaction):
        """Resend the winning bids summary to auctions log channel"""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get current season
                current_season = await get_current_season(db)
                if current_season is None:
                    await interaction.followup.send("❌ No active season found!")
                    return

                # Get period
                status, _ = await get_fa_period_for_season(db, current_season)
                if not status:
                    await interaction.followup.send("❌ No free agency period found!")
                    return

                if status != 'matching':
                    await interaction.followup.send(f"❌ Period is not in matching status (current: {status})")
                    return

                # Resend winning bids summary
                await self.log_winning_bids(db, current_season)

                await interaction.followup.send("✅ Winning bids summary resent to auctions log channel!")

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}")

    async def resend_matching_notifications(self, interaction: discord.Interaction):
        """Resend matching notifications to all teams with winning bids on their players"""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get current season
                current_season = await get_current_season(db)
                if current_season is None:
                    await interaction.followup.send("❌ No active season found!")
                    return

                # Get period
                status, auction_points = await get_fa_period_for_season(db, current_season)
                if not status:
                    await interaction.followup.send("❌ No free agency period found!")
                    return

                if status != 'matching':
                    await interaction.followup.send(f"❌ Period is not in matching status (current: {status})")
                    return

                # Resend to EVERY team with a free agent this season, not
                # just teams who got a bid - same reasoning as
                # start_matching_period's initial notification.
                cursor = await db.execute(
                    """SELECT DISTINCT t.team_id, t.team_name, t.channel_id, t.emoji_id
                       FROM free_agency_results r
                       JOIN teams t ON r.original_team_id = t.team_id
                       WHERE r.season_number = ?
                       AND t.channel_id IS NOT NULL""",
                    (current_season,)
                )
                teams_with_fas = await cursor.fetchall()

                matching_messages_sent = 0
                for team_id, team_name, channel_id, emoji_id in teams_with_fas:
                    try:
                        # Get this team's players with bids (empty if none)
                        cursor = await db.execute(
                            """SELECT r.player_id, p.name, p.position, p.age, p.overall_rating,
                                      r.winning_team_id, t.team_name, t.emoji_id, r.winning_bid
                               FROM free_agency_results r
                               JOIN players p ON r.player_id = p.player_id
                               JOIN teams t ON r.winning_team_id = t.team_id
                               WHERE r.season_number = ? AND r.original_team_id = ?""",
                            (current_season, team_id)
                        )
                        player_bids = await cursor.fetchall()

                        # Calculate remaining points for this team (auction_points - winning bids on other teams' players)
                        cursor = await db.execute(
                            """SELECT COALESCE(SUM(b.bid_amount), 0)
                               FROM free_agency_bids b
                               JOIN players p ON b.player_id = p.player_id
                               WHERE b.season_number = ? AND b.team_id = ? AND b.status = 'winning'
                               AND p.team_id != ?""",
                            (current_season, team_id, team_id)
                        )
                        winning_bid_total = (await cursor.fetchone())[0]
                        remaining_points = auction_points - winning_bid_total

                        band_by_player_id = {}
                        for player_id, name, pos, age, ovr, *_ in player_bids:
                            band_by_player_id[player_id] = await self.get_compensation_band(db, age, ovr)

                        # This team's OWN bids placed on opposition free
                        # agents this period (won or lost) - r.original_team_id
                        # is the player's team AT THE TIME the bid was
                        # resolved, so this stays correct even once
                        # winning bids move players between teams.
                        cursor = await db.execute(
                            """SELECT r.player_id, p.name, p.position, p.age, p.overall_rating,
                                      r.original_team_id, ot.team_name, ot.emoji_id, b.bid_amount, b.status
                               FROM free_agency_bids b
                               JOIN players p ON b.player_id = p.player_id
                               JOIN free_agency_results r ON r.season_number = b.season_number AND r.player_id = b.player_id
                               JOIN teams ot ON r.original_team_id = ot.team_id
                               WHERE b.season_number = ? AND b.team_id = ? AND b.status IN ('winning', 'outbid')
                               ORDER BY b.status, p.name""",
                            (current_season, team_id)
                        )
                        placed_bids = await cursor.fetchall()

                        channel = self.bot.get_channel(int(channel_id))
                        if channel:
                            view = FreeAgencyNotificationView(self.bot, current_season, team_id)
                            embed = await FreeAgencyNotificationView.create_matching_embed(
                                self.bot, current_season, team_id, team_name, emoji_id, player_bids, remaining_points,
                                band_by_player_id, placed_bids
                            )
                            await channel.send(embed=embed, view=view)
                            matching_messages_sent += 1
                    except Exception as e:
                        print(f"Error sending matching message to {team_name}: {e}")

                await interaction.followup.send(
                    f"✅ Matching notifications resent to {matching_messages_sent} teams!"
                )

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}")

    async def send_auction_summaries(self, db, season_number):
        """Send auction summary to each team's channel"""
        try:
            # Get all teams
            cursor = await db.execute("SELECT team_id, team_name, emoji_id, channel_id FROM teams")
            teams = await cursor.fetchall()

            for team_id, team_name, emoji_id, channel_id in teams:
                # Skip Draft Pool
                if team_name == "Draft Pool":
                    continue

                # Get team emoji
                team_emoji = get_team_emoji_str(self.bot, emoji_id)

                # Get players gained (won bids)
                cursor = await db.execute(
                    """SELECT p.name, p.position, p.age, p.overall_rating, ot.emoji_id
                       FROM free_agency_results r
                       JOIN players p ON r.player_id = p.player_id
                       JOIN teams ot ON r.original_team_id = ot.team_id
                       WHERE r.season_number = ? AND r.winning_team_id = ? AND r.matched = 0
                       ORDER BY p.overall_rating DESC, p.name""",
                    (season_number, team_id)
                )
                players_gained = await cursor.fetchall()

                # Get players lost (original team, lost to winning bids)
                cursor = await db.execute(
                    """SELECT p.name, p.position, p.age, p.overall_rating, r.compensation_band,
                              dp.pick_number, r.matched, wt.emoji_id
                       FROM free_agency_results r
                       JOIN players p ON r.player_id = p.player_id
                       LEFT JOIN draft_picks dp ON r.compensation_pick_id = dp.pick_id
                       LEFT JOIN teams wt ON r.winning_team_id = wt.team_id
                       WHERE r.season_number = ? AND r.original_team_id = ? AND (r.winning_team_id IS NOT NULL OR r.matched = 1)
                       ORDER BY p.overall_rating DESC, p.name""",
                    (season_number, team_id)
                )
                players_lost = await cursor.fetchall()

                # Check if there are any auto re-signed players who had no bids
                cursor = await db.execute(
                    """SELECT COUNT(*)
                       FROM free_agency_resigns fr
                       JOIN players p ON fr.player_id = p.player_id
                       WHERE fr.season_number = ? AND fr.team_id = ? AND fr.confirmed = 1
                       AND NOT EXISTS (
                           SELECT 1 FROM free_agency_results r
                           WHERE r.season_number = ? AND r.player_id = p.player_id
                       )""",
                    (season_number, team_id, season_number)
                )
                auto_resigned_count = (await cursor.fetchone())[0]

                # Skip if no activity for this team
                if not players_gained and not players_lost and auto_resigned_count == 0:
                    continue

                # Build summary embed
                embed = discord.Embed(
                    title=f"{team_emoji}Free Agency Period Summary",
                    color=discord.Color.blue()
                )

                # Players Gained
                gained_text = ""
                if players_gained:
                    for player_name, pos, age, ovr, prev_emoji_id in players_gained:
                        prev_emoji = get_team_emoji_str(self.bot, prev_emoji_id)
                        gained_text += f"{prev_emoji}**{player_name}** ({pos}, {age}, {ovr})\n"
                else:
                    gained_text = "*None*"

                embed.add_field(name=":green_circle: Players Gained", value=gained_text, inline=False)

                # Add a single blank line between sections
                embed.add_field(name="", value="", inline=False)

                # Players Lost
                lost_text = ""
                has_lost = False
                if players_lost:
                    for player_name, pos, age, ovr, comp_band, pick_num, matched, new_team_emoji_id in players_lost:
                        if matched:
                            # Player was matched - stayed with original team
                            continue

                        has_lost = True

                        # Get new team emoji
                        new_team_emoji = ""
                        if new_team_emoji_id:
                            emoji = get_team_emoji(self.bot, new_team_emoji_id)
                            new_team_emoji = " → " + str(emoji) if emoji else ""

                        lost_text += f"**{player_name}** ({pos}, {age}, {ovr}){new_team_emoji}\n"
                        if comp_band and pick_num:
                            lost_text += f"└─ Compensation: **Pick {pick_num}** (Band {comp_band})\n"
                        elif comp_band:
                            # Band exists but no pick number (shouldn't happen, but handle it)
                            lost_text += f"└─ Compensation: **Band {comp_band}**\n"
                        else:
                            # No compensation granted
                            lost_text += f"└─ No compensation granted\n"

                if not has_lost:
                    lost_text = "*None*"

                embed.add_field(name=":red_circle: Players Lost", value=lost_text, inline=False)

                # Footer - always show for all teams
                embed.set_footer(text="All other free agents have been re-signed")

                # Send to team channel
                if channel_id:
                    try:
                        channel = self.bot.get_channel(int(channel_id))
                        if channel:
                            await channel.send(embed=embed)
                    except Exception as e:
                        print(f"Error sending auction summary to {team_name}: {e}")

        except Exception as e:
            print(f"Error sending auction summaries: {e}")
            import traceback
            traceback.print_exc()

    async def end_matching_period(self, interaction: discord.Interaction):
        """Process matches, assign players, calculate compensation"""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get current season (active or offseason)
                current_season = await get_current_season(db)
                if current_season is None:
                    await interaction.followup.send("❌ No active season found!")
                    return

                # Get period
                status, auction_points = await get_fa_period_for_season(db, current_season)
                if not status:
                    await interaction.followup.send("❌ No free agency period found!")
                    return

                if status != 'matching':
                    await interaction.followup.send(f"❌ Period is not in matching status (current: {status})")
                    return

                # Check if all teams have confirmed their matches
                # Get teams with winning bids on their players and check if they've confirmed
                cursor = await db.execute(
                    """SELECT DISTINCT t.team_id, t.team_name
                       FROM free_agency_results r
                       JOIN teams t ON r.original_team_id = t.team_id
                       WHERE r.season_number = ? AND r.winning_team_id IS NOT NULL""",
                    (current_season,)
                )
                teams_with_bids = await cursor.fetchall()

                # Check which teams haven't confirmed
                unconfirmed_teams = []
                for team_id, team_name in teams_with_bids:
                    # Check if this team has any unconfirmed results (confirmed_at IS NULL)
                    cursor = await db.execute(
                        """SELECT COUNT(*) FROM free_agency_results
                           WHERE season_number = ? AND original_team_id = ?
                           AND winning_team_id IS NOT NULL AND confirmed_at IS NULL""",
                        (current_season, team_id)
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
                       WHERE r.season_number = ?""",
                    (current_season,)
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
                    new_contract_expiry = calculate_contract_expiry(current_season, contract_years)

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
                        # They no longer play for their old team, so strip
                        # them out of its lineups - a leftover row reads as
                        # an empty slot to validate_lineup and blocks
                        # force-submit (see clear_departed_players_from_lineups).
                        await clear_departed_players_from_lineups(db, [player_id], original_team_id)
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
                await set_fa_period_status(db, 'completed')

                # Clear all bids for this period now that it's completed
                # This "refunds" all auction points for the next season
                await db.execute(
                    "DELETE FROM free_agency_bids WHERE season_number = ?",
                    (current_season,)
                )

                # Insert compensation picks into the current draft automatically
                picks_inserted = 0
                draft_name = None
                if compensation_picks > 0:
                    # Find the draft for THIS free agency period's own
                    # season, not just "whichever draft happens to be
                    # 'current'" - a season's National Draft is named
                    # after (and stored with season_number = ) the season
                    # AFTER the one its ladder is based on (see
                    # ensure_future_seasons_exist's naming convention), so
                    # this period's compensation picks belong in
                    # season_number = current_season + 1. Scoping
                    # explicitly guards against ever misfiling a
                    # compensation pick into the wrong draft if two drafts
                    # were somehow both left at 'current' at once (e.g. an
                    # admin started this season without first completing/
                    # starting last season's draft) - previously this had
                    # no season filter and no LIMIT 1 at all.
                    cursor = await db.execute(
                        "SELECT draft_id, draft_name, season_number FROM drafts WHERE season_number = ? AND status = 'current'",
                        (current_season + 1,)
                    )
                    draft = await cursor.fetchone()

                    if draft:
                        draft_id, draft_name, draft_season = draft

                        # Get all compensation results for this period, ordered by band and ladder position
                        # For bands 1,3,5: order doesn't matter as much (inserted after natural pick)
                        # For bands 2,4: MUST be in reverse ladder order (worst team = highest position number = pick first)
                        cursor = await db.execute(
                            """SELECT r.result_id, r.original_team_id, r.compensation_band, r.player_id
                               FROM free_agency_results r
                               LEFT JOIN ladder_positions lp ON r.original_team_id = lp.team_id
                               LEFT JOIN seasons s ON lp.season_id = s.season_id AND s.season_number = ?
                               WHERE r.season_number = ? AND r.compensation_band IS NOT NULL
                               ORDER BY r.compensation_band,
                                        CASE
                                            WHEN r.compensation_band IN (2, 4) THEN lp.position
                                            ELSE r.original_team_id
                                        END DESC""",
                            (current_season, current_season)
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
                                 f"Band {comp_band} Compensation", team_id, team_id)
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
                await self.log_final_movements(db, current_season)

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
                           WHERE r.season_number = ? AND r.compensation_band IS NOT NULL
                           ORDER BY r.compensation_band, t.team_name""",
                        (current_season,)
                    )
                    comp_picks = await cursor.fetchall()

                    if draft_name:
                        message += f"\n\n**Compensation Picks (inserted into {draft_name}):**"
                    else:
                        message += "\n\n**Compensation Picks (no current draft found - picks not inserted):**"

                    for team_name, band, player_name in comp_picks:
                        message += f"\n• **{team_name}**: Band {band} pick (lost {player_name})"

                # Send auction summaries to all team channels
                await self.send_auction_summaries(db, current_season)

                # Clear re-sign selections for this period now that summaries are sent
                await db.execute(
                    "DELETE FROM free_agency_resigns WHERE season_number = ?",
                    (current_season,)
                )
                await db.commit()

                await interaction.followup.send(message)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}")

    async def build_contract_status_embed(self, db, team_id, team_name, emoji_id=None):
        """The contract-status embed for one team, grouped by contract
        expiry year with 1024-char field auto-splitting - shared by
        /freeagencyhub's "Contract Status" button (was previously the
        standalone /contractstatus command). Returns None if the team has
        no players."""
        cursor = await db.execute(
            """SELECT contract_expiry, name, position, age, overall_rating
               FROM players
               WHERE team_id = ?
               ORDER BY contract_expiry ASC, overall_rating DESC, name""",
            (team_id,)
        )
        players = await cursor.fetchall()
        if not players:
            return None

        players_by_year = {}
        for contract_expiry, name, pos, age, ovr in players:
            players_by_year.setdefault(contract_expiry, []).append(f"{name} ({pos}, {age}, {ovr})")

        emoji_str = get_team_emoji_str(self.bot, emoji_id)
        embed = discord.Embed(
            title=f"{emoji_str}📋 Contract Status",
            description="Players grouped by contract expiry year",
            color=discord.Color.blue()
        )

        for year in sorted(players_by_year.keys()):
            player_list = players_by_year[year]
            field_value = "\n".join(player_list)
            if len(field_value) > 1024:
                chunks = []
                current_chunk = []
                current_length = 0
                for player in player_list:
                    if current_length + len(player) + 1 > 1024:
                        chunks.append("\n".join(current_chunk))
                        current_chunk = [player]
                        current_length = len(player)
                    else:
                        current_chunk.append(player)
                        current_length += len(player) + 1
                if current_chunk:
                    chunks.append("\n".join(current_chunk))

                for i, chunk in enumerate(chunks):
                    field_name = f"Season {year}" if i == 0 else f"Season {year} (cont.)"
                    embed.add_field(name=field_name, value=chunk, inline=False)
            else:
                embed.add_field(name=f"Season {year}", value=field_value, inline=False)

        return embed

    async def build_compensation_chart_file(self, db):
        """The compensation chart as a color-coded Age x OVR grid image -
        band 1 (best/most valuable free agent) through band 5 (least),
        gray for any (age, ovr) combination outside the chart entirely (no
        compensation applies). Shared by /freeagencyhub's "Compensation
        Table" button (was previously the standalone /compensationtable
        command). Returns None if the chart has no data."""
        cursor = await db.execute(
            """SELECT min_age, max_age, min_ovr, max_ovr, compensation_band
               FROM compensation_chart
               ORDER BY compensation_band, min_age, min_ovr"""
        )
        compensation_data = await cursor.fetchall()
        if not compensation_data:
            return None

        # Build map of (age, ovr) -> band by expanding ranges, and track
        # the real min/max seen so the grid always covers exactly what's
        # in the chart, not a hardcoded guess.
        band_by_age_ovr = {}
        min_age = min_ovr = None
        max_age = max_ovr = None
        for chart_min_age, chart_max_age, chart_min_ovr, chart_max_ovr, band in compensation_data:
            age_end = chart_max_age if chart_max_age is not None else chart_min_age
            ovr_end = chart_max_ovr if chart_max_ovr is not None else chart_min_ovr

            min_age = chart_min_age if min_age is None else min(min_age, chart_min_age)
            max_age = age_end if max_age is None else max(max_age, age_end)
            min_ovr = chart_min_ovr if min_ovr is None else min(min_ovr, chart_min_ovr)
            max_ovr = ovr_end if max_ovr is None else max(max_ovr, ovr_end)

            for age in range(chart_min_age, age_end + 1):
                for ovr in range(chart_min_ovr, ovr_end + 1):
                    band_by_age_ovr[(age, ovr)] = band

        ages = list(range(min_age, max_age + 1))
        ovrs = list(range(min_ovr, max_ovr + 1))

        buffer = render_compensation_chart_image(band_by_age_ovr, ages, ovrs)
        return discord.File(buffer, filename="compensation_chart.png")


class FreeAgencyHubView(discord.ui.View):
    """/freeagencyhub's main menu - always available regardless of free
    agency phase (unlike the old /auctionsmenu, gated to bidding/matching
    only). Shows a phase-specific section (free re-signs / live bidding /
    bid matching, or nothing if no period is active) plus three
    always-available buttons (View Free Agents, View Team Contract Status,
    View Compensation Table) that replace the old standalone
    /viewfreeagents, /contractstatus, /compensationtable commands. Each of
    those three posts its own separate ephemeral message rather than
    editing the hub in place, so the hub itself stays put underneath.

    team_id/team_name may be None (user has no team role) - the
    phase-specific section is simply omitted in that case, since every
    phase action requires a team; the always-available buttons still work
    (Contract Status then requires picking a team explicitly)."""
    def __init__(self, bot, team_id, team_name, season_number, emoji_id=None):
        super().__init__(timeout=300)
        self.bot = bot
        self.team_id = team_id
        self.team_name = team_name
        self.emoji_id = emoji_id
        self.season_number = season_number
        self.period_status = None
        self.max_points = DEFAULT_AUCTION_POINTS
        # Populated by build() - whichever of these apply to the current
        # phase stay at their defaults (None/[]) otherwise.
        self.resign_allowance = None
        self.free_agents = []
        self.bids = []
        self.remaining_points = None
        self.winning_bids = []

    async def build(self, db):
        """(Re)loads phase state and this team's data for it, then
        rebuilds components. Called on open and every time control returns
        to the hub (so it always reflects the current phase, even if it
        changed while a sub-view was open)."""
        self.period_status, self.max_points = await get_fa_period_for_season(db, self.season_number)

        if self.team_id is not None and self.period_status == 'resign':
            cog = self.bot.get_cog('FreeAgencyCommands')
            self.resign_allowance = await cog.calculate_free_resign_allowance(db, self.team_id, self.season_number)
            cursor = await db.execute(
                """SELECT player_id, name, position, age, overall_rating
                   FROM players WHERE team_id = ? AND contract_expiry = ?
                   ORDER BY overall_rating DESC, name""",
                (self.team_id, self.season_number)
            )
            self.free_agents = await cursor.fetchall()

        elif self.team_id is not None and self.period_status == 'bidding':
            cursor = await db.execute(
                """SELECT b.bid_id, b.player_id, b.bid_amount, p.name, p.position, p.age, p.overall_rating,
                          t.team_name, t.emoji_id
                   FROM free_agency_bids b
                   JOIN players p ON b.player_id = p.player_id
                   JOIN teams t ON p.team_id = t.team_id
                   WHERE b.season_number = ? AND b.team_id = ? AND b.status = 'active'
                   ORDER BY b.bid_amount DESC, p.name""",
                (self.season_number, self.team_id)
            )
            self.bids = await cursor.fetchall()
            self.remaining_points = self.max_points - sum(bid[2] for bid in self.bids)

        elif self.team_id is not None and self.period_status == 'matching':
            cursor = await db.execute(
                """SELECT p.player_id, p.name, p.position, p.age, p.overall_rating,
                          t.team_id, t.team_name, t.emoji_id, b.bid_amount
                   FROM players p
                   JOIN free_agency_bids b ON p.player_id = b.player_id
                   JOIN teams t ON b.team_id = t.team_id
                   WHERE p.team_id = ? AND b.season_number = ? AND b.status = 'winning'
                   ORDER BY b.bid_amount DESC""",
                (self.team_id, self.season_number)
            )
            self.winning_bids = await cursor.fetchall()
            cursor = await db.execute(
                """SELECT COALESCE(SUM(b.bid_amount), 0)
                   FROM free_agency_bids b
                   JOIN players p ON b.player_id = p.player_id
                   WHERE b.season_number = ? AND b.team_id = ? AND b.status = 'winning'
                   AND p.team_id != ?""",
                (self.season_number, self.team_id, self.team_id)
            )
            winning_bid_total = (await cursor.fetchone())[0]
            self.remaining_points = self.max_points - winning_bid_total

        elif self.team_id is not None:
            # No free agency period active (or a phase this hub doesn't
            # otherwise show data for) - still worth showing the team's
            # free agents, since "who's out of contract" is useful
            # information year-round, not just during the resign phase.
            cursor = await db.execute(
                """SELECT player_id, name, position, age, overall_rating
                   FROM players WHERE team_id = ? AND contract_expiry = ?
                   ORDER BY overall_rating DESC, name""",
                (self.team_id, self.season_number)
            )
            self.free_agents = await cursor.fetchall()

        self.update_buttons()
        return self.create_embed()

    def update_buttons(self):
        self.clear_items()

        if self.team_id is not None:
            if self.period_status == 'resign':
                btn = discord.ui.Button(
                    label="🔄 Select Free Re-Signs",
                    style=discord.ButtonStyle.primary, row=0
                )
                btn.callback = self.open_resigns_callback
                self.add_item(btn)
            elif self.period_status == 'bidding':
                btn = discord.ui.Button(label="💰 Withdraw Bids", style=discord.ButtonStyle.primary, row=0)
                btn.callback = self.open_bids_callback
                self.add_item(btn)
            elif self.period_status == 'matching':
                btn = discord.ui.Button(
                    label=f"🤝 Choose Bids to Match ({len(self.winning_bids)})",
                    style=discord.ButtonStyle.primary, row=0,
                    disabled=not self.winning_bids
                )
                btn.callback = self.open_matching_callback
                self.add_item(btn)

        fa_btn = discord.ui.Button(label="View Free Agents", style=discord.ButtonStyle.secondary, row=1)
        fa_btn.callback = self.view_free_agents_callback
        self.add_item(fa_btn)

        cs_btn = discord.ui.Button(label="View Team Contract Status", style=discord.ButtonStyle.secondary, row=1)
        cs_btn.callback = self.contract_status_callback
        self.add_item(cs_btn)

        comp_btn = discord.ui.Button(label="View Compensation Table", style=discord.ButtonStyle.secondary, row=1)
        comp_btn.callback = self.compensation_table_callback
        self.add_item(comp_btn)

        refresh_btn = discord.ui.Button(label="🔄 Refresh", style=discord.ButtonStyle.secondary, row=2)
        refresh_btn.callback = self.refresh_callback
        self.add_item(refresh_btn)

    def create_embed(self):
        if self.period_status == 'resign':
            status_line = "**Status:** Free Re-Signs Phase"
        elif self.period_status == 'bidding':
            status_line = "**Status:** Live Bidding Phase"
        elif self.period_status == 'matching':
            status_line = "**Status:** Bid Matching Phase"
        elif self.period_status:
            status_line = f"**Status:** {self.period_status.title()}"
        else:
            status_line = "**Status:** No free agency period active"

        if self.team_id is not None:
            emoji_str = get_team_emoji_str(self.bot, self.emoji_id)
            title = f"{emoji_str}Free Agency Hub"
            description = status_line
        else:
            title = "Free Agency Hub"
            description = f"*You don't have a team role.*\n{status_line}"

        embed = discord.Embed(title=title, description=description, color=discord.Color.blue())

        if self.team_id is None:
            return embed

        if self.period_status == 'resign':
            fa_lines = [f"• {name} ({pos}, {age}, {ovr})" for _, name, pos, age, ovr in self.free_agents]
            embed.add_field(
                name=f"Your Free Agents ({len(self.free_agents)})",
                value="\n".join(fa_lines) if fa_lines else "*None*",
                inline=False
            )
            embed.add_field(name="​", value=f"Free Re-Signs Available: **{self.resign_allowance}**", inline=False)
            embed.set_footer(text="Click 'Select Free Re-Signs' to choose who to re-sign for free.")

        elif self.period_status == 'bidding':
            embed.add_field(
                name="Auction Points",
                value=f"**Remaining:** {self.remaining_points} / {self.max_points}",
                inline=False
            )
            if self.bids:
                bid_lines = [
                    f"• {get_team_emoji_str(self.bot, emoji_id)}**{name}** ({pos}, {age}, {ovr}) - **{amount} pts**"
                    for _, _, amount, name, pos, age, ovr, opp_team, emoji_id in self.bids
                ]
                embed.add_field(name=f"Your Active Bids ({len(self.bids)})", value="\n".join(bid_lines), inline=False)
            else:
                embed.add_field(name="Your Active Bids", value="*No active bids*", inline=False)
            embed.set_footer(text="Use /placebid to bid on opposition players.")

        elif self.period_status == 'matching':
            embed.add_field(
                name="Auction Points",
                value=f"**Remaining:** {self.remaining_points} / {self.max_points}",
                inline=False
            )
            if self.winning_bids:
                bid_lines = [
                    f"• {get_team_emoji_str(self.bot, emoji_id)}**{name}** ({pos}, {age}, {ovr}) - {bidding_team} bid **{amount} pts**"
                    for _, name, pos, age, ovr, _, bidding_team, emoji_id, amount in self.winning_bids
                ]
                embed.add_field(name=f"Winning Bids on Your Players ({len(self.winning_bids)})", value="\n".join(bid_lines), inline=False)
            else:
                embed.add_field(name="Winning Bids on Your Players", value="*None*", inline=False)
            embed.set_footer(text="Click 'Choose Bids to Match' to decide which players to keep.")

        else:
            fa_lines = [f"• {name} ({pos}, {age}, {ovr})" for _, name, pos, age, ovr in self.free_agents]
            embed.add_field(
                name=f"Your Free Agents ({len(self.free_agents)})",
                value="\n".join(fa_lines) if fa_lines else "*None*",
                inline=False
            )

        return embed

    async def open_resigns_callback(self, interaction: discord.Interaction):
        if self.resign_allowance == 0:
            await interaction.response.send_message(
                "❌ Your team has no free re-signs",
                ephemeral=True
            )
            return
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                """SELECT player_id, confirmed FROM free_agency_resigns
                   WHERE season_number = ? AND team_id = ?""",
                (self.season_number, self.team_id)
            )
            existing_selections = await cursor.fetchall()
        selected_players = [p[0] for p in existing_selections]
        is_confirmed = any(p[1] for p in existing_selections) if existing_selections else False

        view = FreeResignSelectionView(
            self.bot, self.team_id, self.resign_allowance,
            self.free_agents, selected_players, is_confirmed, self.season_number, hub=self
        )
        embed = view.create_embed()
        await interaction.response.edit_message(embed=embed, view=view)

    async def open_bids_callback(self, interaction: discord.Interaction):
        view = AuctionsMenuView(
            self.bot, self.team_id, self.team_name, self.bids, self.remaining_points,
            self.max_points, self.season_number, self.period_status, hub=self, emoji_id=self.emoji_id
        )
        embed = view.create_embed()
        await interaction.response.edit_message(embed=embed, view=view)

    async def open_matching_callback(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            # free_agency_results (not self.winning_bids, which comes from
            # free_agency_bids and carries no confirmation info) is the
            # source of truth for whether this team already confirmed its
            # matches this matching period - start_matching_period inserts
            # one row per player here with matched=0 and confirmed_at NULL,
            # then MatchingView.confirm_callback sets both once the team
            # locks in their decisions. If already confirmed, show the
            # confirmed summary instead of the live decision UI, exactly
            # like re-opening a submitted form should.
            #
            # Scoped to self.winning_bids' player_ids only - free_agency_results
            # also has a row for every free agent who got NO bid at all
            # (start_matching_period's "will be auto re-signed" branch),
            # and those rows' confirmed_at never gets touched by
            # confirm_callback (which only ever updates the bid-on players
            # in self.matches). Pulling in every result row for the team
            # both double-counted "Let Go" against players that were never
            # actually up for matching, and made has_confirmed depend on
            # which row happened to come back first.
            winning_bid_player_ids = [row[0] for row in self.winning_bids]
            result_rows = []
            if winning_bid_player_ids:
                placeholders = ",".join("?" * len(winning_bid_player_ids))
                cursor = await db.execute(
                    f"""SELECT r.player_id, r.matched, r.confirmed_at
                        FROM free_agency_results r
                        WHERE r.season_number = ? AND r.original_team_id = ?
                        AND r.player_id IN ({placeholders})""",
                    (self.season_number, self.team_id, *winning_bid_player_ids)
                )
                result_rows = await cursor.fetchall()
            has_confirmed = bool(result_rows) and all(row[2] is not None for row in result_rows)

            matching_view = MatchingView(
                self.bot, self.team_id, self.team_name, self.winning_bids,
                self.season_number, self.remaining_points, hub=self, emoji_id=self.emoji_id
            )

            if has_confirmed:
                matches = {player_id: bool(matched) for player_id, matched, _ in result_rows}
                matching_view.matches = matches
                matching_view.confirmed = True
                matching_view.update_buttons()

                total_cost = 0
                for player_id, _, _, age, _, _, _, _, bid in self.winning_bids:
                    if matches.get(player_id, False):
                        total_cost += round(bid * 0.8) if age <= 25 else bid

                matched_count = sum(1 for m in matches.values() if m)
                let_go_count = len(matches) - matched_count

                emoji_str = get_team_emoji_str(self.bot, self.emoji_id)
                embed = discord.Embed(
                    title=f"{emoji_str}Matches Confirmed",
                    description="Your matching decisions have been recorded.",
                    color=discord.Color.green()
                )
                embed.add_field(
                    name="Summary",
                    value=f"**Matched:** {matched_count} player{'s' if matched_count != 1 else ''} ({total_cost} pts)\n"
                          f"**Let Go:** {let_go_count} player{'s' if let_go_count != 1 else ''}",
                    inline=False
                )
                embed.set_footer(text="Click 'Edit Matches' to make changes • Waiting for admin to end matching period...")
            else:
                embed = await matching_view.create_embed()

        await interaction.response.edit_message(embed=embed, view=matching_view)

    async def view_free_agents_callback(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            cog = self.bot.get_cog('FreeAgencyCommands')
            teams_dict, total_count = await cog.fetch_free_agents_grouped(db, self.season_number)
            all_teams = await fetch_teams_for_dropdown(db)
        view = FreeAgentsView(self.bot, teams_dict, self.season_number, total_count, all_teams=all_teams)
        embed = view.create_embed()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def contract_status_callback(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            all_teams = await fetch_teams_for_dropdown(db)
            if self.team_id is not None:
                cog = self.bot.get_cog('FreeAgencyCommands')
                embed = await cog.build_contract_status_embed(db, self.team_id, self.team_name, self.emoji_id)
            else:
                embed = None
        view = _ContractStatusView(self.bot, self.season_number, all_teams=all_teams,
                                    team_id=self.team_id, team_name=self.team_name)
        if embed is None:
            embed = view.no_team_embed()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def compensation_table_callback(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            cog = self.bot.get_cog('FreeAgencyCommands')
            file = await cog.build_compensation_chart_file(db)
        if file is None:
            await interaction.response.send_message(
                "❌ No compensation chart data found! Use `/migratedb` to initialize.", ephemeral=True
            )
            return
        await interaction.response.send_message(file=file, ephemeral=True)

    async def refresh_callback(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            embed = await self.build(db)
        await interaction.response.edit_message(embed=embed, view=self)


class _ContractStatusView(discord.ui.View):
    """Team-filtered Contract Status. Posted as its own standalone
    (ephemeral) message from /freeagencyhub's "View Team Contract Status"
    button - the hub message underneath is left untouched. Defaults to the
    user's own team if they have one; anyone can pick another team from
    the dropdown."""
    def __init__(self, bot, season_number, all_teams, team_id=None, team_name=None):
        super().__init__(timeout=180)
        self.bot = bot
        self.season_number = season_number
        self.all_teams = all_teams
        self.team_id = team_id
        self.team_name = team_name
        self.update_components()

    def update_components(self):
        self.clear_items()
        self.add_item(_ContractStatusTeamSelect(self))

    def no_team_embed(self):
        return discord.Embed(
            title="📋 Contract Status",
            description="You don't have a team role - pick a team from the dropdown to view its contracts.",
            color=discord.Color.blue()
        )


class _ContractStatusTeamSelect(discord.ui.Select):
    def __init__(self, parent_view):
        self.parent_view = parent_view
        options = build_team_options(parent_view.bot, parent_view.all_teams, selected=parent_view.team_id)
        super().__init__(placeholder="Select a team...", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        team_id = int(self.values[0])
        team_row = next((t for t in self.parent_view.all_teams if t[0] == team_id), None)
        team_name = team_row[1] if team_row else "Unknown"
        emoji_id = team_row[2] if team_row and len(team_row) > 2 else None
        self.parent_view.team_id = team_id
        self.parent_view.team_name = team_name
        self.parent_view.update_components()

        async with aiosqlite.connect(DB_PATH) as db:
            cog = self.parent_view.bot.get_cog('FreeAgencyCommands')
            embed = await cog.build_contract_status_embed(db, team_id, team_name, emoji_id)
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class FreeAgentsView(discord.ui.View):
    """Paginated view for free agents list, with a team filter dropdown.
    Posted as its own standalone (ephemeral) message from
    /freeagencyhub's "View Free Agents" button - the hub message
    underneath is left untouched, so there's no "Back to Hub" to wire up."""
    def __init__(self, bot, teams_dict, season_number, total_fa_count, team_filter_id=None, all_teams=None):
        super().__init__(timeout=180)
        self.bot = bot
        self.teams_dict = teams_dict
        self.season_number = season_number
        self.total_fa_count = total_fa_count
        self.teams_list = sorted(teams_dict.keys())
        self.current_page = 0
        self.teams_per_page = 5
        self.team_filter_id = team_filter_id
        # (team_id, team_name, emoji_id) rows for the filter dropdown -
        # fetched once by whoever opens this view (fetch_teams_for_dropdown
        # is async, so it can't be called from inside a Select's own
        # synchronous __init__).
        self.all_teams = all_teams or []

        self.update_buttons()

    def update_buttons(self):
        """Update navigation buttons"""
        self.clear_items()

        self.add_item(_FreeAgentsTeamFilterSelect(self))

        total_pages = max(1, (len(self.teams_list) + self.teams_per_page - 1) // self.teams_per_page)

        # Previous button
        prev_button = discord.ui.Button(
            label="◀ Previous",
            style=discord.ButtonStyle.secondary,
            disabled=self.current_page == 0,
            custom_id="prev",
            row=1
        )
        prev_button.callback = self.prev_callback
        self.add_item(prev_button)

        # Page indicator
        page_button = discord.ui.Button(
            label=f"Page {self.current_page + 1}/{total_pages}",
            style=discord.ButtonStyle.secondary,
            disabled=True,
            custom_id="page",
            row=1
        )
        self.add_item(page_button)

        # Next button
        next_button = discord.ui.Button(
            label="Next ▶",
            style=discord.ButtonStyle.secondary,
            disabled=self.current_page >= total_pages - 1,
            custom_id="next",
            row=1
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
            emoji_str = get_team_emoji_str(self.bot, team_data['emoji_id'])

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


class _FreeAgentsTeamFilterSelect(discord.ui.Select):
    """Filters FreeAgentsView to one team, or clears back to all teams -
    the dropdown-based replacement for the old /viewfreeagents team:
    autocomplete parameter."""
    def __init__(self, parent_view):
        self.parent_view = parent_view
        options = build_team_options(
            parent_view.bot, parent_view.all_teams,
            selected=parent_view.team_filter_id,
            extra_options=[discord.SelectOption(
                label="All teams", value="all",
                default=parent_view.team_filter_id is None,
            )],
        )
        super().__init__(placeholder="Filter by team...", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        value = self.values[0]
        team_filter_id = None if value == "all" else int(value)

        async with aiosqlite.connect(DB_PATH) as db:
            cog = self.parent_view.bot.get_cog('FreeAgencyCommands')
            teams_dict, total_count = await cog.fetch_free_agents_grouped(
                db, self.parent_view.season_number, team_filter_id
            )

        self.parent_view.teams_dict = teams_dict
        self.parent_view.teams_list = sorted(teams_dict.keys())
        self.parent_view.total_fa_count = total_count
        self.parent_view.team_filter_id = team_filter_id
        self.parent_view.current_page = 0
        self.parent_view.update_buttons()
        embed = self.parent_view.create_embed()
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class FreeAgencyNotificationView(discord.ui.View):
    """Persistent notification view with a single "Open Free Agency Hub"
    button - sent to each team's own channel for the resign and matching
    phase-start notifications (one instance per team, team_id fixed at
    construction). The bidding-phase notification is posted once to the
    shared auctions channel instead and carries no button at all (see
    send_bidding_notifications) - there's no single "the" team for a
    persistent view's button to resolve to on a shared message. Always
    routes into /freeagencyhub rather than jumping straight into a
    phase-specific sub-view, so there's one consistent, always-navigable
    entry point regardless of which notification was clicked."""
    def __init__(self, bot, season_number, team_id):
        super().__init__(timeout=None)  # Persistent view
        self.bot = bot
        self.season_number = season_number
        self.team_id = team_id

    @discord.ui.button(label="Open Free Agency Hub", style=discord.ButtonStyle.primary, custom_id="fa_notification_open_hub")
    async def open_hub(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                current_season = await get_current_season(db)

                # Resolve the team from the CHANNEL the button was clicked
                # in, not self.team_id - every team's notification view
                # shares the same custom_id ("fa_notification_open_hub"),
                # and bot.add_view() without a message_id only keeps the
                # LAST-registered instance's dispatch after a restart, so
                # self.team_id can silently belong to a different team than
                # the one this specific message was posted for. The
                # notification channel is fixed per-team (teams.channel_id),
                # so it's an unambiguous way to find the right team
                # regardless of which instance actually handled the click.
                cursor = await db.execute(
                    "SELECT team_id, team_name, emoji_id FROM teams WHERE channel_id = ?",
                    (str(interaction.channel_id),)
                )
                team_result = await cursor.fetchone()
                if not team_result:
                    # Fallback for a channel that isn't a registered team
                    # channel (shouldn't normally happen for this button).
                    cursor = await db.execute(
                        "SELECT team_id, team_name, emoji_id FROM teams WHERE team_id = ?", (self.team_id,)
                    )
                    team_result = await cursor.fetchone()
                if not team_result:
                    await interaction.response.send_message("❌ Team not found!", ephemeral=True)
                    return
                team_id, team_name, emoji_id = team_result

                hub = FreeAgencyHubView(self.bot, team_id, team_name, current_season, emoji_id=emoji_id)
                embed = await hub.build(db)
                await interaction.response.send_message(embed=embed, view=hub, ephemeral=True)

        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)

    @staticmethod
    def notification_title(bot, emoji_id, phase_label):
        """"{emoji}Free Agency - {Phase Name}" - the shared header format
        for all three phase notifications, no team name (the channel
        itself already belongs to one team)."""
        emoji_str = get_team_emoji_str(bot, emoji_id)
        return f"{emoji_str}Free Agency - {phase_label}"

    @staticmethod
    async def create_matching_embed(bot, season_number, team_id, team_name, emoji_id, player_bids, max_points,
                                     band_by_player_id=None, placed_bids=None):
        """The matching-phase notification embed. band_by_player_id maps
        player_id -> compensation band (or None) - now sent to EVERY team
        with a free agent this season, not just ones who got a bid, so
        player_bids may be empty (shown as "No bids on your players").
        placed_bids (optional) is this team's OWN bids placed on
        opposition free agents this period - (player_id, name, pos, age,
        ovr, original_team_id, original_team_name, original_emoji_id,
        bid_amount, status) rows with status 'winning' or 'outbid' -
        shown as a separate "Your Bids" section so a team can see which
        of their own bids succeeded, not just what's happening to their
        own free agents."""
        band_by_player_id = band_by_player_id or {}
        embed = discord.Embed(
            title=FreeAgencyNotificationView.notification_title(bot, emoji_id, "Bid Matching Phase"),
            description=("Your free agents have received bids. Choose which to match:"
                          if player_bids else "None of your free agents received a bid."),
            color=discord.Color.orange()
        )

        # Section order: bids received on your own players, then your
        # remaining points, then bids you placed elsewhere. Remaining
        # Points is appended into the SAME field as "Bids on Your
        # Players" (one more line, not a separate embed field) so there's
        # no gap between them - Discord spaces separate fields apart
        # regardless of content, so merging is the only way to close it.
        # Falls back to its own field when there's no bids-received
        # section to attach to.
        remaining_points_line = f"Remaining Points: **{max_points}**"

        divider = "⎯" * 22

        if player_bids:
            player_lines = []
            for player_id, name, pos, age, ovr, winning_team_id, bidding_team, bidding_emoji_id, bid in player_bids:
                bidding_emoji_str = get_team_emoji_str(bot, bidding_emoji_id)

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

                band = band_by_player_id.get(player_id)
                band_text = f"Band {band}" if band else "No comp"

                player_lines.append(
                    f"**{name}**{rfa_label} ({pos}, {age}, {ovr}) - {band_text}\n"
                    f"    └ {bidding_emoji_str}bid {cost_display}"
                )
            player_bids_body = "\n\n".join(player_lines)
        else:
            player_bids_body = "*None*"

        embed.add_field(
            name="Bids on Your Players",
            value=f"{player_bids_body}\n\n{divider}\n{remaining_points_line}\n{divider}",
            inline=False
        )

        if placed_bids:
            bid_lines = []
            any_winning = False
            for player_id, name, pos, age, ovr, original_team_id, original_team_name, original_emoji_id, bid_amount, status in placed_bids:
                original_emoji_str = get_team_emoji_str(bot, original_emoji_id)
                if status == 'winning':
                    status_text = f"✅ **WON** - **{bid_amount} pts**"
                    any_winning = True
                else:
                    status_text = f"❌ **LOST** ({bid_amount} pts refunded)"
                bid_lines.append(
                    f"{original_emoji_str}**{name}** ({pos}, {age}, {ovr}) - {status_text}"
                )
            your_bids_body = "\n".join(bid_lines)
        else:
            your_bids_body = "*None*"
            any_winning = False

        embed.add_field(
            name="Your Bids",
            value=your_bids_body,
            inline=False
        )
        if any_winning:
            embed.add_field(name="​", value="*Winning bids pending matches", inline=False)

        embed.set_footer(text="Use /freeagencyhub to choose which bids to match.")
        return embed

    @staticmethod
    def create_resign_embed(bot, emoji_id, allowance, free_agents, band_by_player_id):
        """The free-resign-phase notification embed. free_agents rows are
        (player_id, name, pos, age, ovr); band_by_player_id maps player_id
        -> compensation band (or None)."""
        embed = discord.Embed(
            title=FreeAgencyNotificationView.notification_title(bot, emoji_id, "Free Re-Signs Phase"),
            description=f"You have **{allowance}** free re-sign{'s' if allowance != 1 else ''} available.",
            color=discord.Color.blue()
        )

        fa_list = []
        for player_id, name, pos, age, ovr in free_agents:
            band = band_by_player_id.get(player_id)
            band_text = f"Band {band}" if band else "No comp"
            fa_list.append(f"**{name}** ({pos}, {age}, {ovr}) - {band_text}")

        embed.add_field(
            name=f"Your Free Agents ({len(free_agents)})",
            value="\n".join(fa_list) if fa_list else "None",
            inline=False
        )
        embed.set_footer(text="Use /freeagencyhub to select which players to re-sign for free.")
        return embed

    @staticmethod
    def create_bidding_embed(season_number):
        """The live-bidding-phase notification - posted ONCE to the shared
        auctions channel (not per-team channel like resign/matching),
        since bidding is inherently a league-wide event, not a per-team
        one - every club can see every other club's free agents and place
        bids regardless of their own roster. No team emoji/free-agents
        list here (there's no single "your team" for a shared message);
        FreeAgencyNotificationView's button resolves the clicking user's
        own team instead."""
        return discord.Embed(
            title=f"Season {season_number} Free Agency Auctions are LIVE",
            description=(
                "Use /placebid to bid on opposition free agents.\n\n"
                "Use /freeagencyhub to view and withdraw your bids."
            ),
            color=discord.Color.gold()
        )

class MatchingView(discord.ui.View):
    """Interactive UI for teams to match winning bids on their players -
    reached from FreeAgencyHubView's "Choose Bids to Match" button."""
    def __init__(self, bot, team_id, team_name, player_bids, season_number, max_points=300, hub=None, emoji_id=None):
        super().__init__(timeout=180)  # 3 minute timeout for ephemeral view
        self.bot = bot
        self.team_id = team_id
        self.team_name = team_name
        self.player_bids = player_bids  # List of (player_id, name, pos, age, ovr, winning_team_id, team_name, emoji_id, bid)
        self.season_number = season_number
        self.matches = {}  # player_id -> bool (True = match, False = don't match)
        self.confirmed = False  # Track if matches have been confirmed
        self.hub = hub
        self.emoji_id = emoji_id  # this team's OWN emoji, for embed titles - distinct
        # from the per-player emoji_id in player_bids, which is the bidding team's

        # Store max points (remaining after winning bids on other teams' players)
        self.max_points = max_points

        # Add toggle buttons for each player
        for player_id, name, pos, age, ovr, winning_team_id, bidding_team, emoji_id, bid in player_bids:
            self.matches[player_id] = False  # Default to not matching

        self.update_buttons()

    def update_buttons(self):
        """Update all buttons based on current state"""
        self.clear_items()

        # If confirmed, only show "Edit Matches" (+ Back to Hub) button
        if self.confirmed:
            edit_button = discord.ui.Button(
                label="Edit Matches",
                style=discord.ButtonStyle.secondary,
                custom_id="edit_matches",
                row=0
            )
            edit_button.callback = self.edit_matches_callback
            self.add_item(edit_button)
            if self.hub is not None:
                back_button = discord.ui.Button(label="← Back to Hub", style=discord.ButtonStyle.secondary, row=0)
                back_button.callback = self.back_to_hub_callback
                self.add_item(back_button)
            return

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
                custom_id=f"match_select_{self.team_id}",
                row=0
            )
            select.callback = self.select_callback
            self.add_item(select)

        # Add confirm button
        confirm_button = discord.ui.Button(
            label="Confirm Matches",
            style=discord.ButtonStyle.primary,
            custom_id=f"match_confirm_{self.team_id}",
            row=1
        )
        confirm_button.callback = self.confirm_callback
        self.add_item(confirm_button)

        if self.hub is not None:
            back_button = discord.ui.Button(label="← Back to Hub", style=discord.ButtonStyle.secondary, row=1)
            back_button.callback = self.back_to_hub_callback
            self.add_item(back_button)

    async def back_to_hub_callback(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            embed = await self.hub.build(db)
        await interaction.response.edit_message(embed=embed, view=self.hub)

    async def create_embed(self):
        """Create the embed showing current matching status"""
        emoji_str = get_team_emoji_str(self.bot, self.emoji_id)
        embed = discord.Embed(
            title=f"{emoji_str}Free Agency Matching",
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

        # Get compensation bands for all players (load chart once, match in Python
        # instead of one query per player)
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                """SELECT min_age, max_age, min_ovr, max_ovr, compensation_band
                   FROM compensation_chart
                   ORDER BY compensation_band ASC"""
            )
            chart_rows = await cursor.fetchall()

        def band_for(age, ovr):
            for min_age, max_age, min_ovr, max_ovr, band in chart_rows:
                if min_age <= age <= (max_age if max_age is not None else min_age) \
                        and min_ovr <= ovr <= (max_ovr if max_ovr is not None else min_ovr):
                    return band
            return None

        compensation_bands = {}
        for player_id, name, pos, age, ovr, winning_team_id, bidding_team, emoji_id, bid in self.player_bids:
            compensation_bands[player_id] = band_for(age, ovr)

        # List each player with current match status
        player_lines = []
        for player_id, name, pos, age, ovr, winning_team_id, bidding_team, emoji_id, bid in self.player_bids:
            # Get emoji
            emoji_str = get_team_emoji_str(self.bot, emoji_id)

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
                f"    └ {emoji_str}bid {cost_display}{comp_label}"
            )

        embed.add_field(
            name=f"Players ({len(self.player_bids)})",
            value="\n\n".join(player_lines),
            inline=False
        )

        return embed

    async def select_callback(self, interaction: discord.Interaction):
        """Handle player selection from dropdown"""
        # Check if period is still active
        async with aiosqlite.connect(DB_PATH) as db:
            status, _ = await get_fa_period_for_season(db, self.season_number)
            if status != 'matching':
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
                status, _ = await get_fa_period_for_season(db, self.season_number)
                if status != 'matching':
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
                           WHERE season_number = ? AND player_id = ?""",
                        (1 if self.matches[player_id] else 0, self.season_number, player_id)
                    )
                await db.commit()

                # Log to bot logs channel (best-effort - must not mask the successful confirmation above)
                try:
                    log_channel = await self.bot.get_cog('FreeAgencyCommands').get_bot_logs_channel(db)
                    if log_channel:
                        # Get team info
                        cursor = await db.execute("SELECT team_name, emoji_id FROM teams WHERE team_id = ?", (self.team_id,))
                        team_data = await cursor.fetchone()
                        team_name = team_data[0] if team_data else "Unknown Team"
                        emoji_id = team_data[1] if team_data and team_data[1] else None

                        emoji_str = get_team_emoji_str(self.bot, emoji_id)

                        matched_count = sum(1 for m in self.matches.values() if m)
                        let_go_count = len(self.matches) - matched_count

                        await log_channel.send(
                            f"✅ {emoji_str}**{team_name}** confirmed matches: "
                            f"{matched_count} matched ({total_cost}pts), {let_go_count} let go ({interaction.user.mention})"
                        )
                except Exception as e:
                    print(f"Failed to log match confirmation to bot logs channel: {e}")

            # Mark as confirmed and update buttons
            self.confirmed = True
            self.update_buttons()

            confirmed_emoji_str = get_team_emoji_str(self.bot, self.emoji_id)
            embed = discord.Embed(
                title=f"{confirmed_emoji_str}Matches Confirmed",
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

        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)
            return

        # Sent outside the try block: the match confirmation already succeeded above,
        # so a failure here must not be reported as the confirmation itself failing.
        await interaction.response.edit_message(embed=embed, view=self)

    async def edit_matches_callback(self, interaction: discord.Interaction):
        """Allow editing matches after confirmation"""
        # Check if period is still active
        async with aiosqlite.connect(DB_PATH) as db:
            status, _ = await get_fa_period_for_season(db, self.season_number)
            if status != 'matching':
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
    """The "Manage Bids" sub-view, reached from FreeAgencyHubView (only
    ever shown during the 'bidding' phase now - the hub itself handles
    displaying bids/points and decides when to open this, so the
    resign/matching launch buttons this view used to carry (back when it
    doubled as its own phase-aware home screen under the old /auctionsmenu)
    are gone; a Back to Hub button replaces them)."""
    def __init__(self, bot, team_id, team_name, bids, remaining_points, max_points, season_number, period_status,
                 hub=None, emoji_id=None):
        super().__init__(timeout=180)
        self.bot = bot
        self.team_id = team_id
        self.team_name = team_name
        self.bids = bids
        self.remaining_points = remaining_points
        self.max_points = max_points
        self.season_number = season_number
        self.period_status = period_status
        self.hub = hub
        self.emoji_id = emoji_id
        self.selected_bid_ids = []  # Store selected bids for withdrawal

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
                        value=str(bid_id),
                        # Without this, rebuilding the dropdown after a
                        # selection (see select_bids_callback) resets every
                        # option back to unselected, so the picked names
                        # visibly vanish from the dropdown the moment it's
                        # re-rendered - mark whichever bids are already in
                        # selected_bid_ids so they stay showing as chosen.
                        default=(bid_id in self.selected_bid_ids)
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
            select.callback = self.select_bids_callback
            self.add_item(select)

            # Add withdraw button (disabled if nothing selected)
            withdraw_button = discord.ui.Button(
                label="Withdraw Selected Bids",
                style=discord.ButtonStyle.danger,
                custom_id="withdraw_confirm",
                disabled=len(self.selected_bid_ids) == 0,
                row=1
            )
            withdraw_button.callback = self.withdraw_callback
            self.add_item(withdraw_button)

        # Add refresh + back-to-hub buttons on the next free row
        button_row = 2 if self.period_status == 'bidding' and self.bids else 1
        refresh_button = discord.ui.Button(
            label="Refresh",
            style=discord.ButtonStyle.secondary,
            custom_id="refresh",
            row=button_row
        )
        refresh_button.callback = self.refresh_callback
        self.add_item(refresh_button)

        if self.hub is not None:
            back_button = discord.ui.Button(label="← Back to Hub", style=discord.ButtonStyle.secondary, row=button_row)
            back_button.callback = self.back_to_hub_callback
            self.add_item(back_button)

    async def back_to_hub_callback(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            embed = await self.hub.build(db)
        await interaction.response.edit_message(embed=embed, view=self.hub)

    def create_embed(self):
        emoji_str = get_team_emoji_str(self.bot, self.emoji_id)
        embed = discord.Embed(
            title=f"{emoji_str}Free Agency Auction",
            description=f"**Status:** {self.period_status.title()}",
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
                emoji_str = get_team_emoji_str(self.bot, emoji_id)

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

        embed.set_footer(text="Select bids from dropdown, then click 'Withdraw Selected Bids' • Click Refresh to update")
        return embed

    async def select_bids_callback(self, interaction: discord.Interaction):
        """Handle bid selection from dropdown"""
        try:
            self.selected_bid_ids = [int(value) for value in interaction.data['values']]

            # Update the view to enable the withdraw button
            self.update_buttons()
            embed = self.create_embed()
            await interaction.response.edit_message(embed=embed, view=self)

        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)

    async def withdraw_callback(self, interaction: discord.Interaction):
        """Handle withdrawing selected bids after confirmation"""
        try:
            if not self.selected_bid_ids:
                await interaction.response.send_message("❌ No bids selected!", ephemeral=True)
                return

            selected_bid_ids = self.selected_bid_ids

            async with aiosqlite.connect(DB_PATH) as db:
                # Delete the selected bids
                for bid_id in selected_bid_ids:
                    await db.execute(
                        "DELETE FROM free_agency_bids WHERE bid_id = ?",
                        (bid_id,)
                    )
                await db.commit()

                # Log to bot logs channel (best-effort - must not mask the successful withdrawal above)
                try:
                    log_channel = await self.bot.get_cog('FreeAgencyCommands').get_bot_logs_channel(db)
                    if log_channel:
                        # Get team info
                        cursor = await db.execute("SELECT team_name, emoji_id FROM teams WHERE team_id = ?", (self.team_id,))
                        team_data = await cursor.fetchone()
                        team_name = team_data[0] if team_data else "Unknown Team"
                        emoji_id = team_data[1] if team_data and team_data[1] else None

                        emoji_str = get_team_emoji_str(self.bot, emoji_id)

                        # Get withdrawn bids for logging
                        withdrawn_bids_temp = [b for b in self.bids if b[0] in selected_bid_ids]
                        player_names_log = ", ".join(b[3] for b in withdrawn_bids_temp)

                        await log_channel.send(f"🚫 {emoji_str}**{team_name}** withdrew bid(s): {player_names_log} ({interaction.user.mention})")
                except Exception as e:
                    print(f"Failed to log bid withdrawal to bot logs channel: {e}")

            # Update view
            withdrawn_bids = [b for b in self.bids if b[0] in selected_bid_ids]
            refund_amount = sum(b[2] for b in withdrawn_bids)
            self.bids = [b for b in self.bids if b[0] not in selected_bid_ids]
            self.remaining_points += refund_amount
            self.selected_bid_ids = []  # Clear selection after withdrawal

            self.update_buttons()
            embed = self.create_embed()
            player_names = ", ".join(b[3] for b in withdrawn_bids)

        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)
            return

        # Sent outside the try block: the withdrawal already succeeded above,
        # so a failure here must not be reported as the withdrawal itself failing.
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(
            f"✅ Withdrawn {len(selected_bid_ids)} bid(s): {player_names} ({refund_amount} points refunded)",
            ephemeral=True
        )

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
                       WHERE b.season_number = ? AND b.team_id = ? AND b.status = 'active'
                       ORDER BY b.bid_amount DESC, p.name""",
                    (self.season_number, self.team_id)
                )
                self.bids = await cursor.fetchall()

                # Recalculate points
                total_spent = sum(bid[2] for bid in self.bids)
                self.remaining_points = self.max_points - total_spent

                # Re-fetch period status
                status, _ = await get_fa_period_for_season(db, self.season_number)
                if status:
                    self.period_status = status

            # Recreate view with new buttons
            new_view = AuctionsMenuView(
                self.bot, self.team_id, self.team_name,
                self.bids, self.remaining_points, self.max_points, self.season_number, self.period_status,
                hub=self.hub, emoji_id=self.emoji_id
            )
            embed = new_view.create_embed()
            await interaction.response.edit_message(embed=embed, view=new_view)

        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)


class FreeResignSelectionView(discord.ui.View):
    """Interactive UI for teams to select which players to re-sign for free -
    reached from FreeAgencyHubView's "Select Free Re-Signs" button."""
    def __init__(self, bot, team_id, allowance, free_agents, selected_players, is_confirmed, season_number, hub=None):
        super().__init__(timeout=180)
        self.bot = bot
        self.team_id = team_id
        self.allowance = allowance
        self.free_agents = free_agents
        self.selected_players = selected_players
        self.is_confirmed = is_confirmed
        self.season_number = season_number
        self.hub = hub

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

        if self.hub is not None:
            back_button = discord.ui.Button(label="← Back to Hub", style=discord.ButtonStyle.secondary)
            back_button.callback = self.back_to_hub_callback
            self.add_item(back_button)

    async def back_to_hub_callback(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            embed = await self.hub.build(db)
        await interaction.response.edit_message(embed=embed, view=self.hub)

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
                    "DELETE FROM free_agency_resigns WHERE season_number = ? AND team_id = ?",
                    (self.season_number, self.team_id)
                )

                # Insert new selections
                for player_id in selected_ids:
                    await db.execute(
                        """INSERT INTO free_agency_resigns (season_number, team_id, player_id, confirmed)
                           VALUES (?, ?, ?, 0)""",
                        (self.season_number, self.team_id, player_id)
                    )

                await db.commit()

            # Recreate view with updated selections
            new_view = FreeResignSelectionView(
                self.bot, self.team_id, self.allowance,
                self.free_agents, self.selected_players, False, self.season_number, hub=self.hub
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
                       WHERE season_number = ? AND team_id = ?""",
                    (self.season_number, self.team_id)
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

                    emoji_str = get_team_emoji_str(self.bot, emoji_id)

                    if self.selected_players:
                        player_names = []
                        for player_id in self.selected_players:
                            cursor = await db.execute("SELECT name FROM players WHERE player_id = ?", (player_id,))
                            player = await cursor.fetchone()
                            if player:
                                player_names.append(player[0])

                        players_str = ", ".join(player_names)
                        await log_channel.send(f"✅ {emoji_str}**{team_name}** confirmed free re-signs: {players_str} ({interaction.user.mention})")
                    else:
                        await log_channel.send(f"✅ {emoji_str}**{team_name}** confirmed 0 free re-signs ({interaction.user.mention})")

            self.is_confirmed = True

            # Recreate view with confirmed state
            new_view = FreeResignSelectionView(
                self.bot, self.team_id, self.allowance,
                self.free_agents, self.selected_players, True, self.season_number, hub=self.hub
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
                       WHERE season_number = ? AND team_id = ?""",
                    (self.season_number, self.team_id)
                )
                await db.commit()

            self.is_confirmed = False

            # Recreate view in unconfirmed state
            new_view = FreeResignSelectionView(
                self.bot, self.team_id, self.allowance,
                self.free_agents, self.selected_players, False, self.season_number, hub=self.hub
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

        return embed


async def setup(bot):
    await bot.add_cog(FreeAgencyCommands(bot))
