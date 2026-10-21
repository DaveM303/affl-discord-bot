"""/generatefixture - builds a season's regular-season fixture with
fixture_generator and saves it.

The admin picks a season, manages the league's marquee games, forced
double-ups and Stadium Settings (all saved once and reused every season),
then generates, previews round by round, re-rolls as often as they like,
and saves. Every input is saved as it's entered, so nothing is lost if the
menu times out.

Stadium Settings hold the grounds (name, capacity, state), each team's home
and alternate ground and crowd draw (1-5), each state's finals ground, the
Grand Final ground, and the blockbuster rating - see
fixture_generator.assign_grounds for how a game's ground is chosen, and
utils.finals_ground_name for finals.

Saving writes each round's games in play order (see
fixture_generator.assign_timeslots), so everything that lists matches by
match_id - /matchsimulation, Sim Full Round, /matchcentre - shows and
simulates them in timeslot order with no changes of its own. A season can
only be given a fixture before any of its games have been played; an
existing unplayed fixture is replaced.
"""
import asyncio
import random

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

import fixture_generator as fg
from config import DB_PATH, ADMIN_ROLE_ID
from utils import is_admin_user, build_team_options, fetch_teams_for_dropdown, get_team_emoji_str, add_page_buttons

PREVIEW_ROUNDS_PER_PAGE = 4
MENU_TIMEOUT_SECONDS = 900
NO_TIMESLOT = "none"
ANY_ROUND = "any"
MATCH_NAME_MAX_LENGTH = 40
NO_GROUND = "none"
# One dropdown holds 25 options, and ground dropdowns need one for "none".
MAX_GROUNDS = 24
CROWD_DRAWS = (1, 2, 3, 4, 5)
DEFAULT_CROWD_DRAW = 3
DEFAULT_BLOCKBUSTER_DRAW = 5
# Blockbuster rating meaning "no blockbusters" - no team is rated this high.
NO_BLOCKBUSTERS = 6


class FixtureContext:
    """Everything the menus need about one season's fixture setup, loaded
    fresh from the database each time something changes."""

    def __init__(self, bot, season_id, season_number, regular_rounds, teams, ladder_order, ladder_note,
                 derbies, marquees, unplayed_games, grounds=(), team_grounds=None, state_finals=None,
                 grand_final_ground=None, blockbuster_draw=DEFAULT_BLOCKBUSTER_DRAW, marquee_grounds=None,
                 random_hosts=()):
        self.bot = bot
        self.season_id = season_id
        self.season_number = season_number
        self.regular_rounds = regular_rounds
        self.teams = teams  # [(team_id, team_name, emoji_id)]
        self.names = {team_id: name for team_id, name, _ in teams}
        self.emojis = {team_id: emoji_id for team_id, _, emoji_id in teams}
        self.ladder_order = ladder_order  # team ids, 1st first, or None
        self.ladder_note = ladder_note
        self.derbies = derbies  # [(derby_id, team_a_id, team_b_id, name or None)]
        # [(marquee_id, round_number or None, home_id, away_id, timeslot or None, name or None)] -
        # round_number None is a marquee game with no set round (see build_fixture).
        self.marquees = marquees
        self.unplayed_games = unplayed_games  # regular-round games already saved for this season
        self.grounds = list(grounds)  # [(ground_id, name, capacity or None, state or None)]
        self.ground_names = {ground_id: name for ground_id, name, _, _ in self.grounds}
        # {team_id: (home_id, alternate_id, crowd_draw, alternate_games, alternate_opponents tuple)}
        self.team_grounds = team_grounds or {}
        self.state_finals = state_finals or {}  # {state: ground_id}
        self.grand_final_ground = grand_final_ground  # ground_id or None
        self.blockbuster_draw = blockbuster_draw
        self.marquee_grounds = marquee_grounds or {}  # {marquee_id: ground_id} - pinned grounds only
        self.random_hosts = set(random_hosts)  # marquee_ids whose host the generator picks

    @property
    def fresh_rounds(self):
        """Rounds where everyone plays everyone once - the only rounds a
        marquee game can go in (see fixture_generator)."""
        return len(self.teams) - 1

    @property
    def rematches(self):
        return self.regular_rounds - self.fresh_rounds

    def game_text(self, home_id, away_id):
        """Plain team names - for dropdown labels, which can't show custom emojis."""
        return f"{self.names.get(home_id, '?')} v {self.names.get(away_id, '?')}"

    def team_display(self, team_id):
        """The team's emoji, or its name if it has no emoji the bot can show."""
        emoji = get_team_emoji_str(self.bot, self.emojis.get(team_id), trailing_space=False)
        return emoji or self.names.get(team_id, "?")

    def game_display(self, home_id, away_id):
        """A game for embeds - emojis where possible (see team_display)."""
        return f"{self.team_display(home_id)} v {self.team_display(away_id)}"

    def derby_lines(self, display=True):
        game = self.game_display if display else self.game_text
        return [f"{game(a, b)}" + (name_tag(name, display) if name else "") for _, a, b, name in self.derbies]

    def marquee_lines(self, display=True):
        return [describe_marquee(self, row, display) for row in self.marquees]

    def marquee_game_of(self, rows):
        """{marquee_id: (round_number, home_id, away_id)} - which of a
        fixture's games each marquee game is: the pair's game in its set
        round, or with no set round, the pair's first meeting. Matched by
        pair, so a random-host game is found whoever ended up hosting."""
        first_meeting, in_round = {}, {}
        for r, home, away, *_ in rows:
            pair = frozenset((home, away))
            in_round[(r, pair)] = (r, home, away)
            if r < first_meeting.get(pair, (r + 1,))[0]:
                first_meeting[pair] = (r, home, away)
        games = {}
        for marquee_id, r, h, a, _, _ in self.marquees:
            pair = frozenset((h, a))
            game = in_round.get((r, pair)) if r is not None else first_meeting.get(pair)
            if game is not None:
                games[marquee_id] = game
        return games

    def marquee_games(self, rows):
        """{(round_number, home_id, away_id): name or None} - which of a
        fixture's games are marquee games (see marquee_game_of)."""
        names = {row[0]: row[5] for row in self.marquees}
        return {game: names[marquee_id] for marquee_id, game in self.marquee_game_of(rows).items()}

    def ground_name(self, ground_id):
        return self.ground_names.get(ground_id) if ground_id is not None else None

    def team_setup(self, team_id):
        """(home_id, alternate_id, crowd_draw, alternate_games, alternate_opponents) - defaults if unset."""
        return self.team_grounds.get(team_id, (None, None, DEFAULT_CROWD_DRAW, 0, CROWD_DRAWS))

    def teams_with_grounds(self):
        return sum(1 for team_id, _, _ in self.teams if self.team_setup(team_id)[0] is not None)

    def game_names(self, rows):
        """{(round_number, home_id, away_id): special name} - a marquee
        game's own name, else (on both meetings) its forced double-up's
        name. Unnamed games are left out."""
        names = {game: name for game, name in self.marquee_games(rows).items() if name}
        derby_names = {frozenset((a, b)): name for _, a, b, name in self.derbies if name}
        for r, home, away, *_ in rows:
            name = derby_names.get(frozenset((home, away)))
            if name and (r, home, away) not in names:
                names[(r, home, away)] = name
        return names


def round_label(round_number):
    return f"R{round_number}" if round_number else "Any round"


def describe_marquee(ctx, row, display=True, random_host=None):
    """random_host: for a game not saved yet - saved ones are looked up."""
    marquee_id, r, h, a, t, name = row
    game = ctx.game_display(h, a) if display else ctx.game_text(h, a)
    if random_host if random_host is not None else marquee_id in ctx.random_hosts:
        game += " (🎲 host)"
    ground = ctx.ground_name(ctx.marquee_grounds.get(marquee_id))
    return (f"{round_label(r)} · {game}" + (f" · {t}" if t else "") + (f" · @ {ground}" if ground else "")
            + (name_tag(name, display) if name else ""))


def draws_text(draws):
    """(4, 5) -> "4-5", (1, 3) -> "1, 3", all five -> "any"."""
    draws = sorted(draws)
    if tuple(draws) == CROWD_DRAWS:
        return "any"
    if draws == list(range(draws[0], draws[-1] + 1)) and len(draws) > 1:
        return f"{draws[0]}-{draws[-1]}"
    return ", ".join(str(d) for d in draws)


def parse_draws(text):
    draws = tuple(sorted({int(part) for part in (text or "").split(",") if part.strip().isdigit()} & set(CROWD_DRAWS)))
    return draws or CROWD_DRAWS


def name_tag(name, display=True):
    return f" · **{name}**" if display else f" · {name}"


def fit_field(display_lines, plain_lines, empty="*None*"):
    """Joins lines for an embed field within Discord's 1024-character cap.
    Uses the emoji version when it fits, else the plain-name version, else
    as many plain lines as fit plus an "and N more" line - never cutting a
    line (or an emoji code) in half."""
    if not display_lines:
        return empty
    for lines in (display_lines, plain_lines):
        text = "\n".join(lines)
        if len(text) <= 1024:
            return text
    kept = []
    for i, line in enumerate(plain_lines):
        more = f"… and {len(plain_lines) - i} more"
        if len("\n".join(kept + [line, more])) > 1024:
            return "\n".join(kept + [more])
        kept.append(line)
    return "\n".join(kept)


def list_section(name, display_lines, plain_lines, budget, empty="*None*"):
    """A bold heading and its list, for an embed's description - where
    there's room for a long list in one piece (4096 characters) with no
    gaps, unlike fields (see add_list_fields). Uses the emoji version if it
    fits within `budget` characters, else plain names, else as many plain
    lines as fit plus an "and N more" line. Lines are never split."""
    heading = f"**{name}**"
    if not display_lines:
        return f"{heading}\n{empty}"
    for lines in (display_lines, plain_lines):
        text = "\n".join([heading] + lines)
        if len(text) <= budget:
            return text
    kept = [heading]
    for i, line in enumerate(plain_lines):
        more = f"… and {len(plain_lines) - i} more"
        if len("\n".join(kept + [line, more])) > budget:
            return "\n".join(kept + [more])
        kept.append(line)
    return "\n".join(kept)


def add_list_fields(embed, name, display_lines, plain_lines, budget=4500, empty="*None*"):
    """Adds a list to an embed as one or more fields, splitting it across
    as many as it needs - each field holds at most 1024 characters, and a
    list like 38 marquee games is several times that. Uses the emoji
    version if the whole list fits within `budget` characters (the embed's
    6000-character total also covers its title, description and other
    fields), else plain names, else as many plain lines as fit plus an
    "and N more" line. Lines are never split."""
    if not display_lines:
        embed.add_field(name=name, value=empty, inline=False)
        return
    lines = display_lines if sum(len(line) + 1 for line in display_lines) <= budget else plain_lines
    if sum(len(line) + 1 for line in lines) > budget:
        kept, used = [], 0
        for line in lines:
            if used + len(line) + 1 > budget - 40:
                break
            kept.append(line)
            used += len(line) + 1
        lines = kept + [f"… and {len(lines) - len(kept)} more"]

    chunk, length, first = [], 0, True
    for line in lines:
        if chunk and length + len(line) + 1 > 1024:
            embed.add_field(name=name if first else "​", value="\n".join(chunk), inline=False)
            chunk, length, first = [], 0, False
        chunk.append(line)
        length += len(line) + 1
    embed.add_field(name=name if first else "​", value="\n".join(chunk), inline=False)


async def fetch_fixture_seasons(db):
    """Seasons that can be given a fixture: not finished, and with no game
    played yet. [(season_id, season_number, status)], earliest first."""
    cursor = await db.execute(
        """SELECT s.season_id, s.season_number, s.status FROM seasons s
           WHERE s.status != 'completed'
             AND NOT EXISTS (SELECT 1 FROM matches m WHERE m.season_id = s.season_id AND m.simulated = 1)
           ORDER BY s.season_number"""
    )
    return await cursor.fetchall()


async def load_context(db, season_id, bot):
    cursor = await db.execute(
        "SELECT season_number, regular_rounds FROM seasons WHERE season_id = ?", (season_id,)
    )
    season_number, regular_rounds = await cursor.fetchone()
    teams = await fetch_teams_for_dropdown(db)

    # Rematch weighting and prime time use the previous season's final
    # ladder (finals-adjusted - see season_commands.finalize_finals_ladder).
    ladder_order, ladder_note = None, None
    cursor = await db.execute(
        """SELECT lp.team_id FROM ladder_positions lp JOIN seasons s ON lp.season_id = s.season_id
           WHERE s.season_number = ? ORDER BY lp.position""",
        (season_number - 1,)
    )
    previous = [row[0] for row in await cursor.fetchall()]
    if not previous:
        ladder_note = f"No Season {season_number - 1} ladder, so rematches and prime time are random."
    elif sorted(previous) != sorted(team_id for team_id, _, _ in teams):
        ladder_note = (f"Season {season_number - 1}'s ladder doesn't match the current teams, "
                       "so rematches and prime time are random.")
    else:
        ladder_order = previous

    cursor = await db.execute("SELECT derby_id, team_a_id, team_b_id, name FROM fixture_derbies ORDER BY derby_id")
    derbies = await cursor.fetchall()
    cursor = await db.execute(
        """SELECT marquee_id, round_number, home_team_id, away_team_id, timeslot, name FROM fixture_marquees
           ORDER BY round_number IS NULL, round_number, marquee_id"""
    )
    marquees = await cursor.fetchall()
    cursor = await db.execute(
        "SELECT COUNT(*) FROM matches WHERE season_id = ? AND round_number <= ?", (season_id, regular_rounds)
    )
    unplayed_games = (await cursor.fetchone())[0]

    cursor = await db.execute("SELECT ground_id, name, capacity, state FROM grounds ORDER BY name COLLATE NOCASE")
    grounds = await cursor.fetchall()
    cursor = await db.execute(
        """SELECT team_id, home_ground_id, alternate_ground_id, crowd_draw, alternate_games, alternate_opponents
           FROM team_grounds"""
    )
    team_grounds = {
        team_id: (home, alternate, draw, alternate_games, parse_draws(opponents))
        for team_id, home, alternate, draw, alternate_games, opponents in await cursor.fetchall()
    }
    cursor = await db.execute("SELECT state, ground_id FROM state_finals_grounds")
    state_finals = dict(await cursor.fetchall())
    grand_final_ground = await get_int_setting(db, "grand_final_ground_id")
    blockbuster_draw = await get_int_setting(db, "blockbuster_draw") or DEFAULT_BLOCKBUSTER_DRAW
    cursor = await db.execute("SELECT marquee_id, ground_id FROM fixture_marquees WHERE ground_id IS NOT NULL")
    marquee_grounds = dict(await cursor.fetchall())
    cursor = await db.execute("SELECT marquee_id FROM fixture_marquees WHERE random_host = 1")
    random_hosts = [row[0] for row in await cursor.fetchall()]

    return FixtureContext(bot, season_id, season_number, regular_rounds, teams, ladder_order, ladder_note,
                          derbies, marquees, unplayed_games, grounds, team_grounds, state_finals,
                          grand_final_ground, blockbuster_draw, marquee_grounds, random_hosts)


async def get_int_setting(db, key):
    cursor = await db.execute("SELECT setting_value FROM settings WHERE setting_key = ?", (key,))
    row = await cursor.fetchone()
    try:
        return int(row[0]) if row and row[0] is not None else None
    except ValueError:
        return None


async def set_setting(db, key, value):
    if value is None:
        await db.execute("DELETE FROM settings WHERE setting_key = ?", (key,))
    else:
        await db.execute(
            "INSERT INTO settings (setting_key, setting_value) VALUES (?, ?) "
            "ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value",
            (key, str(value)),
        )


def build_fixture(ctx):
    """Runs the generator (CPU-bound, ~1s - call via asyncio.to_thread).
    Returns rows of (round_number, home_id, away_id, timeslot, ground_id or
    None), each round in play order. Raises
    fixture_generator.FixtureGenerationError."""
    rng = random.Random()
    capacity = {ground_id: capacity for ground_id, _, capacity, _ in ctx.grounds}
    team_grounds = {team_id: fg.TeamGrounds(*ctx.team_setup(team_id)) for team_id, _, _ in ctx.teams}

    def timeslots_and_grounds(fixture):
        # Run for each generated fixture: one whose grounds can't all fit
        # is rejected (FixtureRejected), and generate_fixture tries another.
        game_of = ctx.marquee_game_of(fixture)
        marquee_slots = {game_of[marquee_id]: timeslot for marquee_id, r, _, _, timeslot, _ in ctx.marquees
                         if timeslot and r and marquee_id in game_of}
        rows = fg.assign_timeslots(fixture, ladder_order=ctx.ladder_order, marquee_slots=marquee_slots, rng=rng)
        pinned_grounds = {game_of[marquee_id]: ground_id
                          for marquee_id, ground_id in ctx.marquee_grounds.items() if marquee_id in game_of}
        return fg.assign_grounds(rows, team_grounds, capacity, ctx.blockbuster_draw, pinned_grounds,
                                 pinned_slots=marquee_slots,
                                 home_ground_pairs=[(a, b) for _, a, b, _ in ctx.derbies], rng=rng)

    return fg.generate_fixture(
        [team_id for team_id, _, _ in ctx.teams], ctx.regular_rounds,
        marquees=[(r, home, away) for _, r, home, away, _, _ in ctx.marquees if r is not None],
        # A random-host game with no set round pins nothing - it's just the
        # pair's first meeting, named.
        home_games=[(home, away) for m, r, home, away, _, _ in ctx.marquees
                    if r is None and m not in ctx.random_hosts],
        random_hosts=[(home, away) for m, r, home, away, _, _ in ctx.marquees
                      if r is not None and m in ctx.random_hosts],
        guaranteed_doubleups=[(a, b) for _, a, b, _ in ctx.derbies],
        ladder_order=ctx.ladder_order,
        rng=rng,
        accept=timeslots_and_grounds,
    )


def derby_problem(ctx, team_a, team_b):
    """Why this forced double-up can't be added, or None."""
    if team_a == team_b:
        return "Pick two different teams."
    pair = {team_a, team_b}
    if any({a, b} == pair for _, a, b, _ in ctx.derbies):
        return f"{ctx.game_display(team_a, team_b)} is already a forced double-up."
    if ctx.rematches < 1:
        return f"Season {ctx.season_number} has no rematch rounds, so it can't have forced double-ups."
    for team in (team_a, team_b):
        count = sum(1 for _, a, b, _ in ctx.derbies if team in (a, b))
        if count >= ctx.rematches:
            return (f"{ctx.team_display(team)} already has {count} forced double-ups - that's every rematch "
                    f"it gets this season.")
    return None


def marquee_problem(ctx, round_number, home, away, timeslot, ignore_id=None):
    """Why this marquee game can't be added (or, with ignore_id, why the
    marquee game being edited can't be changed to this), or None."""
    if home == away:
        return "Pick two different teams."
    for marquee_id, r, h, a, _, _ in ctx.marquees:
        if marquee_id == ignore_id:
            continue
        if {h, a} == {home, away}:
            where = f"round {r}" if r else "any round"
            return (f"{ctx.game_display(h, a)} is already a marquee game ({where}). They only meet "
                    "once before the rematch rounds - make them a forced double-up to have them play twice.")
        if round_number is not None and r == round_number and (home in (h, a) or away in (h, a)):
            clash = home if home in (h, a) else away
            return f"{ctx.team_display(clash)} already has a marquee game in round {round_number}."
    if timeslot and round_number is None:
        return "Pick a round to give it a timeslot."
    if timeslot:
        pinned = [t for m, r, _, _, t, _ in ctx.marquees if r == round_number and t and m != ignore_id] + [timeslot]
        try:
            fg.check_round_timeslots(round_number, pinned)
        except fg.FixtureGenerationError as e:
            return str(e)
    if ignore_id is not None:
        return marquee_ground_problem(ctx, ignore_id, ctx.marquee_grounds.get(ignore_id), round_number, timeslot)
    return None


def marquee_ground_problem(ctx, marquee_id, ground_id, round_number, timeslot):
    """Why a marquee game can't be pinned to this ground, or None: another
    marquee game pinned to the same ground, round and an overlapping
    timeslot (see fixture_generator.slots_overlap)."""
    if ground_id is None or round_number is None or not timeslot:
        return None
    for other_id, r, h, a, t, _ in ctx.marquees:
        if (other_id != marquee_id and r == round_number and t and ctx.marquee_grounds.get(other_id) == ground_id
                and fg.slots_overlap(fg.timeslot_name(t), fg.timeslot_name(timeslot))):
            return (f"{ctx.game_display(h, a)} is already at {ctx.ground_name(ground_id)} in round {r} at "
                    f"an overlapping time ({t}).")
    return None


def menu_embed(ctx, error=None):
    lines = [f"**{ctx.regular_rounds} rounds, {len(ctx.teams)} teams.**"]
    if ctx.rematches > 0:
        lines.append(
            f"Rounds 1-{ctx.fresh_rounds}: everyone plays everyone once. "
            f"Rounds {ctx.fresh_rounds + 1}-{ctx.regular_rounds}: {ctx.rematches} rematches each."
        )
    lines.append(ctx.ladder_note or
                 f"Rematches and prime time weighted by Season {ctx.season_number - 1}'s final ladder.")
    if ctx.grounds:
        lines.append(f"Stadiums: {len(ctx.grounds)} grounds, {ctx.teams_with_grounds()}/{len(ctx.teams)} "
                     "teams have a home ground.")
    else:
        lines.append("Stadiums: none set up, so games get no ground - see **Stadium Settings**.")
    if ctx.unplayed_games:
        lines.append(f"\n:exclamation: This season already has a fixture ({ctx.unplayed_games} games, none "
                     "played). Saving a new one replaces it.")
    if error:
        lines.append(f"\n❌ **Couldn't generate a fixture:** {error}")

    # Both lists go in the description rather than fields: a list longer
    # than one field (1024 characters) would split across two, leaving a
    # blank gap mid-list.
    intro = "\n".join(lines)
    derbies = list_section(f"Forced double-ups ({len(ctx.derbies)})",
                           ctx.derby_lines(), ctx.derby_lines(display=False), budget=1500)
    marquees = list_section(f"Marquee games ({len(ctx.marquees)})",
                            ctx.marquee_lines(), ctx.marquee_lines(display=False),
                            budget=4096 - len(intro) - len(derbies) - 4)
    embed = discord.Embed(
        title=f"Fixture Generator - Season {ctx.season_number}",
        description=f"{intro}\n\n{derbies}\n\n{marquees}",
        color=discord.Color.red() if error else discord.Color.blurple(),
    )
    return embed


class FixtureCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if await is_admin_user(interaction):
            return True
        if ADMIN_ROLE_ID:
            await interaction.response.send_message("❌ You need the admin role to use this command.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ You need Administrator permissions to use this command.", ephemeral=True)
        return False

    @app_commands.command(name="generatefixture", description="[ADMIN] Generate and save a season's fixture")
    async def generate_fixture(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            seasons = await fetch_fixture_seasons(db)
            if not seasons:
                await interaction.response.send_message(
                    "❌ No season can be given a fixture - every season has either finished or "
                    "already started playing.",
                    ephemeral=True,
                )
                return
            ctx = await load_context(db, seasons[0][0], self.bot)
        menu = FixtureMenuView(self.bot, seasons, ctx)
        await interaction.response.send_message(embed=menu_embed(ctx), view=menu, ephemeral=True)


class FixtureMenuView(discord.ui.View):
    """The main menu: season picker, the saved inputs, and Generate."""

    def __init__(self, bot, seasons, ctx):
        super().__init__(timeout=MENU_TIMEOUT_SECONDS)
        self.bot = bot
        self.seasons = seasons
        self.ctx = ctx
        self.add_item(_SeasonSelect(self))

        derbies_btn = discord.ui.Button(label=f"Forced Double-Ups ({len(ctx.derbies)})", style=discord.ButtonStyle.secondary, row=1)
        derbies_btn.callback = self._derbies
        self.add_item(derbies_btn)

        marquees_btn = discord.ui.Button(label=f"Marquee Games ({len(ctx.marquees)})", style=discord.ButtonStyle.secondary, row=1)
        marquees_btn.callback = self._marquees
        self.add_item(marquees_btn)

        stadiums_btn = discord.ui.Button(label="🏟️ Stadium Settings", style=discord.ButtonStyle.secondary, row=1)
        stadiums_btn.callback = self._stadiums
        self.add_item(stadiums_btn)

        generate_btn = discord.ui.Button(label="🎲 Generate", style=discord.ButtonStyle.primary, row=1)
        generate_btn.callback = self._generate
        self.add_item(generate_btn)

    async def reopen(self, interaction, season_id=None):
        async with aiosqlite.connect(DB_PATH) as db:
            ctx = await load_context(db, season_id or self.ctx.season_id, self.bot)
        menu = FixtureMenuView(self.bot, self.seasons, ctx)
        await interaction.response.edit_message(content=None, embed=menu_embed(ctx), view=menu)

    async def _derbies(self, interaction):
        view = DerbiesView(self)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    async def _marquees(self, interaction):
        view = MarqueesView(self)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    async def _stadiums(self, interaction):
        view = StadiumSettingsView(self)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    async def _generate(self, interaction):
        await interaction.response.defer()
        async with aiosqlite.connect(DB_PATH) as db:
            ctx = await load_context(db, self.ctx.season_id, self.bot)
        try:
            rows = await asyncio.to_thread(build_fixture, ctx)
        except fg.FixtureGenerationError as e:
            await interaction.edit_original_response(content=None, embed=menu_embed(ctx, error=e), view=self)
            return
        preview = PreviewView(self, ctx, rows)
        await interaction.edit_original_response(content=None, embed=preview.embed(), view=preview)


class _SeasonSelect(discord.ui.Select):
    def __init__(self, menu):
        self.menu = menu
        options = [
            discord.SelectOption(
                label=f"Season {number}", description=status.capitalize(),
                value=str(season_id), default=season_id == menu.ctx.season_id,
            )
            for season_id, number, status in menu.seasons[:25]
        ]
        super().__init__(placeholder="Season...", options=options, row=0)

    async def callback(self, interaction):
        await self.menu.reopen(interaction, int(self.values[0]))


class DerbiesView(discord.ui.View):
    """Add or remove the league's forced double-ups - pairs that always play twice."""

    def __init__(self, menu, team_a=None, team_b=None):
        super().__init__(timeout=MENU_TIMEOUT_SECONDS)
        self.menu = menu
        self.ctx = menu.ctx
        self.team_a = team_a
        self.team_b = team_b

        self.add_item(_PickTeamSelect(self, "team_a", "First team...", row=0))
        self.add_item(_PickTeamSelect(self, "team_b", "Second team...", row=1))
        if self.ctx.derbies:
            self.add_item(_RemoveDerbySelect(self))

        if self.existing():
            add_btn = discord.ui.Button(label="Rename...", style=discord.ButtonStyle.primary, row=3)
            add_btn.callback = self._rename
        else:
            add_btn = discord.ui.Button(label="Add Forced Double-Up", style=discord.ButtonStyle.success, row=3,
                                        disabled=not (team_a and team_b))
            add_btn.callback = self._add
        self.add_item(add_btn)
        back_btn = discord.ui.Button(label="← Back", style=discord.ButtonStyle.secondary, row=3)
        back_btn.callback = self._back
        self.add_item(back_btn)

    def embed(self, note=None):
        embed = discord.Embed(
            title="Forced Double-Ups",
            description=(
                "These pairs play twice every season - once in rounds 1-"
                f"{self.ctx.fresh_rounds} and again in a rematch round (never the last one). "
                "An optional name goes on both games (a marquee game's own name wins on its game). "
                "Pick a pair that's already listed to rename it. Saved for every season."
                + (f"\n\n{note}" if note else "")
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name=f"Forced double-ups ({len(self.ctx.derbies)})",
            value=fit_field(self.ctx.derby_lines(), self.ctx.derby_lines(display=False)),
            inline=False,
        )
        return embed

    async def redraw(self, interaction, note=None, keep_picks=True):
        async with aiosqlite.connect(DB_PATH) as db:
            self.menu.ctx = await load_context(db, self.ctx.season_id, self.menu.bot)
        view = DerbiesView(self.menu, self.team_a if keep_picks else None, self.team_b if keep_picks else None)
        await interaction.response.edit_message(embed=view.embed(note), view=view)

    def existing(self):
        if not (self.team_a and self.team_b):
            return None
        return next((row for row in self.ctx.derbies if {row[1], row[2]} == {self.team_a, self.team_b}), None)

    async def _add(self, interaction):
        problem = derby_problem(self.ctx, self.team_a, self.team_b)
        if problem:
            await self.redraw(interaction, f":exclamation: {problem}")
            return
        await interaction.response.send_modal(NameModal(
            "Forced Double-Up Name", "Name (optional)", self._save, required=False,
            placeholder="e.g. Western Derby - leave blank for none",
        ))

    async def _save(self, interaction, name):
        a, b = sorted((self.team_a, self.team_b))
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO fixture_derbies (team_a_id, team_b_id, name) VALUES (?, ?, ?)",
                             (a, b, name or None))
            await db.commit()
        await self.redraw(interaction, f"✅ Added {self.ctx.game_display(a, b)}"
                          + (name_tag(name) if name else "") + ".", keep_picks=False)

    async def _rename(self, interaction):
        await interaction.response.send_modal(NameModal(
            "Rename Forced Double-Up", "Name (optional)", self._save_name, required=False,
            default=self.existing()[3], placeholder="Leave blank to remove the name",
        ))

    async def _save_name(self, interaction, name):
        derby_id, a, b, _ = self.existing()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE fixture_derbies SET name = ? WHERE derby_id = ?", (name or None, derby_id))
            await db.commit()
        game = self.ctx.game_display(a, b)
        note = f"✅ Renamed {game}{name_tag(name)}." if name else f"✅ Removed {game}'s name."
        await self.redraw(interaction, note, keep_picks=False)

    async def picked(self, interaction):
        await self.redraw(interaction)

    async def _back(self, interaction):
        await self.menu.reopen(interaction)


class _RemoveDerbySelect(discord.ui.Select):
    def __init__(self, view):
        self.parent_view = view
        options = [
            discord.SelectOption(label=(view.ctx.game_text(a, b) + (f" · {name}" if name else ""))[:100],
                                 value=str(derby_id))
            for derby_id, a, b, name in view.ctx.derbies[:25]
        ]
        super().__init__(placeholder="Remove a forced double-up...", options=options, row=2)

    async def callback(self, interaction):
        derby_id = int(self.values[0])
        ctx = self.parent_view.ctx
        removed = next(ctx.game_display(a, b) for d, a, b, _ in ctx.derbies if d == derby_id)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM fixture_derbies WHERE derby_id = ?", (derby_id,))
            await db.commit()
        await self.parent_view.redraw(interaction, f"Removed {removed}.")


class MarqueesView(discord.ui.View):
    """Add marquee games, each pinned to a round and optionally a timeslot -
    or, with editing set to a marquee_id, change or delete that one using
    the same dropdowns, pre-filled with its current details."""

    def __init__(self, menu, round_number=None, home=None, away=None, timeslot=None, editing=None,
                 random_host=False):
        super().__init__(timeout=MENU_TIMEOUT_SECONDS)
        self.menu = menu
        self.ctx = menu.ctx
        self.round_number = round_number
        self.home = home
        self.away = away
        self.timeslot = timeslot
        self.random_host = random_host
        # Dropped back to adding if the game being edited no longer exists.
        self.editing = editing if any(row[0] == editing for row in self.ctx.marquees) else None

        self.add_item(_MarqueeRoundSelect(self))
        self.add_item(_PickTeamSelect(self, "home", "First team..." if random_host else "Home team...", row=1))
        self.add_item(_PickTeamSelect(self, "away", "Second team..." if random_host else "Away team...", row=2))
        self.add_item(_MarqueeTimeslotSelect(self))

        picks_complete = bool(home and away)
        host_button = ("🎲 Host: Random" if random_host else "🏠 Host: Set", discord.ButtonStyle.secondary,
                       self._toggle_host, False)
        if self.editing is None:
            buttons = [
                ("Add Marquee Game", discord.ButtonStyle.success, self._add, not picks_complete),
                host_button,
                ("Edit...", discord.ButtonStyle.primary, self._choose_edit, not self.ctx.marquees),
                ("← Back", discord.ButtonStyle.secondary, self._back, False),
            ]
        else:
            buttons = [
                ("Rename...", discord.ButtonStyle.primary, self._rename, False),
                host_button,
                ("🏟️ Ground...", discord.ButtonStyle.primary, self._ground, not self.ctx.grounds),
                ("Delete", discord.ButtonStyle.danger, self._delete, False),
                ("← Back", discord.ButtonStyle.secondary, self._done, False),
            ]
        for label, style, callback, disabled in buttons:
            button = discord.ui.Button(label=label, style=style, row=4, disabled=disabled)
            button.callback = callback
            self.add_item(button)

    def editing_row(self):
        return next((row for row in self.ctx.marquees if row[0] == self.editing), None)

    def describe(self, row, display=True):
        return describe_marquee(self.ctx, row, display)

    def embed(self, note=None):
        if self.editing is not None:
            description = (
                f"Editing: {self.describe(self.editing_row())}\n\nChanges save as soon as you pick "
                "them. **Host** switches between a set home team and a random one, **Rename...** changes "
                "its name, **Ground...** pins it to a ground (otherwise it goes wherever the home team's "
                "games normally would), and **Delete** removes it."
            )
            title = "Edit Marquee Game"
        else:
            description = (
                "Pick the two teams and the home team - or switch **Host** to 🎲 Random and the generator "
                "picks which team hosts it each season. Leave the round as **Any round** and the "
                f"generator puts their first meeting somewhere in rounds 1-{self.ctx.fresh_rounds}; "
                "pick a round to pin it there, and optionally a timeslot. Any day works - up to two "
                "games per round can be outside the standard timeslots: the first replaces a Saturday "
                "afternoon game, the second a Sunday afternoon game. Saved for every season."
            )
            title = "Marquee Games"
        intro = description + (f"\n\n{note}" if note else "")
        marquees = list_section(f"Marquee games ({len(self.ctx.marquees)})",
                                self.ctx.marquee_lines(), self.ctx.marquee_lines(display=False),
                                budget=4096 - len(intro) - 2)
        embed = discord.Embed(
            title=title,
            description=f"{intro}\n\n{marquees}",
            color=discord.Color.blurple(),
        )
        return embed

    async def redraw(self, interaction, note=None, keep_picks=True):
        async with aiosqlite.connect(DB_PATH) as db:
            self.menu.ctx = await load_context(db, self.ctx.season_id, self.menu.bot)
        if keep_picks:
            view = MarqueesView(self.menu, self.round_number, self.home, self.away, self.timeslot, self.editing,
                                self.random_host)
        else:
            view = MarqueesView(self.menu)
        await interaction.response.edit_message(embed=view.embed(note), view=view)

    async def _add(self, interaction):
        problem = marquee_problem(self.ctx, self.round_number, self.home, self.away, self.timeslot)
        if problem:
            await self.redraw(interaction, f":exclamation: {problem}")
            return
        await interaction.response.send_modal(NameModal(
            "Marquee Game Name", "Name (optional)", self._save, required=False,
            placeholder="e.g. Anzac Day Clash - leave blank for none",
        ))

    async def _rename(self, interaction):
        await interaction.response.send_modal(NameModal(
            "Rename Marquee Game", "Name (optional)", self._save_name, required=False,
            default=self.editing_row()[5], placeholder="Leave blank to remove the name",
        ))

    async def _save_name(self, interaction, name):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE fixture_marquees SET name = ? WHERE marquee_id = ?", (name or None, self.editing))
            await db.commit()
        game = self.ctx.game_display(self.editing_row()[2], self.editing_row()[3])
        note = f"✅ Renamed {game}{name_tag(name)}." if name else f"✅ Removed {game}'s name."
        await self.redraw(interaction, note)

    async def _save(self, interaction, name):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT INTO fixture_marquees (round_number, home_team_id, away_team_id, timeslot, name, random_host)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (self.round_number, self.home, self.away, self.timeslot, name or None, int(self.random_host)),
            )
            await db.commit()
        saved = describe_marquee(self.ctx, (None, self.round_number, self.home, self.away, self.timeslot, name or None),
                                 random_host=self.random_host)
        await self.redraw(interaction, f"✅ Added {saved}.", keep_picks=False)

    async def picked(self, interaction):
        """A dropdown changed. When adding, just remember it; when editing,
        save it straight away - or refuse it and keep the saved value."""
        if self.round_number is None:
            self.timeslot = None  # a timeslot needs a round
        if self.editing is None:
            await self.redraw(interaction)
            return
        _, saved_round, saved_home, saved_away, saved_timeslot, _ = self.editing_row()
        # Picking the other team for home (or away) swaps the two, so home
        # and away can be switched without passing through "same team twice".
        if self.home == self.away:
            if self.home != saved_home:
                self.away = saved_home
            else:
                self.home = saved_away
        problem = marquee_problem(self.ctx, self.round_number, self.home, self.away, self.timeslot,
                                  ignore_id=self.editing)
        if problem:
            self.round_number, self.home, self.away, self.timeslot = (
                saved_round, saved_home, saved_away, saved_timeslot)
            await self.redraw(interaction, f":exclamation: {problem} Not saved.")
            return
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """UPDATE fixture_marquees
                   SET round_number = ?, home_team_id = ?, away_team_id = ?, timeslot = ?
                   WHERE marquee_id = ?""",
                (self.round_number, self.home, self.away, self.timeslot, self.editing),
            )
            await db.commit()
        await self.redraw(interaction, "✅ Saved.")

    async def _toggle_host(self, interaction):
        """Adding: just flips the choice. Editing: saves it straight away."""
        self.random_host = not self.random_host
        if self.editing is None:
            await self.redraw(interaction)
            return
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE fixture_marquees SET random_host = ? WHERE marquee_id = ?",
                             (int(self.random_host), self.editing))
            await db.commit()
        await self.redraw(interaction, "✅ Saved - " + (
            "the generator picks the host." if self.random_host else
            f"{self.ctx.team_display(self.home)} hosts it."))

    async def _ground(self, interaction):
        view = MarqueeGroundView(self)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    async def _choose_edit(self, interaction):
        await interaction.response.edit_message(embed=self.embed(), view=ChooseMarqueeToEditView(self))

    async def _delete(self, interaction):
        """Deletes the game, then goes back to the Edit... list so the next
        one can be picked straight away - or to the marquee screen if there
        are none left to edit."""
        deleted = self.describe(self.editing_row())
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM fixture_marquees WHERE marquee_id = ?", (self.editing,))
            await db.commit()
            self.menu.ctx = await load_context(db, self.ctx.season_id, self.menu.bot)
        marquees_view = MarqueesView(self.menu)
        view = ChooseMarqueeToEditView(marquees_view) if self.menu.ctx.marquees else marquees_view
        await interaction.response.edit_message(embed=marquees_view.embed(f"Deleted {deleted}."), view=view)

    async def _done(self, interaction):
        """Back to the Edit... list, on the page holding this game, so the
        next one can be picked straight away."""
        async with aiosqlite.connect(DB_PATH) as db:
            self.menu.ctx = await load_context(db, self.ctx.season_id, self.menu.bot)
        marquees_view = MarqueesView(self.menu)
        ids = [row[0] for row in self.menu.ctx.marquees]
        page = ids.index(self.editing) // 25 if self.editing in ids else 0
        await interaction.response.edit_message(embed=marquees_view.embed(),
                                                view=ChooseMarqueeToEditView(marquees_view, page))

    async def _back(self, interaction):
        await self.menu.reopen(interaction)


class NameModal(discord.ui.Modal):
    """A one-box text prompt; hands the trimmed text to on_submit(interaction, text)."""

    def __init__(self, title, label, on_submit, default=None, required=True, placeholder=None):
        super().__init__(title=title)
        self.text = discord.ui.TextInput(
            label=label, default=default, required=required, placeholder=placeholder,
            max_length=MATCH_NAME_MAX_LENGTH,
        )
        self.add_item(self.text)
        self._on_submit = on_submit

    async def on_submit(self, interaction):
        await self._on_submit(interaction, self.text.value.strip())


class _MarqueeRoundSelect(discord.ui.Select):
    def __init__(self, view):
        self.parent_view = view
        options = [discord.SelectOption(label="Any round", value=ANY_ROUND, default=view.round_number is None)]
        options += [
            discord.SelectOption(label=f"Round {r}", value=str(r), default=r == view.round_number)
            for r in range(1, min(view.ctx.fresh_rounds, 24) + 1)
        ]
        super().__init__(placeholder="Round...", options=options, row=0)

    async def callback(self, interaction):
        value = self.values[0]
        self.parent_view.round_number = None if value == ANY_ROUND else int(value)
        await self.parent_view.picked(interaction)


class _MarqueeTimeslotSelect(discord.ui.Select):
    def __init__(self, view):
        self.parent_view = view
        options = [discord.SelectOption(label="No set timeslot", value=NO_TIMESLOT, default=view.timeslot is None)]
        for day in fg.DAYS:
            for time_of_day in fg.TIMES_OF_DAY:
                name = f"{day} {time_of_day}"
                options.append(discord.SelectOption(label=name, value=name, default=view.timeslot == name))
        no_round = view.round_number is None
        super().__init__(
            placeholder="Timeslot - pick a round first" if no_round else "Timeslot (optional)...",
            options=options[:25], row=3, disabled=no_round,
        )

    async def callback(self, interaction):
        value = self.values[0]
        self.parent_view.timeslot = None if value == NO_TIMESLOT else value
        await self.parent_view.picked(interaction)


class _PickTeamSelect(discord.ui.Select):
    """A team dropdown that stores its pick on the parent view under `attr`."""

    def __init__(self, view, attr, placeholder, row):
        self.parent_view = view
        self.attr = attr
        options = build_team_options(view.menu.bot, view.ctx.teams, selected=getattr(view, attr))
        super().__init__(placeholder=placeholder, options=options, row=row)

    async def callback(self, interaction):
        setattr(self.parent_view, self.attr, int(self.values[0]))
        await self.parent_view.picked(interaction)


class ChooseMarqueeToEditView(discord.ui.View):
    """Picks which marquee game to edit, then opens it in MarqueesView.
    A dropdown holds at most 25 options, so longer lists are paged."""

    def __init__(self, marquees_view, page=0):
        super().__init__(timeout=MENU_TIMEOUT_SECONDS)
        self.marquees_view = marquees_view
        self.page = page
        marquees = marquees_view.ctx.marquees
        pages = max(1, -(-len(marquees) // 25))
        shown = marquees[page * 25:(page + 1) * 25]
        self.select = discord.ui.Select(
            placeholder="Choose a marquee game to edit..." + (f" (page {page + 1}/{pages})" if pages > 1 else ""),
            options=[
                discord.SelectOption(label=marquees_view.describe(row, display=False)[:100], value=str(row[0]))
                for row in shown
            ],
        )
        self.select.callback = self._chosen
        self.add_item(self.select)
        add_page_buttons(self, page, pages, self._go_to_page, row=1)
        back_btn = discord.ui.Button(label="← Back", style=discord.ButtonStyle.secondary, row=1)
        back_btn.callback = self._back
        self.add_item(back_btn)

    async def _go_to_page(self, interaction, page):
        await interaction.response.edit_message(view=ChooseMarqueeToEditView(self.marquees_view, page))

    async def _chosen(self, interaction):
        marquee_id, r, h, a, t, _ = next(
            row for row in self.marquees_view.ctx.marquees if row[0] == int(self.select.values[0])
        )
        view = MarqueesView(self.marquees_view.menu, r, h, a, t, editing=marquee_id,
                            random_host=marquee_id in self.marquees_view.ctx.random_hosts)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    async def _back(self, interaction):
        await self.marquees_view.redraw(interaction)


class PreviewView(discord.ui.View):
    """A generated fixture, a few rounds per page, with Re-roll and Save."""

    def __init__(self, menu, ctx, rows, page=0):
        super().__init__(timeout=MENU_TIMEOUT_SECONDS)
        self.menu = menu
        self.ctx = ctx
        self.rows = rows
        self.page = page
        self.rounds = sorted({row[0] for row in rows})
        self.pages = max(1, -(-len(self.rounds) // PREVIEW_ROUNDS_PER_PAGE))
        self.names = ctx.game_names(rows)

        add_page_buttons(self, page, self.pages, self._turn, row=0)
        reroll_btn = discord.ui.Button(label="🎲 Re-roll", style=discord.ButtonStyle.primary, row=1)
        reroll_btn.callback = self._reroll
        self.add_item(reroll_btn)
        save_label = "✅ Save (replaces current)" if ctx.unplayed_games else "✅ Save Fixture"
        save_btn = discord.ui.Button(label=save_label, style=discord.ButtonStyle.success, row=1)
        save_btn.callback = self._save
        self.add_item(save_btn)
        back_btn = discord.ui.Button(label="← Back", style=discord.ButtonStyle.secondary, row=1)
        back_btn.callback = self._back
        self.add_item(back_btn)

    def embed(self, error=None):
        embed = discord.Embed(
            title=f"Season {self.ctx.season_number} Fixture - Preview",
            description=(
                f"Page {self.page + 1}/{self.pages}. Nothing is saved until you press Save - "
                "Re-roll for a different fixture."
                + (f"\n\n❌ **Re-roll failed** - this is still the previous fixture: {error}" if error else "")
            ),
            color=discord.Color.red() if error else discord.Color.gold(),
        )
        marquee_games = set(self.ctx.marquee_games(self.rows))
        derby_pairs = {frozenset((a, b)) for _, a, b, _ in self.ctx.derbies}
        start = self.page * PREVIEW_ROUNDS_PER_PAGE
        for round_number in self.rounds[start:start + PREVIEW_ROUNDS_PER_PAGE]:
            display_lines, plain_lines = [], []
            for r, home, away, timeslot, ground_id in self.rows:
                if r != round_number:
                    continue
                tag = ""
                if (r, home, away) in marquee_games:
                    tag += " ⭐"
                if frozenset((home, away)) in derby_pairs:
                    tag += " 🔥"
                slot = f"`{fg.short_timeslot(timeslot):<13}`"
                game_name = self.names.get((r, home, away))
                named_display = name_tag(game_name) if game_name else ""
                named_plain = name_tag(game_name, display=False) if game_name else ""
                ground = self.ctx.ground_name(ground_id)
                at = f" · {ground}" if ground else ""
                display_lines.append(f"{slot} {self.ctx.game_display(home, away)}{at}{tag}{named_display}")
                plain_lines.append(f"{slot} {self.ctx.game_text(home, away)}{at}{tag}{named_plain}")
            name = f"Round {round_number}"
            if round_number > self.ctx.fresh_rounds:
                name += " · rematches"
            embed.add_field(name=name, value=fit_field(display_lines, plain_lines), inline=False)
        embed.set_footer(text="⭐ marquee game · 🔥 forced double-up")
        return embed

    async def _turn(self, interaction, page):
        view = PreviewView(self.menu, self.ctx, self.rows, page)
        await interaction.response.edit_message(embed=view.embed(), view=view)


    async def _reroll(self, interaction):
        await interaction.response.defer()
        try:
            rows = await asyncio.to_thread(build_fixture, self.ctx)
        except fg.FixtureGenerationError as e:
            await interaction.edit_original_response(content=None, embed=self.embed(error=e), view=self)
            return
        view = PreviewView(self.menu, self.ctx, rows, self.page)
        await interaction.edit_original_response(content=None, embed=view.embed(), view=view)

    async def _save(self, interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM matches WHERE season_id = ? AND simulated = 1", (self.ctx.season_id,)
            )
            if (await cursor.fetchone())[0]:
                embed = discord.Embed(
                    title="❌ Fixture Not Saved",
                    description=(f"Season {self.ctx.season_number} has started playing since this was "
                                 "generated, so its fixture can't be replaced now."),
                    color=discord.Color.red(),
                )
                await interaction.response.edit_message(content=None, embed=embed, view=BackToMenuView(self.menu))
                return
            await db.execute(
                "DELETE FROM matches WHERE season_id = ? AND round_number <= ?",
                (self.ctx.season_id, self.ctx.regular_rounds),
            )
            # Inserted in play order: match_id order is timeslot order (see module docstring).
            await db.executemany(
                """INSERT INTO matches (season_id, round_number, home_team_id, away_team_id,
                                        home_score, away_score, simulated, timeslot, match_name, ground)
                   VALUES (?, ?, ?, ?, 0, 0, 0, ?, ?, ?)""",
                [(self.ctx.season_id, r, home, away, timeslot, self.names.get((r, home, away)),
                  self.ctx.ground_name(ground_id))
                 for r, home, away, timeslot, ground_id in self.rows],
            )
            await db.commit()

        replaced = (f"\nIt replaced the previous unplayed fixture ({self.ctx.unplayed_games} games)."
                    if self.ctx.unplayed_games else "")
        embed = discord.Embed(
            title="✅ Fixture Saved",
            description=(f"Season {self.ctx.season_number}'s fixture is saved - {len(self.rows)} games over "
                         f"{len(self.rounds)} rounds.{replaced}"),
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(content=None, embed=embed, view=BackToMenuView(self.menu))

    async def _back(self, interaction):
        await self.menu.reopen(interaction)


class BackToMenuView(discord.ui.View):
    def __init__(self, menu):
        super().__init__(timeout=MENU_TIMEOUT_SECONDS)
        self.menu = menu

    @discord.ui.button(label="← Back to Menu", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.menu.reopen(interaction)


def ground_options(ctx, selected, none_label):
    """Dropdown options: a "none" choice, then every ground."""
    options = [discord.SelectOption(label=none_label, value=NO_GROUND, default=selected is None)]
    for ground_id, name, capacity, state in ctx.grounds[:MAX_GROUNDS]:
        details = " · ".join(part for part in (f"{capacity:,}" if capacity else None, state) if part)
        options.append(discord.SelectOption(label=name[:100], value=str(ground_id), description=details or None,
                                            default=ground_id == selected))
    return options


def picked_ground(value):
    return None if value == NO_GROUND else int(value)


class StadiumSettingsView(discord.ui.View):
    """Grounds, each team's ground setup, and the league-wide ground
    settings. Saved for every season."""

    def __init__(self, menu):
        super().__init__(timeout=MENU_TIMEOUT_SECONDS)
        self.menu = menu
        self.ctx = menu.ctx

        team_select = discord.ui.Select(
            placeholder="Edit a team's grounds...",
            options=build_team_options(menu.bot, self.ctx.teams), row=0,
        )
        team_select.callback = self._team
        self.add_item(team_select)
        if self.ctx.grounds:
            ground_select = discord.ui.Select(
                placeholder="Edit a ground...", options=ground_options(self.ctx, None, "-")[1:], row=1,
            )
            ground_select.callback = self._ground
            self.add_item(ground_select)

        for label, style, callback, disabled in (
            ("Add Ground", discord.ButtonStyle.success, self._add, len(self.ctx.grounds) >= MAX_GROUNDS),
            ("League Settings", discord.ButtonStyle.primary, self._league, False),
            ("← Back", discord.ButtonStyle.secondary, self._back, False),
        ):
            button = discord.ui.Button(label=label, style=style, row=2, disabled=disabled)
            button.callback = callback
            self.add_item(button)

    def embed(self, note=None):
        ctx = self.ctx
        blockbusters = ("off" if ctx.blockbuster_draw >= NO_BLOCKBUSTERS
                        else f"both teams rated {ctx.blockbuster_draw}+")
        finals = ", ".join(f"{state}: {ctx.ground_name(g)}" for state, g in sorted(ctx.state_finals.items())) or "none"
        embed = discord.Embed(
            title="🏟️ Stadium Settings",
            description=(
                "Each team plays at its home ground, except:\n"
                "• **Forced double-ups** are always at the home team's home ground.\n"
                "• **Blockbusters** go to the bigger of the home team's two grounds.\n"
                "• A team's **alternate ground** hosts the set number of its home games each season, "
                "against opponents of the crowd draws you allow - on top of any blockbusters there.\n"
                "• A ground can host an afternoon and a night game on the same day. Any other **clash** "
                "is fixed by swapping timeslots (never Thursday or Friday night).\n\n"
                f"Grand Final: **{ctx.ground_name(ctx.grand_final_ground) or 'no set ground'}** · "
                f"Blockbusters: **{blockbusters}** · State finals grounds: **{finals}**"
                + (f"\n\n{note}" if note else "")
            ),
            color=discord.Color.blurple(),
        )
        ground_lines = []
        for ground_id, name, capacity, state in ctx.grounds:
            parts = [f"**{name}**", f"{capacity:,}" if capacity else "no capacity", state or "no state"]
            ground_lines.append(" · ".join(parts))
        add_list_fields(embed, f"Grounds ({len(ctx.grounds)})", ground_lines, ground_lines, budget=1500)
        team_display, team_plain = [], []
        for team_id, name, _ in ctx.teams:
            line = self.team_line(team_id)
            team_display.append(f"{ctx.team_display(team_id)} {line}")
            team_plain.append(f"{name}: {line}")
        add_list_fields(embed, "Teams", team_display, team_plain, budget=2800)
        return embed

    def team_line(self, team_id):
        ctx = self.ctx
        home, alternate, draw, alternate_games, opponents = ctx.team_setup(team_id)
        line = f"{ctx.ground_name(home) or '*no home ground*'}"
        if alternate is not None:
            line += f" / {ctx.ground_name(alternate)} ({alternate_games} v {draws_text(opponents)})"
        return f"{line} · draw {draw}"

    async def redraw(self, interaction, note=None):
        async with aiosqlite.connect(DB_PATH) as db:
            self.menu.ctx = await load_context(db, self.ctx.season_id, self.menu.bot)
        view = StadiumSettingsView(self.menu)
        await interaction.response.edit_message(embed=view.embed(note), view=view)

    async def _team(self, interaction):
        view = TeamGroundsView(self.menu, int(interaction.data["values"][0]))
        await interaction.response.edit_message(embed=view.embed(), view=view)

    async def _ground(self, interaction):
        view = GroundView(self.menu, int(interaction.data["values"][0]))
        await interaction.response.edit_message(embed=view.embed(), view=view)

    async def _add(self, interaction):
        await interaction.response.send_modal(GroundModal(self, None))

    async def _league(self, interaction):
        view = LeagueSettingsView(self.menu)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    async def _back(self, interaction):
        await self.menu.reopen(interaction)


class GroundModal(discord.ui.Modal):
    """Adds a ground (ground_id None) or edits one's name, capacity and state."""

    def __init__(self, stadiums_view, ground_id):
        super().__init__(title="Edit Ground" if ground_id else "Add Ground")
        self.stadiums_view = stadiums_view
        self.ground_id = ground_id
        current = next((row for row in stadiums_view.ctx.grounds if row[0] == ground_id), (None, None, None, None))
        self.name = discord.ui.TextInput(label="Name", default=current[1], max_length=40,
                                         placeholder="e.g. MCG")
        self.capacity = discord.ui.TextInput(label="Capacity (optional)", required=False, max_length=7,
                                             default=str(current[2]) if current[2] else None,
                                             placeholder="e.g. 100000 - picks a blockbuster's ground")
        self.state = discord.ui.TextInput(label="State (optional)", required=False, max_length=10,
                                          default=current[3], placeholder="e.g. VIC - used for finals")
        for item in (self.name, self.capacity, self.state):
            self.add_item(item)

    async def on_submit(self, interaction):
        view = self.stadiums_view
        name = self.name.value.strip()
        capacity_text = self.capacity.value.replace(",", "").strip()
        state = self.state.value.strip().upper() or None
        if capacity_text and not capacity_text.isdigit():
            await view.redraw(interaction, f":exclamation: Capacity must be a number, not \"{capacity_text}\". Not saved.")
            return
        capacity = int(capacity_text) if capacity_text else None
        if any(n.lower() == name.lower() and g != self.ground_id for g, n, _, _ in view.ctx.grounds):
            await view.redraw(interaction, f":exclamation: There's already a ground called {name}. Not saved.")
            return
        async with aiosqlite.connect(DB_PATH) as db:
            if self.ground_id is None:
                await db.execute("INSERT INTO grounds (name, capacity, state) VALUES (?, ?, ?)", (name, capacity, state))
            else:
                await db.execute("UPDATE grounds SET name = ?, capacity = ?, state = ? WHERE ground_id = ?",
                                 (name, capacity, state, self.ground_id))
            await db.commit()
        await view.redraw(interaction, f"✅ {'Saved' if self.ground_id else 'Added'} **{name}**.")


class GroundView(discord.ui.View):
    """One ground: edit its details or delete it."""

    def __init__(self, menu, ground_id):
        super().__init__(timeout=MENU_TIMEOUT_SECONDS)
        self.menu = menu
        self.ctx = menu.ctx
        self.ground_id = ground_id
        for label, style, callback in (
            ("Edit Details...", discord.ButtonStyle.primary, self._edit),
            ("Delete", discord.ButtonStyle.danger, self._delete),
            ("← Back", discord.ButtonStyle.secondary, self._back),
        ):
            button = discord.ui.Button(label=label, style=style)
            button.callback = callback
            self.add_item(button)

    def uses(self):
        """Everything that refers to this ground, as display lines."""
        ctx, ground_id = self.ctx, self.ground_id
        uses = []
        for team_id, _, _ in ctx.teams:
            home, alternate, *_ = ctx.team_setup(team_id)
            if home == ground_id:
                uses.append(f"{ctx.team_display(team_id)} home ground")
            if alternate == ground_id:
                uses.append(f"{ctx.team_display(team_id)} alternate ground")
        uses += [f"{state} finals ground" for state, g in ctx.state_finals.items() if g == ground_id]
        if ctx.grand_final_ground == ground_id:
            uses.append("Grand Final ground")
        uses += [f"Marquee game: {describe_marquee(ctx, row)}" for row in ctx.marquees
                 if ctx.marquee_grounds.get(row[0]) == ground_id]
        return uses

    def embed(self, note=None):
        _, name, capacity, state = next(row for row in self.ctx.grounds if row[0] == self.ground_id)
        embed = discord.Embed(
            title=f"🏟️ {name}",
            description=(f"Capacity: **{f'{capacity:,}' if capacity else 'not set'}** · "
                         f"State: **{state or 'not set'}**" + (f"\n\n{note}" if note else "")),
            color=discord.Color.blurple(),
        )
        uses = self.uses()
        add_list_fields(embed, "Used by", uses, uses, budget=1500, empty="*Nothing yet*")
        return embed

    async def _edit(self, interaction):
        await interaction.response.send_modal(GroundModal(StadiumSettingsView(self.menu), self.ground_id))

    async def _delete(self, interaction):
        if self.uses():
            await interaction.response.edit_message(
                embed=self.embed(":exclamation: Change everything listed under **Used by** to another "
                                 "ground first, then delete it."), view=self)
            return
        name = self.ctx.ground_name(self.ground_id)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM grounds WHERE ground_id = ?", (self.ground_id,))
            await db.commit()
        await StadiumSettingsView(self.menu).redraw(interaction, f"Deleted **{name}**.")

    async def _back(self, interaction):
        await StadiumSettingsView(self.menu).redraw(interaction)


class TeamGroundsView(discord.ui.View):
    """One team's home ground, alternate ground, crowd draw, and alternate-
    ground games. Every change saves straight away."""

    def __init__(self, menu, team_id):
        super().__init__(timeout=MENU_TIMEOUT_SECONDS)
        self.menu = menu
        self.ctx = menu.ctx
        self.team_id = team_id
        home, alternate, draw, alternate_games, opponents = self.ctx.team_setup(team_id)

        self._select(ground_options(self.ctx, home, "No home ground"), "Home ground...", 0, self._home)
        self._select(ground_options(self.ctx, alternate, "No alternate ground"), "Alternate ground...", 1,
                     self._alternate)
        self._select(
            [discord.SelectOption(label=f"Crowd draw: {d}" + {1: " (smallest)", 5: " (biggest)"}.get(d, ""),
                                  value=str(d), default=d == draw) for d in CROWD_DRAWS],
            "Crowd draw...", 2, self._draw,
        )
        self._select(
            [discord.SelectOption(label=f"v teams rated {d}", value=str(d), default=d in opponents)
             for d in CROWD_DRAWS],
            "Alternate-ground opponents..." if alternate else "Alternate-ground opponents - set an alternate first",
            3, self._opponents, disabled=alternate is None, min_values=1, max_values=len(CROWD_DRAWS),
        )
        games_btn = discord.ui.Button(label=f"Alternate-ground games: {alternate_games}...",
                                      style=discord.ButtonStyle.primary, row=4, disabled=alternate is None)
        games_btn.callback = self._games
        self.add_item(games_btn)
        back_btn = discord.ui.Button(label="← Back to Stadium Settings", style=discord.ButtonStyle.secondary, row=4)
        back_btn.callback = self._back
        self.add_item(back_btn)

    def _select(self, options, placeholder, row, callback, **kwargs):
        select = discord.ui.Select(placeholder=placeholder, options=options, row=row, **kwargs)
        select.callback = lambda interaction, select=select: callback(interaction, select.values)
        self.add_item(select)

    def embed(self, note=None):
        ctx = self.ctx
        home, alternate, draw, alternate_games, opponents = ctx.team_setup(self.team_id)
        lines = [
            f"Home ground: **{ctx.ground_name(home) or 'none'}**",
            f"Alternate ground: **{ctx.ground_name(alternate) or 'none'}**",
            f"Crowd draw: **{draw}** (1 smallest - 5 biggest)",
        ]
        if alternate is not None:
            lines.append(f"Alternate-ground games: **{alternate_games}** a season, against teams rated "
                         f"**{draws_text(opponents)}**")
        lines.append(
            "\nWith an alternate ground, this team's blockbusters (both teams rated "
            f"{ctx.blockbuster_draw}+) go to the bigger of its two grounds. Changes save as soon as you "
            "pick them."
        )
        if not ctx.grounds:
            lines.append("\n:exclamation: There are no grounds yet - add some in Stadium Settings first.")
        embed = discord.Embed(
            title=f"🏟️ {ctx.team_display(self.team_id)} {ctx.names.get(self.team_id, '?')}",
            description="\n".join(lines) + (f"\n\n{note}" if note else ""),
            color=discord.Color.blurple(),
        )
        return embed

    async def save(self, interaction, column, value, note="✅ Saved."):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR IGNORE INTO team_grounds (team_id) VALUES (?)", (self.team_id,))
            await db.execute(f"UPDATE team_grounds SET {column} = ? WHERE team_id = ?", (value, self.team_id))
            await db.commit()
            self.menu.ctx = await load_context(db, self.ctx.season_id, self.menu.bot)
        view = TeamGroundsView(self.menu, self.team_id)
        await interaction.response.edit_message(embed=view.embed(note), view=view)

    async def _home(self, interaction, values):
        ground_id = picked_ground(values[0])
        if ground_id is not None and ground_id == self.ctx.team_setup(self.team_id)[1]:
            await interaction.response.edit_message(
                embed=self.embed(":exclamation: That's already its alternate ground. Not saved."), view=self)
            return
        await self.save(interaction, "home_ground_id", ground_id)

    async def _alternate(self, interaction, values):
        ground_id = picked_ground(values[0])
        if ground_id is not None and ground_id == self.ctx.team_setup(self.team_id)[0]:
            await interaction.response.edit_message(
                embed=self.embed(":exclamation: That's already its home ground. Not saved."), view=self)
            return
        await self.save(interaction, "alternate_ground_id", ground_id)

    async def _draw(self, interaction, values):
        await self.save(interaction, "crowd_draw", int(values[0]))

    async def _opponents(self, interaction, values):
        await self.save(interaction, "alternate_opponents", ",".join(sorted(values)))

    async def _games(self, interaction):
        current = self.ctx.team_setup(self.team_id)[3]
        await interaction.response.send_modal(NumberModal(
            "Alternate-Ground Games", "Home games a season at the alternate ground", current, self._save_games,
        ))

    async def _save_games(self, interaction, number):
        await self.save(interaction, "alternate_games", number)

    async def _back(self, interaction):
        await StadiumSettingsView(self.menu).redraw(interaction)


class NumberModal(discord.ui.Modal):
    """A one-box whole-number prompt (0-99); hands it to on_submit(interaction, number)."""

    def __init__(self, title, label, default, on_submit):
        super().__init__(title=title)
        self.number = discord.ui.TextInput(label=label, default=str(default), max_length=2)
        self.add_item(self.number)
        self._on_submit = on_submit

    async def on_submit(self, interaction):
        text = self.number.value.strip()
        if not text.isdigit():
            await interaction.response.send_message(f"❌ \"{text}\" isn't a whole number.", ephemeral=True)
            return
        await self._on_submit(interaction, int(text))


class LeagueSettingsView(discord.ui.View):
    """Grand Final ground, blockbuster rating, and each state's finals ground."""

    def __init__(self, menu, state=None):
        super().__init__(timeout=MENU_TIMEOUT_SECONDS)
        self.menu = menu
        self.ctx = menu.ctx
        states = sorted({state for _, _, _, state in self.ctx.grounds if state})
        self.state = state if state in states else None

        gf = discord.ui.Select(placeholder="Grand Final ground...", row=0,
                               options=ground_options(self.ctx, self.ctx.grand_final_ground, "Grand Final: no set ground"))
        gf.callback = self._grand_final
        self.add_item(gf)

        blockbuster_options = [
            discord.SelectOption(label=f"Blockbusters: both teams rated {d}+", value=str(d),
                                 default=d == self.ctx.blockbuster_draw)
            for d in reversed(CROWD_DRAWS)
        ]
        blockbuster_options.append(discord.SelectOption(label="No blockbusters", value=str(NO_BLOCKBUSTERS),
                                                        default=self.ctx.blockbuster_draw >= NO_BLOCKBUSTERS))
        blockbuster = discord.ui.Select(placeholder="Blockbusters...", row=1, options=blockbuster_options)
        blockbuster.callback = self._blockbuster
        self.add_item(blockbuster)

        if states:
            state_select = discord.ui.Select(
                placeholder="Set a state's finals ground...", row=2,
                options=[
                    discord.SelectOption(
                        label=f"{s} finals: {self.ctx.ground_name(self.ctx.state_finals.get(s)) or 'host’s home ground'}"[:100],
                        value=s, default=s == self.state)
                    for s in states[:25]
                ],
            )
            state_select.callback = self._pick_state
            self.add_item(state_select)
        if self.state:
            finals = discord.ui.Select(
                placeholder=f"{self.state} finals ground...", row=3,
                options=ground_options(self.ctx, self.ctx.state_finals.get(self.state),
                                       f"{self.state} finals: host's home ground"),
            )
            finals.callback = self._finals_ground
            self.add_item(finals)

        back_btn = discord.ui.Button(label="← Back to Stadium Settings", style=discord.ButtonStyle.secondary, row=4)
        back_btn.callback = self._back
        self.add_item(back_btn)

    def embed(self, note=None):
        ctx = self.ctx
        lines = [
            f"**Grand Final:** {ctx.ground_name(ctx.grand_final_ground) or 'no set ground'}",
            "**Blockbusters:** " + ("off" if ctx.blockbuster_draw >= NO_BLOCKBUSTERS
                                    else f"both teams rated {ctx.blockbuster_draw}+ - played at the bigger "
                                         "of the home team's grounds"),
            "**Finals:** a final is played at its host's state finals ground if that state has one, "
            "otherwise at the host's home ground. A team's state is its home ground's state.",
        ]
        lines += [f"• {state}: {ctx.ground_name(g)}" for state, g in sorted(ctx.state_finals.items())]
        if not any(state for _, _, _, state in ctx.grounds):
            lines.append("\nGive grounds a state (Edit a ground) to set state finals grounds.")
        return discord.Embed(
            title="🏟️ League Settings",
            description="\n".join(lines) + (f"\n\n{note}" if note else ""),
            color=discord.Color.blurple(),
        )

    async def redraw(self, interaction, note=None):
        async with aiosqlite.connect(DB_PATH) as db:
            self.menu.ctx = await load_context(db, self.ctx.season_id, self.menu.bot)
        view = LeagueSettingsView(self.menu, self.state)
        await interaction.response.edit_message(embed=view.embed(note), view=view)

    async def _save_setting(self, interaction, key, value):
        async with aiosqlite.connect(DB_PATH) as db:
            await set_setting(db, key, value)
            await db.commit()
        await self.redraw(interaction, "✅ Saved.")

    async def _grand_final(self, interaction):
        await self._save_setting(interaction, "grand_final_ground_id", picked_ground(interaction.data["values"][0]))

    async def _blockbuster(self, interaction):
        await self._save_setting(interaction, "blockbuster_draw", int(interaction.data["values"][0]))

    async def _pick_state(self, interaction):
        self.state = interaction.data["values"][0]
        await self.redraw(interaction)

    async def _finals_ground(self, interaction):
        ground_id = picked_ground(interaction.data["values"][0])
        async with aiosqlite.connect(DB_PATH) as db:
            if ground_id is None:
                await db.execute("DELETE FROM state_finals_grounds WHERE state = ?", (self.state,))
            else:
                await db.execute(
                    "INSERT INTO state_finals_grounds (state, ground_id) VALUES (?, ?) "
                    "ON CONFLICT(state) DO UPDATE SET ground_id = excluded.ground_id",
                    (self.state, ground_id),
                )
            await db.commit()
        await self.redraw(interaction, "✅ Saved.")

    async def _back(self, interaction):
        await StadiumSettingsView(self.menu).redraw(interaction)


class MarqueeGroundView(discord.ui.View):
    """Pins the marquee game being edited to a ground, or unpins it."""

    def __init__(self, marquees_view):
        super().__init__(timeout=MENU_TIMEOUT_SECONDS)
        self.marquees_view = marquees_view
        self.ctx = marquees_view.ctx
        self.marquee_id = marquees_view.editing
        select = discord.ui.Select(
            placeholder="Ground...",
            options=ground_options(self.ctx, self.ctx.marquee_grounds.get(self.marquee_id), "Home team's usual ground"),
        )
        select.callback = self._picked
        self.add_item(select)
        back_btn = discord.ui.Button(label="← Back to Edit Marquee Game", style=discord.ButtonStyle.secondary, row=1)
        back_btn.callback = self._back
        self.add_item(back_btn)

    def embed(self, note=None):
        row = self.marquees_view.editing_row()
        return discord.Embed(
            title="Marquee Game Ground",
            description=(
                f"{describe_marquee(self.ctx, row)}\n\nPin it to a ground, e.g. an Anzac Day game at the MCG. "
                "A pinned game is never moved to sort out a clash. Leave it on **Home team's usual ground** "
                "to treat it like any other home game." + (f"\n\n{note}" if note else "")
            ),
            color=discord.Color.blurple(),
        )

    async def _picked(self, interaction):
        ground_id = picked_ground(interaction.data["values"][0])
        _, round_number, _, _, timeslot, _ = self.marquees_view.editing_row()
        problem = marquee_ground_problem(self.ctx, self.marquee_id, ground_id, round_number, timeslot)
        if problem:
            await interaction.response.edit_message(embed=self.embed(f":exclamation: {problem} Not saved."), view=self)
            return
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE fixture_marquees SET ground_id = ? WHERE marquee_id = ?", (ground_id, self.marquee_id))
            await db.commit()
            self.marquees_view.menu.ctx = await load_context(db, self.ctx.season_id, self.marquees_view.menu.bot)
        mv = self.marquees_view
        view = MarqueeGroundView(MarqueesView(mv.menu, mv.round_number, mv.home, mv.away, mv.timeslot, mv.editing,
                                              mv.random_host))
        await interaction.response.edit_message(embed=view.embed("✅ Saved."), view=view)

    async def _back(self, interaction):
        await self.marquees_view.redraw(interaction)


async def setup(bot):
    await bot.add_cog(FixtureCommands(bot))
