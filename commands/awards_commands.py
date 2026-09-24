import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from config import DB_PATH, ADMIN_ROLE_ID
from utils import is_admin_user, get_team_emoji, get_team_emoji_str, build_team_options

AWARDS_PLAYERS_PER_PAGE = 15


class AwardsCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Check if user has admin permissions - every command in this cog is admin-only."""
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

    async def _resolve_active_season(self, db):
        """Same shape as StatsCommands._resolve_active_season, plus
        regular_rounds (that method lives on a different cog and isn't a
        shared utility, so it's reimplemented here rather than reaching
        into StatsCommands) - regular_rounds is needed so Brownlow/Coleman
        can be scoped to the home & away season only, excluding finals
        rounds (see _fetch_brownlow_totals/_fetch_coleman_totals)."""
        cursor = await db.execute(
            "SELECT season_id, season_number, regular_rounds FROM seasons WHERE status = 'active' LIMIT 1"
        )
        return await cursor.fetchone()

    @app_commands.command(name="awards", description="[ADMIN] View who is leading the season's awards")
    async def awards(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            season = await self._resolve_active_season(db)
            if not season:
                await interaction.response.send_message("❌ No active season!", ephemeral=True)
                return
            season_id, season_number, regular_rounds = season

            cursor = await db.execute(
                "SELECT team_id, team_name, emoji_id FROM teams WHERE team_name != 'Draft Pool' ORDER BY team_name"
            )
            all_teams = await cursor.fetchall()

            view = AwardsMenuView(self.bot, season_id, season_number, regular_rounds, all_teams)
            await view.refresh(db)

        await interaction.response.send_message(embed=view.create_embed(), view=view, ephemeral=True)


async def _fetch_brownlow_totals(db, season_id, regular_rounds):
    """League-wide Brownlow Medal tally - {player_id: (name, overall_rating,
    team_id, team_name, emoji_id, total_votes)}, one row per player who has
    polled at least one vote. brownlow_votes is awarded per match_sim.py's
    MatchResult.brownlow_votes() across the WHOLE match (both teams judged
    together, not per-club), so this is deliberately never team-filtered -
    unlike best_fairest_votes (club-specific), there's only ever one
    league-wide Brownlow tally.

    Scoped to m.round_number <= regular_rounds (the home & away season
    only) - like the real medal, the Brownlow is awarded before finals,
    so votes polled in a finals match don't count toward the tally."""
    cursor = await db.execute(
        """SELECT pms.player_id, p.name, p.overall_rating, pms.team_id, t.team_name, t.emoji_id,
                  SUM(pms.brownlow_votes) as total_votes
           FROM player_match_stats pms
           JOIN matches m ON pms.match_id = m.match_id
           JOIN players p ON pms.player_id = p.player_id
           JOIN teams t ON pms.team_id = t.team_id
           WHERE m.season_id = ? AND m.round_number <= ?
           GROUP BY pms.player_id
           HAVING total_votes > 0
           ORDER BY total_votes DESC""",
        (season_id, regular_rounds)
    )
    rows = await cursor.fetchall()
    return [
        {"player_id": pid, "name": name, "overall_rating": ovr, "team_id": team_id,
         "team_name": team_name, "emoji_id": emoji_id, "votes": votes}
        for pid, name, ovr, team_id, team_name, emoji_id, votes in rows
    ]


async def _fetch_coleman_totals(db, season_id, regular_rounds):
    """League-wide Coleman Medal tally (most goals) - same shape as
    _fetch_brownlow_totals. Goals are a raw stat total, not a per-match
    award, so team_id here is just descriptive (which club they kicked
    them for - a since-traded player's earlier-season goals stay with the
    team they were kicked for, same reasoning as post_season_summaries'
    own goalkicker query) rather than a filter.

    Scoped to m.round_number <= regular_rounds (the home & away season
    only) - like the real medal, the Coleman is awarded before finals, so
    goals kicked in a finals match don't count toward the tally."""
    cursor = await db.execute(
        """SELECT pms.player_id, p.name, p.overall_rating, pms.team_id, t.team_name, t.emoji_id,
                  SUM(pms.goals) as total_goals, SUM(pms.behinds) as total_behinds
           FROM player_match_stats pms
           JOIN matches m ON pms.match_id = m.match_id
           JOIN players p ON pms.player_id = p.player_id
           JOIN teams t ON pms.team_id = t.team_id
           WHERE m.season_id = ? AND m.round_number <= ?
           GROUP BY pms.player_id
           HAVING total_goals > 0
           ORDER BY total_goals DESC""",
        (season_id, regular_rounds)
    )
    rows = await cursor.fetchall()
    return [
        {"player_id": pid, "name": name, "overall_rating": ovr, "team_id": team_id,
         "team_name": team_name, "emoji_id": emoji_id, "goals": goals, "behinds": behinds}
        for pid, name, ovr, team_id, team_name, emoji_id, goals, behinds in rows
    ]


async def _fetch_best_fairest_totals(db, season_id, team_id):
    """One club's Best & Fairest tally for the season - {player_id: (name,
    overall_rating, total_votes)}, filtered to pms.team_id (the team a
    player earned the votes FOR, not their current team - same reasoning
    as post_season_summaries' own B&F query), ordered by votes descending."""
    cursor = await db.execute(
        """SELECT pms.player_id, p.name, p.overall_rating, SUM(pms.best_fairest_votes) as total_votes
           FROM player_match_stats pms
           JOIN matches m ON pms.match_id = m.match_id
           JOIN players p ON pms.player_id = p.player_id
           WHERE m.season_id = ? AND pms.team_id = ?
           GROUP BY pms.player_id
           HAVING total_votes > 0
           ORDER BY total_votes DESC""",
        (season_id, team_id)
    )
    rows = await cursor.fetchall()
    return [
        {"player_id": pid, "name": name, "overall_rating": ovr, "votes": votes}
        for pid, name, ovr, votes in rows
    ]


class AwardsMenuView(discord.ui.View):
    """Main /awards menu: Brownlow top 3 + Coleman top 3 (both league-wide),
    plus buttons into the full Brownlow leaderboard and the club-by-club
    Best & Fairest view. Mirrors StatsMenuView's shape (stats_commands.py) -
    re-fetches self.brownlow/self.coleman from the DB on refresh() rather
    than re-querying per render."""
    def __init__(self, bot, season_id, season_number, regular_rounds, all_teams):
        super().__init__(timeout=1800)
        self.bot = bot
        self.season_id = season_id
        self.season_number = season_number
        self.regular_rounds = regular_rounds
        self.all_teams = all_teams
        self.brownlow = []
        self.coleman = []

    async def refresh(self, db):
        self.brownlow = await _fetch_brownlow_totals(db, self.season_id, self.regular_rounds)
        self.coleman = await _fetch_coleman_totals(db, self.season_id, self.regular_rounds)
        self.update_components()

    def update_components(self):
        self.clear_items()
        self.add_item(_ViewBrownlowButton(self))
        self.add_item(_ViewColemanButton(self))
        self.add_item(_ViewBestAndFairestButton(self))

    def create_embed(self):
        embed = discord.Embed(
            title=f"🏅 Awards - Season {self.season_number}",
            color=discord.Color.gold(),
        )

        if self.brownlow:
            embed.add_field(
                name="Brownlow Medal - Top 3",
                value="\n".join(
                    f"{get_team_emoji_str(self.bot, p['emoji_id'])}{p['name']} ({p['overall_rating']}) - **{p['votes']}**"
                    for p in self.brownlow[:3]
                ),
                inline=True,
            )
        if self.coleman:
            embed.add_field(
                name="Coleman Medal - Top 3",
                value="\n".join(
                    f"{get_team_emoji_str(self.bot, p['emoji_id'])}{p['name']} ({p['overall_rating']}) - **{p['goals']}**"
                    for p in self.coleman[:3]
                ),
                inline=True,
            )
        if not self.brownlow and not self.coleman:
            embed.description = "No stats recorded yet this season."

        return embed


class _ViewBrownlowButton(discord.ui.Button):
    def __init__(self, parent_view: AwardsMenuView):
        super().__init__(label="View Full Brownlow Votes", style=discord.ButtonStyle.primary)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        brownlow_view = _AwardLeaderboardView(
            self.parent_view, "brownlow", "votes",
            "Brownlow Medal Votes", "No Brownlow votes recorded yet this season."
        )
        await interaction.response.edit_message(embed=brownlow_view.create_embed(), view=brownlow_view)


class _ViewColemanButton(discord.ui.Button):
    def __init__(self, parent_view: AwardsMenuView):
        super().__init__(label="View Full Coleman Leaderboard", style=discord.ButtonStyle.primary)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        coleman_view = _AwardLeaderboardView(
            self.parent_view, "coleman", "goals",
            "Coleman Medal Goals", "No goals recorded yet this season."
        )
        await interaction.response.edit_message(embed=coleman_view.create_embed(), view=coleman_view)


class _ViewBestAndFairestButton(discord.ui.Button):
    def __init__(self, parent_view: AwardsMenuView):
        super().__init__(label="View Club Best & Fairest", style=discord.ButtonStyle.primary)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        default_team_id, default_team_name, default_emoji_id = self.parent_view.all_teams[0]
        async with aiosqlite.connect(DB_PATH) as db:
            totals = await _fetch_best_fairest_totals(db, self.parent_view.season_id, default_team_id)
        bf_view = _BestAndFairestView(self.parent_view, default_team_id, default_team_name, default_emoji_id, totals)
        await interaction.response.edit_message(embed=bf_view.create_embed(), view=bf_view)


class _AwardLeaderboardView(discord.ui.View):
    """Full league-wide leaderboard for one award, paginated - same
    Previous/Next convention as _StatLeaderboardView (stats_commands.py):
    labels "◀ Previous"/"Next ▶", disabled (not hidden) at the edges, page
    number shown in the embed field name. No team filter/mode toggle - both
    awards are already league-wide and count-only (no total/average
    distinction, unlike a raw box-score stat).

    Shared by the Brownlow (vote tallies) and Coleman (goal tallies): the
    two differ only in which list they page through and how they're
    labelled, so `rows_attr` names the AwardsMenuView attribute holding the
    already-fetched, already-sorted rows and `value_key` names the field in
    each row carrying the number to show."""
    def __init__(self, parent_view: AwardsMenuView, rows_attr, value_key, title, empty_text):
        super().__init__(timeout=1800)
        self.parent_view = parent_view
        self.rows_attr = rows_attr
        self.value_key = value_key
        self.title = title
        self.empty_text = empty_text
        self.page = 0
        self.update_components()

    @property
    def rows(self):
        return getattr(self.parent_view, self.rows_attr)

    def _total_pages(self):
        return max(1, -(-len(self.rows) // AWARDS_PLAYERS_PER_PAGE))

    def create_embed(self):
        total_pages = self._total_pages()
        start = self.page * AWARDS_PLAYERS_PER_PAGE
        page_players = self.rows[start:start + AWARDS_PLAYERS_PER_PAGE]

        embed = discord.Embed(
            title=f"{self.title} - Season {self.parent_view.season_number}",
            color=discord.Color.gold(),
        )

        if not page_players:
            embed.description = self.empty_text
            return embed

        lines = [
            f"{i}. {get_team_emoji_str(self.parent_view.bot, p['emoji_id'])}{p['name']} ({p['overall_rating']}) - **{p[self.value_key]}**"
            for i, p in enumerate(page_players, start=start + 1)
        ]
        field_name = "Rankings"
        if total_pages > 1:
            field_name += f" (Page {self.page + 1}/{total_pages})"
        embed.add_field(name=field_name, value="\n".join(lines), inline=False)
        return embed

    def update_components(self):
        self.clear_items()
        total_pages = self._total_pages()

        prev_button = discord.ui.Button(label="◀ Previous", style=discord.ButtonStyle.primary, disabled=(self.page == 0))
        prev_button.callback = self._previous_page
        self.add_item(prev_button)

        next_button = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.primary, disabled=(self.page >= total_pages - 1))
        next_button.callback = self._next_page
        self.add_item(next_button)

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

    async def _back(self, interaction: discord.Interaction):
        self.parent_view.update_components()
        await interaction.response.edit_message(embed=self.parent_view.create_embed(), view=self.parent_view)


class _BestAndFairestView(discord.ui.View):
    """One club's full Best & Fairest leaderboard, with a dropdown to
    switch between clubs - re-queries self.totals on every team switch
    (a different club's votes, not just a different slice of the same
    data), same reasoning as MatchCentreView's own per-selection re-query
    (match_commands.py)."""
    def __init__(self, parent_view: AwardsMenuView, team_id, team_name, emoji_id, totals):
        super().__init__(timeout=1800)
        self.parent_view = parent_view
        self.team_id = team_id
        self.team_name = team_name
        self.emoji_id = emoji_id
        self.totals = totals
        self.page = 0
        self.update_components()

    def _total_pages(self):
        return max(1, -(-len(self.totals) // AWARDS_PLAYERS_PER_PAGE))

    def create_embed(self):
        total_pages = self._total_pages()
        start = self.page * AWARDS_PLAYERS_PER_PAGE
        page_players = self.totals[start:start + AWARDS_PLAYERS_PER_PAGE]

        emoji = get_team_emoji_str(self.parent_view.bot, self.emoji_id)
        embed = discord.Embed(
            title=f"{emoji}{self.team_name} Best & Fairest - Season {self.parent_view.season_number}",
            color=discord.Color.gold(),
        )

        if not page_players:
            embed.description = "No Best & Fairest votes recorded yet this season."
            return embed

        lines = [
            f"{i}. {p['name']} ({p['overall_rating']}) - **{p['votes']}**"
            for i, p in enumerate(page_players, start=start + 1)
        ]
        field_name = "Rankings"
        if total_pages > 1:
            field_name += f" (Page {self.page + 1}/{total_pages})"
        embed.add_field(name=field_name, value="\n".join(lines), inline=False)
        return embed

    def update_components(self):
        self.clear_items()
        self.add_item(_BestAndFairestTeamSelect(self))

        total_pages = self._total_pages()

        prev_button = discord.ui.Button(label="◀ Previous", style=discord.ButtonStyle.primary, disabled=(self.page == 0))
        prev_button.callback = self._previous_page
        self.add_item(prev_button)

        next_button = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.primary, disabled=(self.page >= total_pages - 1))
        next_button.callback = self._next_page
        self.add_item(next_button)

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

    async def _back(self, interaction: discord.Interaction):
        self.parent_view.update_components()
        await interaction.response.edit_message(embed=self.parent_view.create_embed(), view=self.parent_view)


class _BestAndFairestTeamSelect(discord.ui.Select):
    def __init__(self, parent_view: _BestAndFairestView):
        self.parent_view = parent_view
        bot = parent_view.parent_view.bot
        options = build_team_options(
            bot, parent_view.parent_view.all_teams, selected=parent_view.team_id
        )
        super().__init__(placeholder="Switch club...", options=options)

    async def callback(self, interaction: discord.Interaction):
        team_id = int(self.values[0])
        team_name, emoji_id = next(
            ((name, eid) for tid, name, eid in self.parent_view.parent_view.all_teams if tid == team_id), (None, None)
        )
        self.parent_view.team_id = team_id
        self.parent_view.team_name = team_name
        self.parent_view.emoji_id = emoji_id
        self.parent_view.page = 0

        async with aiosqlite.connect(DB_PATH) as db:
            self.parent_view.totals = await _fetch_best_fairest_totals(db, self.parent_view.parent_view.season_id, team_id)

        self.parent_view.update_components()
        await interaction.response.edit_message(embed=self.parent_view.create_embed(), view=self.parent_view)


async def setup(bot):
    await bot.add_cog(AwardsCommands(bot))
