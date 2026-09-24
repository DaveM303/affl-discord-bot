import re
import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from config import DB_PATH
from utils import get_team_emoji_str, build_team_options, fetch_teams_for_dropdown

class PlayerCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def get_team_emoji(self, emoji_id: str) -> str:
        """Get server emoji by ID, return empty string if not found."""
        return get_team_emoji_str(self.bot, emoji_id, trailing_space=False)

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
                    """SELECT p.player_id, p.name, p.position, p.overall_rating, p.age, t.team_name, t.emoji_id
                       FROM players p
                       LEFT JOIN teams t ON p.team_id = t.team_id
                       WHERE p.name LIKE ? AND p.team_id IS NOT NULL
                       ORDER BY p.overall_rating DESC""",
                    (f"%{search_term}%",)
                )
                results = await cursor.fetchall()
                all_players.extend(results)

            # Remove duplicates by player_id (not name, to allow duplicate names)
            seen = set()
            unique_players = []
            for player in all_players:
                player_id = player[0]
                if player_id not in seen:
                    seen.add(player_id)
                    unique_players.append(player)

            if not unique_players:
                search_display = "', '".join(search_terms)
                await interaction.response.send_message(
                    f"No players found matching '{search_display}'",
                    ephemeral=True
                )
                return

            search_display = "', '".join(search_terms)

            # A single hit is unambiguous, so skip the list and open that
            # player's profile straight away. Several hits need the list (and
            # its dropdown) to pick from.
            if len(unique_players) == 1:
                season_id, season_number = await resolve_profile_season(db)
                data = await fetch_player_profile_data(db, unique_players[0][0], season_id)
                view = PlayerProfileView(self, data, season_number)
                await interaction.response.send_message(
                    embed=view.create_embed(), view=view, ephemeral=True
                )
                return

        view = PlayerSearchResultsView(self, unique_players, search_display)
        await interaction.response.send_message(
            embed=view.create_embed(), view=view, ephemeral=True
        )
        view.message = await interaction.original_response()

    async def team_name_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for team names"""
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT team_name FROM teams WHERE team_name != 'Draft Pool' ORDER BY team_name")
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
    @app_commands.describe(
        team_name="Name of the team (leave empty for your team)",
        sort_by="Sort by (default: Position)"
    )
    @app_commands.autocomplete(team_name=team_name_autocomplete)
    @app_commands.choices(sort_by=[
        app_commands.Choice(name="OVR (High to Low)", value="ovr_desc"),
        app_commands.Choice(name="OVR (Low to High)", value="ovr_asc"),
        app_commands.Choice(name="Age (Oldest to Youngest)", value="age_desc"),
        app_commands.Choice(name="Age (Youngest to Oldest)", value="age_asc"),
        app_commands.Choice(name="Position", value="position"),
    ])
    async def roster(self, interaction: discord.Interaction, team_name: str = None, sort_by: str = "position"):
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
            
            # Build ORDER BY clause
            if sort_by == "ovr_desc":
                order_clause = "overall_rating DESC, age ASC"
            elif sort_by == "ovr_asc":
                order_clause = "overall_rating ASC, age ASC"
            elif sort_by == "age_desc":
                order_clause = "age DESC, overall_rating DESC"
            elif sort_by == "age_asc":
                order_clause = "age ASC, overall_rating DESC"
            elif sort_by == "position":
                # Use CASE to order by POSITION_DISPLAY_ORDER
                from positions import POSITION_DISPLAY_ORDER
                case_parts = []
                for idx, pos in enumerate(POSITION_DISPLAY_ORDER):
                    case_parts.append(f"WHEN position = '{pos}' THEN {idx}")
                case_statement = "CASE " + " ".join(case_parts) + " ELSE 999 END"
                order_clause = f"{case_statement}, overall_rating DESC"
            else:
                order_clause = "overall_rating DESC, age ASC"

            # Get roster (exclude Draft Pool players)
            cursor = await db.execute(
                f"""SELECT name, position, overall_rating, age
                   FROM players
                   WHERE team_id = ?
                   AND team_id != (SELECT team_id FROM teams WHERE team_name = 'Draft Pool')
                   ORDER BY {order_clause}""",
                (team_id,)
            )
            players = await cursor.fetchall()

            embed = discord.Embed(title=f"{team_title}", color=discord.Color.blue())

            if players:
                # Hide OVR for Draft Pool team
                is_draft_pool = (t_name == "Draft Pool")

                # Check if we should group by position
                if sort_by == "position":
                    # Group players by position
                    from positions import POSITION_DISPLAY_ORDER
                    position_groups = {}
                    for name, pos, rating, age in players:
                        if pos not in position_groups:
                            position_groups[pos] = []
                        ovr_display = "??" if is_draft_pool else str(rating)
                        position_groups[pos].append(f"**{name}** - {ovr_display} OVR, {age}yo")

                    # Add fields in POSITION_DISPLAY_ORDER
                    for pos in POSITION_DISPLAY_ORDER:
                        if pos in position_groups:
                            embed.add_field(
                                name=pos,
                                value="\n".join(position_groups[pos]),
                                inline=False
                            )
                else:
                    # Build simple player list
                    player_lines = []
                    for name, pos, rating, age in players:
                        ovr_display = "??" if is_draft_pool else str(rating)
                        player_lines.append(f"**{name}** - {pos}, {ovr_display} OVR, {age}yo")

                    # Split into chunks if needed (Discord embed description limit is 4096 chars)
                    description = "\n".join(player_lines)
                    if len(description) > 4000:
                        # If too long, split into fields
                        chunk_size = 20
                        for i in range(0, len(player_lines), chunk_size):
                            chunk = player_lines[i:i + chunk_size]
                            embed.add_field(
                                name=f"Players {i+1}-{i+len(chunk)}" if i > 0 else "Players",
                                value="\n".join(chunk),
                                inline=False
                            )
                    else:
                        embed.description = description

                # Add roster size to footer
                embed.set_footer(text=f"{len(players)}/44 players")
            else:
                embed.description = "No players on this team"
                embed.set_footer(text="0/44 players")

            await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="injurylist", description="View current injuries and suspensions")
    @app_commands.describe(team_name="Name of the team (leave empty for all teams)")
    @app_commands.autocomplete(team_name=team_name_autocomplete)
    async def injurylist(self, interaction: discord.Interaction, team_name: str = None):
        from commands.season_commands import get_round_name

        await interaction.response.defer(ephemeral=True)
        async with aiosqlite.connect(DB_PATH) as db:
            team_id = None
            team_title = "All Teams"
            if team_name:
                cursor = await db.execute(
                    "SELECT team_id, team_name, emoji_id FROM teams WHERE team_name = ?",
                    (team_name,)
                )
                team = await cursor.fetchone()
                if not team:
                    await interaction.followup.send(
                        f"❌ Team '{team_name}' not found. Please select from the autocomplete suggestions."
                    )
                    return
                team_id, t_name, emoji_id = team
                emoji = get_team_emoji_str(self.bot, emoji_id)
                team_title = f"{emoji}{t_name}"

            cursor = await db.execute(
                """SELECT season_id, season_number, current_round, regular_rounds, total_rounds, status
                   FROM seasons WHERE status IN ('active', 'offseason')
                   ORDER BY CASE status WHEN 'active' THEN 1 ELSE 2 END LIMIT 1"""
            )
            season = await cursor.fetchone()

            if not season:
                await interaction.followup.send("❌ No active season!")
                return

            season_id, season_number, current_round, regular_rounds, total_rounds, status = season

            if status == 'offseason':
                # No live "current round" to compare against during the
                # offseason - return_round is still expressed in the OLD
                # season's round numbers (carryover into the new season's
                # numbering only happens once /startseason actually runs,
                # since it needs an admin-supplied offseason_weeks value -
                # see season_commands.py's start_season). Estimate using
                # that command's own default (23) so the round shown here
                # lines up with what /startseason will produce if run with
                # its default, but it's still just an estimate - offseason_weeks
                # can be overridden at that point in time.
                default_offseason_weeks = 23
                if team_id:
                    cursor = await db.execute(
                        """SELECT p.name, p.overall_rating, i.injury_type, i.return_round, t.team_name, t.emoji_id
                           FROM injuries i
                           JOIN players p ON i.player_id = p.player_id
                           LEFT JOIN teams t ON p.team_id = t.team_id
                           WHERE i.status = 'injured' AND p.team_id = ?
                           ORDER BY i.return_round ASC, p.name ASC""",
                        (team_id,)
                    )
                else:
                    cursor = await db.execute(
                        """SELECT p.name, p.overall_rating, i.injury_type, i.return_round, t.team_name, t.emoji_id
                           FROM injuries i
                           JOIN players p ON i.player_id = p.player_id
                           LEFT JOIN teams t ON p.team_id = t.team_id
                           WHERE i.status = 'injured'
                           ORDER BY t.team_name ASC, i.return_round ASC, p.name ASC"""
                    )
                injuries = await cursor.fetchall()

                if team_id:
                    cursor = await db.execute(
                        """SELECT p.name, p.overall_rating, s.suspension_reason, s.games_remaining, t.team_name, t.emoji_id
                           FROM suspensions s
                           JOIN players p ON s.player_id = p.player_id
                           LEFT JOIN teams t ON p.team_id = t.team_id
                           WHERE s.status = 'suspended' AND p.team_id = ?
                           ORDER BY s.games_remaining ASC, p.name ASC""",
                        (team_id,)
                    )
                else:
                    cursor = await db.execute(
                        """SELECT p.name, p.overall_rating, s.suspension_reason, s.games_remaining, t.team_name, t.emoji_id
                           FROM suspensions s
                           JOIN players p ON s.player_id = p.player_id
                           LEFT JOIN teams t ON p.team_id = t.team_id
                           WHERE s.status = 'suspended'
                           ORDER BY t.team_name ASC, s.games_remaining ASC, p.name ASC"""
                    )
                suspensions = await cursor.fetchall()

                lines = []
                if injuries:
                    lines.append("**🚑 Injuries:**")
                    for name, ovr, injury_type, return_round, t_name, emoji_id in injuries:
                        team_display = get_team_emoji_str(self.bot, emoji_id) if t_name and not team_id else ""
                        weeks_remaining = return_round - total_rounds
                        new_season_round = max(weeks_remaining - default_offseason_weeks, 1)
                        lines.append(f"{team_display}{name} ({ovr}) - {injury_type} - Round {new_season_round}")

                if suspensions:
                    if injuries:
                        lines.append("")
                    lines.append("**🚫 Suspensions:**")
                    for name, ovr, suspension_reason, games_remaining, t_name, emoji_id in suspensions:
                        team_display = get_team_emoji_str(self.bot, emoji_id) if t_name and not team_id else ""
                        display_reason = re.sub(r' - (low|medium|high) impact$', '', suspension_reason)
                        game_text = "match" if games_remaining == 1 else "matches"
                        lines.append(f"{team_display}{name} ({ovr}) - {display_reason} - {games_remaining} {game_text}")

                embed = discord.Embed(
                    title=f"{team_title} - Injuries & Suspensions",
                    description="\n".join(lines) if lines else "No current injuries or suspensions.",
                    color=discord.Color.blue()
                )
                await interaction.followup.send(embed=embed)
                return

            # Deliberately NOT filtered by finals elimination. This is a
            # public reference list people check for any player - an
            # eliminated club's injuries still matter for trade/draft
            # planning and next season, so /injurylist shows every team all
            # year. The round-by-round list posted to the injury channel
            # (build_injury_suspension_list in injury_commands.py) DOES
            # exclude eliminated teams, since that one is about who is
            # available for the matches still to be played.

            if team_id:
                cursor = await db.execute(
                    """SELECT p.name, p.overall_rating, i.injury_type, i.return_round, t.team_name, t.emoji_id, p.team_id
                       FROM injuries i
                       JOIN players p ON i.player_id = p.player_id
                       LEFT JOIN teams t ON p.team_id = t.team_id
                       WHERE i.status = 'injured' AND p.team_id = ?
                       ORDER BY i.return_round ASC, p.name ASC""",
                    (team_id,)
                )
            else:
                cursor = await db.execute(
                    """SELECT p.name, p.overall_rating, i.injury_type, i.return_round, t.team_name, t.emoji_id, p.team_id
                       FROM injuries i
                       JOIN players p ON i.player_id = p.player_id
                       LEFT JOIN teams t ON p.team_id = t.team_id
                       WHERE i.status = 'injured'
                       ORDER BY t.team_name ASC, i.return_round ASC, p.name ASC"""
                )
            injuries = await cursor.fetchall()

            if team_id:
                cursor = await db.execute(
                    """SELECT p.name, p.overall_rating, s.suspension_reason, s.games_remaining, t.team_name, t.emoji_id, p.team_id
                       FROM suspensions s
                       JOIN players p ON s.player_id = p.player_id
                       LEFT JOIN teams t ON p.team_id = t.team_id
                       WHERE s.status = 'suspended' AND p.team_id = ?
                       ORDER BY s.games_remaining ASC, p.name ASC""",
                    (team_id,)
                )
            else:
                cursor = await db.execute(
                    """SELECT p.name, p.overall_rating, s.suspension_reason, s.games_remaining, t.team_name, t.emoji_id, p.team_id
                       FROM suspensions s
                       JOIN players p ON s.player_id = p.player_id
                       LEFT JOIN teams t ON p.team_id = t.team_id
                       WHERE s.status = 'suspended'
                       ORDER BY t.team_name ASC, s.games_remaining ASC, p.name ASC"""
                )
            suspensions = await cursor.fetchall()

            lines = []
            if injuries:
                lines.append("**🚑 Injuries:**")
                for name, ovr, injury_type, return_round, t_name, emoji_id, _ in injuries:
                    team_display = get_team_emoji_str(self.bot, emoji_id) if t_name and not team_id else ""
                    weeks_left = return_round - current_round
                    if weeks_left <= 0:
                        status_text = "✅ Recovered"
                    else:
                        week_text = "week" if weeks_left == 1 else "weeks"
                        season_indicator = " (SEASON)" if return_round > total_rounds else ""
                        status_text = f"{weeks_left} {week_text}{season_indicator}"
                    lines.append(f"{team_display}{name} ({ovr}) - {injury_type} - {status_text}")

            if suspensions:
                if injuries:
                    lines.append("")
                lines.append("**🚫 Suspensions:**")
                for name, ovr, suspension_reason, games_remaining, t_name, emoji_id, _ in suspensions:
                    team_display = get_team_emoji_str(self.bot, emoji_id) if t_name and not team_id else ""
                    display_reason = re.sub(r' - (low|medium|high) impact$', '', suspension_reason)
                    game_text = "match" if games_remaining == 1 else "matches"
                    lines.append(f"{team_display}{name} ({ovr}) - {display_reason} - {games_remaining} {game_text}")

            embed = discord.Embed(
                title=f"{team_title} - Injuries & Suspensions",
                description="\n".join(lines) if lines else "No current injuries or suspensions.",
                color=discord.Color.blue()
            )
            await interaction.followup.send(embed=embed)
    @app_commands.command(name="filterplayers", description="Search for players with filters")
    async def search_players(self, interaction: discord.Interaction):
        """Opens the interactive filter menu. All filtering happens through
        the menu's own dropdowns/buttons rather than command parameters, so
        filters can be adjusted without re-running the command."""
        async with aiosqlite.connect(DB_PATH) as db:
            teams = await fetch_teams_for_dropdown(db)

            view = FilterPlayersView(self, teams)
            await view.load(db)

        view.update_components()
        await interaction.response.send_message(
            embed=view.create_embed(), view=view, ephemeral=True
        )
        view.message = await interaction.original_response()


# ---------------------------------------------------------------------------
# Player profiles
# ---------------------------------------------------------------------------
# A profile is one embed showing a player's identity (name/team/position/age/
# OVR), their availability, and their stats for a single season. Stats live in
# player_match_stats (one row per player per simulated match), which has no
# season column of its own - the season is reached by joining through matches,
# so every query here goes player_match_stats -> matches -> seasons.

# Stat columns in display order: (db column, short label, long label).
PROFILE_STAT_COLUMNS = [
    ('disposals', 'Disposals', 'Disposals'),
    ('goals', 'Goals', 'Goals'),
    ('behinds', 'Behinds', 'Behinds'),
    ('marks', 'Marks', 'Marks'),
    ('tackles', 'Tackles', 'Tackles'),
    ('spoils', 'Spoils', 'Spoils'),
    ('hitouts', 'Hitouts', 'Hitouts'),
]


async def resolve_profile_season(db):
    """The season a profile's stats are shown for: the active season if there
    is one, otherwise the most recent season that exists (so profiles still
    work in the offseason, showing the season just completed). Returns
    (season_id, season_number) or (None, None) if no season exists at all."""
    cursor = await db.execute(
        "SELECT season_id, season_number FROM seasons WHERE status = 'active' LIMIT 1"
    )
    row = await cursor.fetchone()
    if row:
        return row[0], row[1]

    cursor = await db.execute(
        "SELECT season_id, season_number FROM seasons ORDER BY season_number DESC LIMIT 1"
    )
    row = await cursor.fetchone()
    return (row[0], row[1]) if row else (None, None)


async def fetch_player_profile_data(db, player_id, season_id):
    """Everything a profile embed needs for one player, as a dict. Returns
    None if the player doesn't exist."""
    cursor = await db.execute(
        """SELECT p.player_id, p.name, p.position, p.overall_rating, p.age,
                  p.contract_expiry, t.team_name, t.emoji_id
           FROM players p
           LEFT JOIN teams t ON p.team_id = t.team_id
           WHERE p.player_id = ?""",
        (player_id,)
    )
    row = await cursor.fetchone()
    if not row:
        return None

    data = {
        'player_id': row[0],
        'name': row[1],
        'position': row[2],
        'overall_rating': row[3],
        'age': row[4],
        'contract_expiry': row[5],
        'team_name': row[6],
        'emoji_id': row[7],
    }

    # Season stats. games_played is the row count, not a stored column - a row
    # exists precisely when the player took the field in a simulated match.
    stat_sums = ", ".join(f"COALESCE(SUM(pms.{col}), 0)" for col, _, _ in PROFILE_STAT_COLUMNS)
    if season_id is not None:
        cursor = await db.execute(
            f"""SELECT COUNT(*), {stat_sums}
                FROM player_match_stats pms
                JOIN matches m ON pms.match_id = m.match_id
                WHERE pms.player_id = ? AND m.season_id = ?""",
            (player_id, season_id)
        )
    else:
        cursor = await db.execute(
            f"""SELECT COUNT(*), {stat_sums}
                FROM player_match_stats pms
                WHERE pms.player_id = ? AND 1 = 0""",
            (player_id,)
        )
    stat_row = await cursor.fetchone()
    data['games_played'] = stat_row[0]
    data['stats'] = {
        col: stat_row[idx + 1] for idx, (col, _, _) in enumerate(PROFILE_STAT_COLUMNS)
    }

    # Career games, across every season - context for a player whose current
    # season has barely started.
    cursor = await db.execute(
        "SELECT COUNT(*) FROM player_match_stats WHERE player_id = ?",
        (player_id,)
    )
    data['career_games'] = (await cursor.fetchone())[0]

    # Availability. Mirrors the status wording used by /injurylist.
    cursor = await db.execute(
        """SELECT injury_type, return_round FROM injuries
           WHERE player_id = ? AND status = 'injured'""",
        (player_id,)
    )
    injury = await cursor.fetchone()
    cursor = await db.execute(
        """SELECT suspension_reason, games_remaining FROM suspensions
           WHERE player_id = ? AND status = 'suspended'""",
        (player_id,)
    )
    suspension = await cursor.fetchone()
    data['injury'] = injury
    data['suspension'] = suspension

    return data


def build_player_profile_embed(cog, data, season_number, per_game=False):
    """One player's profile embed. per_game divides every stat by games
    played (rendered to 1 decimal place); totals are shown as integers.

    season_number is the season the stats were fetched for, and heads the
    stats block."""
    emoji = cog.get_team_emoji(data['emoji_id']) if data['emoji_id'] else ""

    # Draft Pool ratings are hidden everywhere else, so keep them hidden here.
    ovr_display = "??" if data['team_name'] == 'Draft Pool' else str(data['overall_rating'])

    title = f"{emoji} {data['name']}" if emoji else data['name']

    # The team is already carried by the title's emoji, so it isn't repeated
    # as its own line - except for a delisted player, who has no team and so
    # no emoji to carry it.
    header_bits = [
        f"**{data['position']}** • {data['age']}yo • **{ovr_display}** OVR",
    ]
    if data['team_name'] is None:
        header_bits.append("*Delisted*")

    # Availability line, only when there's something to report.
    if data['suspension']:
        reason, games_remaining = data['suspension']
        display_reason = re.sub(r' - (low|medium|high) impact$', '', reason)
        game_text = "match" if games_remaining == 1 else "matches"
        header_bits.append(f"🟥 Suspended - {display_reason} ({games_remaining} {game_text})")
    elif data['injury']:
        injury_type, return_round = data['injury']
        if return_round:
            header_bits.append(f"🏥 Injured - {injury_type} (returns Round {return_round})")
        else:
            header_bits.append(f"🏥 Injured - {injury_type}")

    embed = discord.Embed(
        title=title,
        description="\n".join(header_bits),
        color=discord.Color.blue()
    )

    games = data['games_played']

    # The header names the season only - which mode is showing is conveyed by
    # the toggle button and by whether the values carry a decimal.
    season_label = f"Season {season_number} Stats" if season_number is not None else "Stats"

    if games == 0:
        embed.add_field(
            name=season_label,
            value="No games played this season.",
            inline=False
        )
    else:
        stat_lines = [f"Games Played: **{games}**"]
        for col, label, _ in PROFILE_STAT_COLUMNS:
            total = data['stats'][col]
            if per_game:
                stat_lines.append(f"{label}: **{total / games:.1f}**")
            else:
                stat_lines.append(f"{label}: **{total}**")
        embed.add_field(
            name=season_label,
            value="\n".join(stat_lines),
            inline=False
        )

    if data['career_games'] != games:
        embed.set_footer(text=f"Career games: {data['career_games']}")

    return embed


class PlayerProfileView(discord.ui.View):
    """A player's profile with a totals/per-game toggle. `back_view` and
    `back_embed`, when given, add a Back button returning to whatever menu
    opened this profile (a filter results page, or a multi-result /player
    list) so the profile can be opened and closed without re-running the
    command."""

    def __init__(self, cog, data, season_number, back_view=None, back_embed=None):
        super().__init__(timeout=180)
        self.cog = cog
        self.data = data
        self.season_number = season_number
        self.per_game = False
        self.back_view = back_view
        self.back_embed = back_embed
        self.update_components()

    def create_embed(self):
        return build_player_profile_embed(
            self.cog, self.data, self.season_number, per_game=self.per_game
        )

    def update_components(self):
        self.clear_items()

        # The toggle is pointless with no games played - every stat is blank.
        if self.data['games_played'] > 0:
            toggle = discord.ui.Button(
                label="Show Totals" if self.per_game else "Show Averages",
                style=discord.ButtonStyle.primary
            )
            toggle.callback = self.toggle_mode
            self.add_item(toggle)

        if self.back_view is not None:
            back = discord.ui.Button(label="◀ Back", style=discord.ButtonStyle.secondary)
            back.callback = self.go_back
            self.add_item(back)

    async def toggle_mode(self, interaction: discord.Interaction):
        self.per_game = not self.per_game
        self.update_components()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    async def go_back(self, interaction: discord.Interaction):
        embed = self.back_embed
        if embed is None and hasattr(self.back_view, 'create_embed'):
            embed = self.back_view.create_embed()
        await interaction.response.edit_message(embed=embed, view=self.back_view)


class ProfilePickSelect(discord.ui.Select):
    """Opens a player's profile from a list of players. `players` is a list of
    (player_id, name, position, rating, age, team_name, emoji_id) tuples,
    capped by the caller to Discord's 25-option limit."""

    def __init__(self, cog, players, parent_view, row=None, placeholder="View a player's profile..."):
        options = []
        for player_id, name, position, rating, age, team_name, emoji_id in players[:25]:
            ovr_display = "??" if team_name == 'Draft Pool' else str(rating)
            team_display = team_name or "Delisted"
            options.append(discord.SelectOption(
                label=name[:100],
                value=str(player_id),
                description=f"{team_display} • {position} • {age}yo • {ovr_display} OVR"[:100]
            ))

        super().__init__(placeholder=placeholder, options=options, min_values=1, max_values=1, row=row)
        self.cog = cog
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        player_id = int(self.values[0])
        async with aiosqlite.connect(DB_PATH) as db:
            season_id, season_number = await resolve_profile_season(db)
            data = await fetch_player_profile_data(db, player_id, season_id)

        if data is None:
            await interaction.response.send_message(
                "❌ That player no longer exists.", ephemeral=True
            )
            return

        back_embed = None
        if hasattr(self.parent_view, 'create_embed'):
            back_embed = self.parent_view.create_embed()

        view = PlayerProfileView(
            self.cog, data, season_number,
            back_view=self.parent_view, back_embed=back_embed
        )
        await interaction.response.edit_message(embed=view.create_embed(), view=view)


# ---------------------------------------------------------------------------
# Interactive player filter menu (/filterplayers)
# ---------------------------------------------------------------------------
# Filters are held in a FilterState and re-queried on every change, rather
# than fetching every player once and filtering in memory - the query is
# cheap, and it keeps the SQL as the single source of truth for filtering.
#
# Component budget: Discord allows 5 action rows per message and a Select
# eats a whole row. Position / team / sort / profile-pick dropdowns are 4 of
# them, so the five numeric filters share ONE button opening a 5-field modal
# (a modal's own limit is 5 inputs, which is exactly what's needed) and the
# last row carries that button plus pagination.

FILTER_SORT_OPTIONS = [
    ('ovr_desc', 'OVR (High to Low)'),
    ('ovr_asc', 'OVR (Low to High)'),
    ('age_desc', 'Age (Oldest to Youngest)'),
    ('age_asc', 'Age (Youngest to Oldest)'),
]

# (FilterState attribute, modal field label) for the numeric filters, in the
# order they appear in the modal.
NUMERIC_FILTER_FIELDS = [
    ('min_age', 'Min Age'),
    ('max_age', 'Max Age'),
    ('min_ovr', 'Min OVR'),
    ('max_ovr', 'Max OVR'),
    ('contract_expiry', 'Contract Expiry (season)'),
]


class FilterState:
    """The current filter selections, and the query that realises them."""

    def __init__(self):
        self.positions = []        # [] means all positions
        self.team_ids = []         # [] means all teams
        # Delisted players (team_id IS NULL) are excluded by default - the
        # common case is browsing listed players. Selecting "Delisted" in the
        # team dropdown turns them on, either on their own or alongside real
        # teams.
        self.include_delisted = False
        self.min_age = None
        self.max_age = None
        self.min_ovr = None
        self.max_ovr = None
        self.contract_expiry = None
        self.sort_by = 'ovr_desc'

    async def fetch(self, db):
        # No LIMIT: the menu pages through whatever matches, and a cap here
        # would silently truncate the list AND make the "(N found)" count in
        # the embed title wrong. A full league is comfortably small enough to
        # hold in memory.
        # Draft Pool players are never listed here (their ratings are hidden
        # until drafted - see /viewdraftpool).
        query = """SELECT p.player_id, p.name, p.position, p.overall_rating, p.age,
                          t.team_name, t.emoji_id
                   FROM players p
                   LEFT JOIN teams t ON p.team_id = t.team_id
                   WHERE (t.team_name IS NULL OR t.team_name != 'Draft Pool')"""
        params = []

        # Team scoping. A delisted player has team_id IS NULL, so it can't be
        # expressed as a team_id and needs its own OR branch.
        if self.team_ids and self.include_delisted:
            placeholders = ", ".join(["?"] * len(self.team_ids))
            query += f" AND (p.team_id IN ({placeholders}) OR p.team_id IS NULL)"
            params.extend(self.team_ids)
        elif self.team_ids:
            placeholders = ", ".join(["?"] * len(self.team_ids))
            query += f" AND p.team_id IN ({placeholders})"
            params.extend(self.team_ids)
        elif self.include_delisted:
            # Delisted only - no team selected alongside it.
            query += " AND p.team_id IS NULL"
        else:
            query += " AND p.team_id IS NOT NULL"

        if self.positions:
            placeholders = ", ".join(["?"] * len(self.positions))
            query += f" AND p.position IN ({placeholders})"
            params.extend(self.positions)

        if self.min_age is not None:
            query += " AND p.age >= ?"
            params.append(self.min_age)
        if self.max_age is not None:
            query += " AND p.age <= ?"
            params.append(self.max_age)
        if self.min_ovr is not None:
            query += " AND p.overall_rating >= ?"
            params.append(self.min_ovr)
        if self.max_ovr is not None:
            query += " AND p.overall_rating <= ?"
            params.append(self.max_ovr)
        if self.contract_expiry is not None:
            query += " AND p.contract_expiry = ?"
            params.append(self.contract_expiry)

        order_clauses = {
            'ovr_desc': "p.overall_rating DESC, p.age ASC",
            'ovr_asc': "p.overall_rating ASC, p.age ASC",
            'age_desc': "p.age DESC, p.overall_rating DESC",
            'age_asc': "p.age ASC, p.overall_rating DESC",
        }
        query += f" ORDER BY {order_clauses.get(self.sort_by, order_clauses['ovr_desc'])}"

        cursor = await db.execute(query, params)
        return await cursor.fetchall()

    def describe(self, team_names):
        """Human-readable summary of the active filters. `team_names` maps
        team_id -> team_name for rendering the team filter."""
        bits = []
        if self.positions:
            bits.append(f"Positions: {', '.join(self.positions)}")
        if self.team_ids or self.include_delisted:
            names = [team_names.get(tid, str(tid)) for tid in self.team_ids]
            if self.include_delisted:
                names.append("Delisted")
            bits.append(f"Teams: {', '.join(names)}")
        if self.min_ovr is not None:
            bits.append(f"OVR min {self.min_ovr}")
        if self.max_ovr is not None:
            bits.append(f"OVR max {self.max_ovr}")
        if self.min_age is not None:
            bits.append(f"Age min {self.min_age}")
        if self.max_age is not None:
            bits.append(f"Age max {self.max_age}")
        if self.contract_expiry is not None:
            bits.append(f"Contract expiry: Season {self.contract_expiry}")
        return " | ".join(bits) if bits else "No filters"


class NumericFiltersModal(discord.ui.Modal, title="Set Numeric Filters"):
    """All five numeric filters in one form. Each field is pre-filled with its
    current value and is optional - clearing a field clears that filter, which
    is how a user undoes a min/max they no longer want."""

    def __init__(self, parent_view):
        super().__init__()
        self.parent_view = parent_view
        self.inputs = {}

        for attribute, label in NUMERIC_FILTER_FIELDS:
            current = getattr(parent_view.state, attribute)
            field = discord.ui.TextInput(
                label=label,
                placeholder="Leave blank for no limit",
                default=str(current) if current is not None else None,
                required=False,
                max_length=4
            )
            self.inputs[attribute] = field
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction):
        # Validate everything before applying any of it, so a single typo
        # doesn't leave the filters half-updated.
        parsed = {}
        for attribute, label in NUMERIC_FILTER_FIELDS:
            raw = self.inputs[attribute].value.strip()
            if not raw:
                parsed[attribute] = None
                continue
            try:
                parsed[attribute] = int(raw)
            except ValueError:
                await interaction.response.send_message(
                    f"'{raw}' isn't a whole number - check the {label} field.",
                    ephemeral=True
                )
                return

        for attribute, value in parsed.items():
            setattr(self.parent_view.state, attribute, value)

        await self.parent_view.refresh(interaction)


class NumericFiltersButton(discord.ui.Button):
    def __init__(self, parent_view, row):
        state = parent_view.state
        active = sum(
            1 for attribute, _ in NUMERIC_FILTER_FIELDS
            if getattr(state, attribute) is not None
        )
        super().__init__(
            label=f"Age / OVR / Contract ({active})" if active else "Age / OVR / Contract",
            style=discord.ButtonStyle.primary if active else discord.ButtonStyle.secondary,
            row=row
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(NumericFiltersModal(self.parent_view))


class ResetFiltersButton(discord.ui.Button):
    def __init__(self, parent_view, row):
        super().__init__(label="Reset Filters", style=discord.ButtonStyle.danger, row=row)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.state = FilterState()
        await self.parent_view.refresh(interaction)


class PositionFilterSelect(discord.ui.Select):
    def __init__(self, parent_view, row):
        from positions import POSITION_DISPLAY_ORDER
        options = [
            discord.SelectOption(
                label=pos, value=pos,
                default=pos in parent_view.state.positions
            )
            for pos in POSITION_DISPLAY_ORDER
        ]
        super().__init__(
            placeholder="Filter by position",
            options=options, min_values=0, max_values=len(options), row=row
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.state.positions = list(self.values)
        self.parent_view.current_page = 0
        await self.parent_view.refresh(interaction)


class TeamFilterSelect(discord.ui.Select):
    # Sentinel value for the "Delisted" entry - delisted players have no
    # team_id at all (it's NULL), so they can't be addressed by one.
    DELISTED_VALUE = "delisted"

    def __init__(self, parent_view, teams, row):
        # Discord caps a Select at 25 options; an 18-20 team league plus the
        # Delisted entry fits, but trim the teams defensively so an oversized
        # league degrades rather than erroring out.
        options = build_team_options(
            parent_view.cog.bot, teams,
            selected=parent_view.state.team_ids,
            extra_options=[discord.SelectOption(
                label="Delisted", value=self.DELISTED_VALUE,
                description="Players not on any list",
                default=parent_view.state.include_delisted,
            )],
        )
        super().__init__(
            placeholder="Filter by team",
            options=options, min_values=0, max_values=len(options), row=row
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        values = list(self.values)
        self.parent_view.state.include_delisted = self.DELISTED_VALUE in values
        self.parent_view.state.team_ids = [
            int(v) for v in values if v != self.DELISTED_VALUE
        ]
        self.parent_view.current_page = 0
        await self.parent_view.refresh(interaction)


class SortFilterSelect(discord.ui.Select):
    def __init__(self, parent_view, row):
        options = [
            discord.SelectOption(
                label=label, value=value,
                default=value == parent_view.state.sort_by
            )
            for value, label in FILTER_SORT_OPTIONS
        ]
        super().__init__(placeholder="Sort by...", options=options,
                         min_values=1, max_values=1, row=row)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.state.sort_by = self.values[0]
        self.parent_view.current_page = 0
        await self.parent_view.refresh(interaction)


class FilterPlayersView(discord.ui.View):
    """The /filterplayers menu: live filter controls plus the matching player
    list. Every control change re-runs the query and edits the same message,
    so the filters and their results are always one panel.

    Row layout (Discord's cap is 5 rows, a Select taking a full row):
        0  numeric-filters button, reset, pagination
        1  position dropdown
        2  team dropdown
        3  sort dropdown
        4  profile-pick dropdown (only when there are results)
    """

    PLAYERS_PER_PAGE = 15

    def __init__(self, cog, teams, state=None):
        super().__init__(timeout=300)
        self.cog = cog
        self.teams = teams                                  # [(team_id, team_name, emoji_id)]
        self.team_names = {row[0]: row[1] for row in teams}
        self.state = state or FilterState()
        self.players = []
        self.current_page = 0
        self.message = None

    @property
    def total_pages(self):
        if not self.players:
            return 1
        return (len(self.players) + self.PLAYERS_PER_PAGE - 1) // self.PLAYERS_PER_PAGE

    def page_players(self):
        start = self.current_page * self.PLAYERS_PER_PAGE
        return self.players[start:start + self.PLAYERS_PER_PAGE]

    async def load(self, db):
        """Re-runs the filter query and clamps the page to the new result
        count (a tightened filter can leave current_page past the end)."""
        self.players = await self.state.fetch(db)
        if self.current_page >= self.total_pages:
            self.current_page = max(self.total_pages - 1, 0)

    async def refresh(self, interaction: discord.Interaction):
        """Re-query, rebuild the components, and edit the message in place.
        Used by every filter control's callback."""
        async with aiosqlite.connect(DB_PATH) as db:
            await self.load(db)
        self.update_components()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    def update_components(self):
        self.clear_items()

        self.add_item(NumericFiltersButton(self, row=0))
        self.add_item(ResetFiltersButton(self, row=0))

        if self.total_pages > 1:
            prev_button = discord.ui.Button(
                label="◀ Prev",
                style=discord.ButtonStyle.secondary,
                disabled=(self.current_page == 0),
                row=0
            )
            prev_button.callback = self.previous_page
            self.add_item(prev_button)

            next_button = discord.ui.Button(
                label="Next ▶",
                style=discord.ButtonStyle.secondary,
                disabled=(self.current_page >= self.total_pages - 1),
                row=0
            )
            next_button.callback = self.next_page
            self.add_item(next_button)

        self.add_item(PositionFilterSelect(self, row=1))
        self.add_item(TeamFilterSelect(self, self.teams, row=2))
        self.add_item(SortFilterSelect(self, row=3))

        # The profile dropdown lists this page's players, so it stays within
        # Discord's 25-option cap as long as a page does.
        page_players = self.page_players()
        if page_players:
            self.add_item(ProfilePickSelect(self.cog, page_players, self, row=4))

    def create_embed(self):
        page_players = self.page_players()

        if not self.players:
            description = "No players match these filters."
        else:
            lines = []
            for _, name, position, rating, age, team_name, emoji_id in page_players:
                emoji = self.cog.get_team_emoji(emoji_id) if emoji_id else ""
                team_prefix = f"{emoji} " if emoji else ""
                ovr_display = "??" if team_name == 'Draft Pool' else str(rating)
                lines.append(f"{team_prefix}**{name}** - {position} ({ovr_display} OVR, {age}yo)")
            description = "\n".join(lines)

        embed = discord.Embed(
            title=f"Player Search ({len(self.players)} found)",
            description=description,
            color=discord.Color.purple()
        )
        embed.add_field(name="Filters", value=self.state.describe(self.team_names), inline=False)

        if self.players and self.total_pages > 1:
            start = self.current_page * self.PLAYERS_PER_PAGE
            end = min(start + self.PLAYERS_PER_PAGE, len(self.players))
            embed.set_footer(
                text=f"Page {self.current_page + 1}/{self.total_pages} - "
                     f"showing {start + 1}-{end} of {len(self.players)}"
            )

        return embed

    async def previous_page(self, interaction: discord.Interaction):
        if self.current_page > 0:
            self.current_page -= 1
            self.update_components()
            await interaction.response.edit_message(embed=self.create_embed(), view=self)
        else:
            await interaction.response.defer()

    async def next_page(self, interaction: discord.Interaction):
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            self.update_components()
            await interaction.response.edit_message(embed=self.create_embed(), view=self)
        else:
            await interaction.response.defer()


class PlayerSearchResultsView(discord.ui.View):
    """The multi-result list from /player: the matching players, with a
    dropdown to open any of their profiles. Unlike FilterPlayersView this has
    no filter controls - the search terms came from the command itself - so
    the results are a fixed list captured at command time."""

    PLAYERS_PER_PAGE = 15

    def __init__(self, cog, players, search_display):
        super().__init__(timeout=300)
        self.cog = cog
        self.players = players
        self.search_display = search_display
        self.current_page = 0
        self.message = None
        self.update_components()

    @property
    def total_pages(self):
        if not self.players:
            return 1
        return (len(self.players) + self.PLAYERS_PER_PAGE - 1) // self.PLAYERS_PER_PAGE

    def page_players(self):
        start = self.current_page * self.PLAYERS_PER_PAGE
        return self.players[start:start + self.PLAYERS_PER_PAGE]

    def update_components(self):
        self.clear_items()

        page_players = self.page_players()
        if page_players:
            self.add_item(ProfilePickSelect(self.cog, page_players, self, row=0))

        if self.total_pages > 1:
            prev_button = discord.ui.Button(
                label="◀ Prev",
                style=discord.ButtonStyle.secondary,
                disabled=(self.current_page == 0),
                row=1
            )
            prev_button.callback = self.previous_page
            self.add_item(prev_button)

            next_button = discord.ui.Button(
                label="Next ▶",
                style=discord.ButtonStyle.secondary,
                disabled=(self.current_page >= self.total_pages - 1),
                row=1
            )
            next_button.callback = self.next_page
            self.add_item(next_button)

    def create_embed(self):
        lines = []
        for _, name, position, rating, age, team_name, emoji_id in self.page_players():
            emoji = self.cog.get_team_emoji(emoji_id) if emoji_id else ""
            team_prefix = f"{emoji} " if emoji else ""
            ovr_display = "??" if team_name == 'Draft Pool' else str(rating)
            lines.append(f"{team_prefix}**{name}** - {position} ({ovr_display} OVR, {age}yo)")

        embed = discord.Embed(
            title=f"Player Search ({len(self.players)} found)",
            description="\n".join(lines),
            color=discord.Color.green()
        )
        embed.set_footer(
            text=f"Searched: {self.search_display}" if self.total_pages == 1
            else f"Searched: {self.search_display} - page {self.current_page + 1}/{self.total_pages}"
        )
        return embed

    async def previous_page(self, interaction: discord.Interaction):
        if self.current_page > 0:
            self.current_page -= 1
            self.update_components()
            await interaction.response.edit_message(embed=self.create_embed(), view=self)
        else:
            await interaction.response.defer()

    async def next_page(self, interaction: discord.Interaction):
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            self.update_components()
            await interaction.response.edit_message(embed=self.create_embed(), view=self)
        else:
            await interaction.response.defer()


async def setup(bot):
    await bot.add_cog(PlayerCommands(bot))