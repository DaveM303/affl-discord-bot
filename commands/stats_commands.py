import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from config import DB_PATH
from utils import get_team_emoji_str

# Core box-score stats only - brownlow_votes/best_fairest_votes are
# deliberately excluded from the leaderboard dropdown (those already have
# their own dedicated views via sim_season.py's --brownlow/--bestfairest
# tooling and post_season_summaries' B&F top 10 - a season leaderboard
# sorted by raw vote count would be redundant with those).
STAT_LABELS = {
    "disposals": "Disposals", "goals": "Goals",
    "marks": "Marks", "tackles": "Tackles", "spoils": "Spoils", "hitouts": "Hitouts",
}
LEADERBOARD_PLAYERS_PER_PAGE = 15


class StatsCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _resolve_active_season(self, db):
        """Same shape as MatchCommands._resolve_season(db, None) - that
        method lives on a different cog and isn't a shared utility, so
        it's reimplemented here rather than reaching into MatchCommands."""
        cursor = await db.execute(
            "SELECT season_id, season_number FROM seasons WHERE status = 'active' LIMIT 1"
        )
        return await cursor.fetchone()

    @app_commands.command(name="statsmenu", description="View player season stat leaderboards")
    async def stats_menu(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            season = await self._resolve_active_season(db)
            if not season:
                await interaction.response.send_message("❌ No active season!", ephemeral=True)
                return
            season_id, season_number = season

            cursor = await db.execute(
                "SELECT team_id, team_name FROM teams WHERE team_name != 'Draft Pool' ORDER BY team_name"
            )
            all_teams = await cursor.fetchall()

            view = StatsMenuView(self.bot, season_id, season_number, all_teams)
            await view.refresh(db)

        await interaction.response.send_message(embed=view.create_embed(), view=view, ephemeral=True)


async def _fetch_season_stat_totals(db, season_id, team_id=None):
    """Returns a list of dicts, one per player who recorded at least one
    stat line in this season (filtered to team_id if given), with summed
    totals for every STAT_LABELS key plus games played. Grouped/filtered by
    pms.team_id (the team a player played FOR in each match), not
    players.team_id - same reasoning as post_season_summaries' goalkicker
    query, so a since-traded player's earlier-season stats stay with the
    team they were earned for. Averages are derived from these totals at
    render time (total / games) rather than a separate query."""
    query = """SELECT pms.player_id, p.name, p.overall_rating, pms.team_id, t.team_name, t.emoji_id,
                      SUM(pms.disposals), SUM(pms.goals), SUM(pms.behinds),
                      SUM(pms.marks), SUM(pms.tackles), SUM(pms.spoils), SUM(pms.hitouts),
                      COUNT(*)
               FROM player_match_stats pms
               JOIN matches m ON pms.match_id = m.match_id
               JOIN players p ON pms.player_id = p.player_id
               JOIN teams t ON pms.team_id = t.team_id
               WHERE m.season_id = ?"""
    params = [season_id]
    if team_id is not None:
        query += " AND pms.team_id = ?"
        params.append(team_id)
    query += " GROUP BY pms.player_id, pms.team_id"

    cursor = await db.execute(query, params)
    rows = await cursor.fetchall()

    totals = []
    for player_id, name, overall_rating, row_team_id, team_name, emoji_id, disposals, goals, behinds, marks, tackles, spoils, hitouts, games in rows:
        totals.append({
            "player_id": player_id, "name": name, "overall_rating": overall_rating, "team_id": row_team_id,
            "team_name": team_name, "emoji_id": emoji_id, "games": games,
            "disposals": disposals, "goals": goals, "behinds": behinds,
            "marks": marks, "tackles": tackles, "spoils": spoils, "hitouts": hitouts,
        })
    return totals


def _stat_value(p, stat_key, mode):
    """Raw total or per-game average for one stat, per the leaderboard's
    total/average toggle. Averages round to 1 decimal place - enough
    precision to differentiate players without implying false accuracy."""
    total = p[stat_key]
    if mode == "total":
        return total
    return round(total / p["games"], 1) if p["games"] else 0.0


class StatsMenuView(discord.ui.View):
    """Main menu: top 5 goalkickers + disposal winners, league-wide (no
    team filter here - that only applies once inside a specific stat
    leaderboard, see _StatLeaderboardView), and a stat dropdown to open a
    full leaderboard. Mirrors MatchCentreView's shape (match_commands.py) -
    re-fetches self.totals from the DB on refresh() rather than
    re-querying per render, same reasoning as that view's own self.matches
    cache."""
    def __init__(self, bot, season_id, season_number, all_teams):
        super().__init__(timeout=1800)
        self.bot = bot
        self.season_id = season_id
        self.season_number = season_number
        self.all_teams = all_teams
        self.totals = []

    async def refresh(self, db):
        self.totals = await _fetch_season_stat_totals(db, self.season_id)
        self.update_components()

    def update_components(self):
        self.clear_items()
        self.add_item(_StatLeaderboardSelect(self))

    def create_embed(self):
        embed = discord.Embed(
            title=f"Player Stats - Season {self.season_number}",
            color=discord.Color.blurple(),
        )

        top_goals = sorted((p for p in self.totals if p["goals"] > 0), key=lambda p: -p["goals"])[:5]
        top_disposals = sorted((p for p in self.totals if p["disposals"] > 0), key=lambda p: -p["disposals"])[:5]

        if top_goals:
            embed.add_field(
                name="Leading Goalkickers",
                value="\n".join(
                    f"{get_team_emoji_str(self.bot, p['emoji_id'])}{p['name']} ({p['overall_rating']}) - **{p['goals']}**"
                    for p in top_goals
                ),
                inline=True,
            )
        if top_disposals:
            embed.add_field(
                name="Disposal Winners",
                value="\n".join(
                    f"{get_team_emoji_str(self.bot, p['emoji_id'])}{p['name']} ({p['overall_rating']}) - **{p['disposals']}**"
                    for p in top_disposals
                ),
                inline=True,
            )
        if not top_goals and not top_disposals:
            embed.description = "No stats recorded yet this season."

        return embed


class _StatLeaderboardSelect(discord.ui.Select):
    def __init__(self, parent_view: StatsMenuView):
        self.parent_view = parent_view
        options = [discord.SelectOption(label=label, value=key) for key, label in STAT_LABELS.items()]
        super().__init__(placeholder="View a stat leaderboard...", options=options)

    async def callback(self, interaction: discord.Interaction):
        leaderboard_view = _StatLeaderboardView(self.parent_view, self.values[0])
        await interaction.response.edit_message(embed=leaderboard_view.create_embed(), view=leaderboard_view)


class _StatLeaderboardView(discord.ui.View):
    """Full leaderboard for one stat, paginated - same Previous/Next
    convention as _MatchStatsView (match_commands.py): labels "◀ Previous"/
    "Next ▶", disabled (not hidden) at the edges, page number shown in the
    embed field name rather than a separate page-indicator button.

    Owns its own stat dropdown, team-filter dropdown, and total/average
    toggle so a user can pivot between stats/teams/modes without ever
    returning to the main menu - each of those callbacks stays on THIS
    view (re-fetching self.totals as needed) rather than bouncing back
    to StatsMenuView. self.parent_view is kept only for the "Main menu"
    button and to read season_id/season_number/all_teams."""
    def __init__(self, parent_view: StatsMenuView, stat_key, team_id=None, team_name=None, mode="total"):
        super().__init__(timeout=1800)
        self.parent_view = parent_view
        self.stat_key = stat_key
        self.team_id = team_id
        self.team_name = team_name
        self.mode = mode  # "total" or "average"
        self.page = 0
        self.totals = parent_view.totals if team_id is None else None
        self.update_components()

    def _ranked(self):
        if self.stat_key == "goals" and self.mode == "total":
            # Goals ties broken by behinds only meaningful as raw totals -
            # once averaged, behinds-per-game is a much noisier tiebreak
            # than just falling through to name order, so skip it there.
            return sorted(
                (p for p in self.totals if p["goals"] > 0 or p["behinds"] > 0),
                key=lambda p: (-p["goals"], -p["behinds"])
            )
        return sorted(
            (p for p in self.totals if p[self.stat_key] > 0),
            key=lambda p: -_stat_value(p, self.stat_key, self.mode)
        )

    def _total_pages(self, ranked):
        return max(1, -(-len(ranked) // LEADERBOARD_PLAYERS_PER_PAGE))

    def create_embed(self):
        ranked = self._ranked()
        total_pages = self._total_pages(ranked)
        start = self.page * LEADERBOARD_PLAYERS_PER_PAGE
        page_players = ranked[start:start + LEADERBOARD_PLAYERS_PER_PAGE]

        mode_label = "Total" if self.mode == "total" else "Average"
        filter_note = f" - {self.team_name}" if self.team_id else ""
        embed = discord.Embed(
            title=f"{STAT_LABELS[self.stat_key]} Leaderboard ({mode_label}) - Season {self.parent_view.season_number}{filter_note}",
            color=discord.Color.blurple(),
        )

        if not page_players:
            embed.description = "No players have recorded this stat yet."
            return embed

        lines = []
        for i, p in enumerate(page_players, start=start + 1):
            emoji = get_team_emoji_str(self.parent_view.bot, p["emoji_id"]) if not self.team_id else ""
            if self.stat_key == "goals" and self.mode == "total":
                value = f"{p['goals']}.{p['behinds']}"
            else:
                value = _stat_value(p, self.stat_key, self.mode)
            lines.append(f"{i}. {emoji}{p['name']} ({p['overall_rating']}) - **{value}**")

        field_name = "Rankings"
        if total_pages > 1:
            field_name += f" (Page {self.page + 1}/{total_pages})"
        embed.add_field(name=field_name, value="\n".join(lines), inline=False)
        return embed

    def update_components(self):
        self.clear_items()
        self.add_item(_LeaderboardStatSelect(self))
        self.add_item(_LeaderboardTeamFilterSelect(self))

        total_pages = self._total_pages(self._ranked())

        prev_button = discord.ui.Button(label="◀ Previous", style=discord.ButtonStyle.primary, disabled=(self.page == 0))
        prev_button.callback = self._previous_page
        self.add_item(prev_button)

        next_button = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.primary, disabled=(self.page >= total_pages - 1))
        next_button.callback = self._next_page
        self.add_item(next_button)

        # Toggle button labeled with the mode it switches TO.
        mode_button = discord.ui.Button(
            label="Show Average" if self.mode == "total" else "Show Total",
            style=discord.ButtonStyle.secondary,
        )
        mode_button.callback = self._toggle_mode
        self.add_item(mode_button)

        back_button = discord.ui.Button(label="Main menu", style=discord.ButtonStyle.secondary)
        back_button.callback = self._back
        self.add_item(back_button)

    async def _previous_page(self, interaction: discord.Interaction):
        self.page -= 1
        self.update_components()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    async def _next_page(self, interaction: discord.Interaction):
        self.page += 1
        self.update_components()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    async def _toggle_mode(self, interaction: discord.Interaction):
        self.mode = "average" if self.mode == "total" else "total"
        self.page = 0
        self.update_components()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    async def _back(self, interaction: discord.Interaction):
        self.parent_view.update_components()
        await interaction.response.edit_message(embed=self.parent_view.create_embed(), view=self.parent_view)


class _LeaderboardStatSelect(discord.ui.Select):
    def __init__(self, parent_view: _StatLeaderboardView):
        self.parent_view = parent_view
        options = [
            discord.SelectOption(label=label, value=key, default=(key == parent_view.stat_key))
            for key, label in STAT_LABELS.items()
        ]
        super().__init__(placeholder="View a stat leaderboard...", options=options)

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.stat_key = self.values[0]
        self.parent_view.page = 0
        self.parent_view.update_components()
        await interaction.response.edit_message(embed=self.parent_view.create_embed(), view=self.parent_view)


class _LeaderboardTeamFilterSelect(discord.ui.Select):
    def __init__(self, parent_view: _StatLeaderboardView):
        self.parent_view = parent_view
        options = [discord.SelectOption(label="All teams", value="all", default=(parent_view.team_id is None))]
        for team_id, team_name in parent_view.parent_view.all_teams:
            options.append(discord.SelectOption(label=team_name, value=str(team_id), default=(team_id == parent_view.team_id)))
        super().__init__(placeholder="Filter by team...", options=options[:25])

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]
        if selected == "all":
            self.parent_view.team_id = None
            self.parent_view.team_name = None
        else:
            team_id = int(selected)
            self.parent_view.team_id = team_id
            self.parent_view.team_name = next(
                (name for tid, name in self.parent_view.parent_view.all_teams if tid == team_id), None
            )
        self.parent_view.page = 0

        async with aiosqlite.connect(DB_PATH) as db:
            self.parent_view.totals = await _fetch_season_stat_totals(db, self.parent_view.parent_view.season_id, self.parent_view.team_id)

        self.parent_view.update_components()
        await interaction.response.edit_message(embed=self.parent_view.create_embed(), view=self.parent_view)


async def setup(bot):
    await bot.add_cog(StatsCommands(bot))
