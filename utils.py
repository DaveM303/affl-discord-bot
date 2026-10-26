"""Utility functions for the AFFL Discord Bot"""

import aiosqlite
from config import DB_PATH, ADMIN_ROLE_ID


async def is_admin_user(interaction) -> bool:
    """
    Check if the interacting user has admin permissions, in order:
    1. Guild owner (always allowed)
    2. Has the configured ADMIN_ROLE_ID role (if one is configured)
    3. Has the Discord "Administrator" permission directly
    4. Has any role with the "Administrator" permission

    Does not send any response to the user - callers decide how to
    react to a False result (e.g. a cog-level interaction_check sending
    a denial message, or a button callback doing the same).

    Args:
        interaction: discord.Interaction

    Returns:
        bool: True if the user has admin permissions
    """
    if interaction.guild.owner_id == interaction.user.id:
        return True

    if ADMIN_ROLE_ID:
        member = interaction.guild.get_member(interaction.user.id) or interaction.user
        if member:
            admin_role_id = int(ADMIN_ROLE_ID) if isinstance(ADMIN_ROLE_ID, str) else ADMIN_ROLE_ID
            if any(role.id == admin_role_id for role in member.roles):
                return True
        return False

    try:
        if interaction.user.guild_permissions.administrator:
            return True
    except AttributeError:
        pass

    member = interaction.guild.get_member(interaction.user.id)
    if member:
        for role in member.roles:
            if role.permissions.administrator:
                return True

    return False


async def get_user_team(db, member):
    """
    Find the team a Discord member belongs to, based on which of their roles
    matches a team's configured role_id. Checks roles in the member's role
    order and returns the first match (mirrors the member's role priority).

    Args:
        db: Active aiosqlite database connection
        member: discord.Member (or discord.User with a .roles attribute)

    Returns:
        (team_id, team_name) tuple, or (None, None) if no role matches a team
    """
    cursor = await db.execute("SELECT team_id, team_name, role_id FROM teams WHERE role_id IS NOT NULL")
    teams_by_role_id = {role_id: (team_id, team_name) for team_id, team_name, role_id in await cursor.fetchall()}

    for role in member.roles:
        match = teams_by_role_id.get(str(role.id))
        if match:
            return match

    return None, None


async def get_current_season(db):
    """
    Get the current season number (prefers 'active', falls back to 'offseason',
    then the most recent season of any other status).

    Args:
        db: Active aiosqlite database connection

    Returns:
        int or None: Current season number, or None if no seasons exist
    """
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
    return season_result[0] if season_result else None


async def get_current_year(db):
    """
    Get the current calendar year based on current season and season_1_year setting.

    Args:
        db: Active aiosqlite database connection

    Returns:
        int: Current calendar year
    """
    current_season = await get_current_season(db)
    if current_season is None:
        return None

    # Get season_1_year setting
    cursor = await db.execute(
        "SELECT setting_value FROM settings WHERE setting_key = 'season_1_year'"
    )
    setting_result = await cursor.fetchone()
    if not setting_result:
        # Fallback: assume year equals season number
        return current_season

    season_1_year = int(setting_result[0])
    current_year = season_1_year + (current_season - 1)
    return current_year


def calculate_contract_expiry(start_season, contract_years):
    """
    Calculate the last season a contract covers.

    Args:
        start_season: The season the contract begins from (e.g. the draft's
            season_number for a rookie, or the current season for a re-sign)
        contract_years: Length of the contract in years

    Returns:
        int: The season number the contract expires after
    """
    return start_season + contract_years


async def assign_drafted_player(db, team_id, player_id, season_number, rookie_years):
    """
    Assign a newly-drafted player to a team and set their rookie contract_expiry.

    Args:
        db: Active aiosqlite database connection
        team_id: Team the player is being assigned to
        player_id: Player being assigned
        season_number: The draft's season_number. Regular end-of-season
            drafts store the season they feed INTO (e.g. the "Season 9
            National Draft" stores 10), so start_season + years already
            lands correctly there. Custom drafts always store 0 (they
            aren't tied to a season slot - see create_custom_draft), which
            isn't a real season to add years to.
        rookie_years: The draft's rookie_contract_years

    Returns:
        int: The contract_expiry that was assigned
    """
    if season_number:
        contract_expiry = calculate_contract_expiry(season_number, rookie_years)
    else:
        # Custom draft (season_number == 0): the pick happens mid-season,
        # against the currently active season, not a season boundary. A
        # 1-year contract is meant to cover the rest of THIS season and
        # nothing more, so the current season counts as year 1 - unlike an
        # end-of-season draft/re-sign, where the just-finished season is
        # never counted (see calculate_contract_expiry's docstring).
        cursor = await db.execute(
            "SELECT season_number FROM seasons WHERE status = 'active' LIMIT 1"
        )
        row = await cursor.fetchone()
        current_season = row[0] if row else None
        if current_season is None:
            # No active season to anchor to (shouldn't happen - custom
            # drafts require one to even be created) - fall back to the
            # old, wrong-but-non-crashing behaviour rather than raising.
            contract_expiry = calculate_contract_expiry(season_number, rookie_years)
        else:
            contract_expiry = current_season + rookie_years - 1
    await db.execute(
        "UPDATE players SET team_id = ?, contract_expiry = ? WHERE player_id = ?",
        (team_id, contract_expiry, player_id)
    )
    return contract_expiry


def get_team_emoji(bot, emoji_id):
    """
    Resolve a stored emoji_id to a live discord.Emoji, tolerating missing/invalid IDs.

    Args:
        bot: The discord.py bot/client (has .get_emoji)
        emoji_id: Stored emoji ID (str, int, or None/falsy)

    Returns:
        discord.Emoji or None
    """
    if not emoji_id:
        return None
    try:
        return bot.get_emoji(int(emoji_id))
    except (TypeError, ValueError):
        return None


def get_team_emoji_str(bot, emoji_id, trailing_space=True):
    """
    Resolve a stored emoji_id to a display string, e.g. "<:eagles:123> " or "".

    Args:
        bot: The discord.py bot/client (has .get_emoji)
        emoji_id: Stored emoji ID (str, int, or None/falsy)
        trailing_space: Append a trailing space after the emoji when found
            (matches the "{emoji} **Team**" convention used throughout the bot)

    Returns:
        str: The emoji as a string (with trailing space if requested), or ""
    """
    emoji = get_team_emoji(bot, emoji_id)
    if not emoji:
        return ""
    return f"{emoji} " if trailing_space else str(emoji)


DISCORD_SELECT_MAX_OPTIONS = 25

# The Draft Pool is a holding "team" for undrafted players, not a real club -
# it can't play a match, own a pick, or be traded with, so it is excluded
# from team dropdowns unless a menu explicitly asks for it.
DRAFT_POOL_TEAM_NAME = "Draft Pool"


async def fetch_teams_for_dropdown(db, include_draft_pool=False):
    """Teams for a dropdown, as (team_id, team_name, emoji_id) in name order.

    Central place for the "which teams belong in a picker" rule, so menus
    don't each re-decide it - they previously disagreed, and the Draft Pool
    leaked into pickers that can't meaningfully use it.
    """
    query = "SELECT team_id, team_name, emoji_id FROM teams"
    params = ()
    if not include_draft_pool:
        query += " WHERE team_name != ?"
        params = (DRAFT_POOL_TEAM_NAME,)
    query += " ORDER BY team_name"

    cursor = await db.execute(query, params)
    return await cursor.fetchall()


def add_page_buttons(view, page, total_pages, on_page, row=None,
                     prev_label="◀", next_label="▶", count_label="Page"):
    """Adds the bot's standard page controls to `view`: a grey ◀, a
    greyed-out "Page 2/5" counter, and a grey ▶. The arrows are disabled
    on the first/last page, and nothing is added at all when there's only
    one page. Every paginated menu uses this, so they look and behave the
    same everywhere.

    Args:
        view: the discord.ui.View to add the buttons to.
        page: the current page, 0-based.
        total_pages: how many pages there are.
        on_page: async callable(interaction, new_page), awaited when an
            arrow is pressed - the menu sets its page and redraws itself.
        row: the action row to put the buttons on (None = automatic).
        prev_label / next_label: the arrows' labels, for menus that page
            through something specific - e.g. "◀ Earlier Rounds".
        count_label: the counter's word - "Page 2/5", "Round 2/4"...
            None leaves the counter out (e.g. when the menu's footer or
            title already says where you are).
    """
    import discord  # local: utils is imported by non-discord tooling too

    if total_pages <= 1:
        return

    def arrow(label, target, disabled):
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary,
                                   disabled=disabled, row=row)

        async def callback(interaction):
            await on_page(interaction, target)
        button.callback = callback
        return button

    view.add_item(arrow(prev_label, page - 1, page <= 0))
    if count_label:
        view.add_item(discord.ui.Button(label=f"{count_label} {page + 1}/{total_pages}",
                                        style=discord.ButtonStyle.secondary, disabled=True, row=row))
    view.add_item(arrow(next_label, page + 1, page >= total_pages - 1))


def add_share_button(view, get_embed=None, row=None, get_file=None):
    """Adds the standard "📢 Share" button to an ephemeral menu: posts a copy
    of the embed currently on screen as a public message in the same
    channel, with "Shared by @user" above it. Only the embed is posted, no
    buttons or dropdowns, so nobody else can drive the sharer's menu, and
    the sharer's own menu stays open. Only add it to menus whose content is
    fine for everyone to see - never admin menus or anything with hidden
    ratings.

    get_embed: callable returning the embed to share (or a coroutine
    function, for menus that build their embed asynchronously) - for a menu
    that shares something other than what's on screen. Without it, the
    embed on screen is shared as it is (falling back to view.create_embed()
    if the message isn't available), so it works even for screens whose
    embed was built outside the view.
    row: the action row to put the button on (None = automatic).
    get_file: coroutine function returning a discord.File - for a menu
    showing an image rather than an embed (e.g. the compensation table).
    A File can only be sent once, so it's built fresh for each share."""
    import discord  # local: utils is imported by non-discord tooling too

    button = discord.ui.Button(label="📢 Share", style=discord.ButtonStyle.secondary, row=row)

    async def callback(interaction):
        import inspect
        # Posted as a new message in the channel, not as the reply to the
        # button press - a reply would carry a link back to the private
        # menu, which everyone else sees as "message could not be loaded".
        # The press itself is just acknowledged, leaving the menu as it is.
        await interaction.response.defer()
        if get_file is not None:
            content = {"file": await get_file()}
            if content["file"] is None:
                await interaction.followup.send("❌ There's nothing to share.", ephemeral=True)
                return
        else:
            message = getattr(interaction, "message", None)
            if get_embed is None and message is not None and message.embeds:
                embed = message.embeds[0]
            else:
                embed = (get_embed or view.create_embed)()
                if inspect.isawaitable(embed):
                    embed = await embed
            content = {"embed": discord.Embed.from_dict(embed.to_dict())}
        try:
            await interaction.channel.send(
                f"📢 Shared by {interaction.user.mention}",
                allowed_mentions=discord.AllowedMentions.none(), **content
            )
        except (discord.Forbidden, AttributeError):
            await interaction.followup.send(
                "❌ I can't post in this channel - check I have permission to send messages here.",
                ephemeral=True
            )
    button.callback = callback
    view.add_item(button)


def share_view(embed=None, timeout=900, get_file=None):
    """A view holding just the 📢 Share button, for a command that replies
    with a plain embed and no menu of its own (e.g. /roster) - or, with
    get_file, an image (see add_share_button)."""
    import discord  # local: utils is imported by non-discord tooling too

    view = discord.ui.View(timeout=timeout)
    add_share_button(view, get_embed=(lambda: embed) if embed is not None else None, get_file=get_file)
    return view


async def fetch_stat_seasons(db, player_id=None):
    """Seasons with stats to look back on, for a season dropdown (see
    add_season_select): started (active, offseason or completed) and with
    at least one fixture, newest first. With player_id, only the seasons
    that player played a game in. Rows are (season_id, season_number)."""
    if player_id is None:
        cursor = await db.execute(
            """SELECT s.season_id, s.season_number FROM seasons s
               WHERE s.status IN ('active', 'offseason', 'completed')
                 AND EXISTS (SELECT 1 FROM matches m WHERE m.season_id = s.season_id)
               ORDER BY s.season_number DESC"""
        )
    else:
        cursor = await db.execute(
            """SELECT s.season_id, s.season_number FROM seasons s
               WHERE EXISTS (SELECT 1 FROM player_match_stats pms
                             JOIN matches m ON pms.match_id = m.match_id
                             WHERE m.season_id = s.season_id AND pms.player_id = ?)
               ORDER BY s.season_number DESC""",
            (player_id,)
        )
    return [tuple(row) for row in await cursor.fetchall()]


def add_season_select(view, seasons, season_id, season_number, on_select, row=None):
    """Adds the standard "Season N" dropdown to `view`, for browsing
    previous seasons. `seasons` is fetch_stat_seasons' rows; the season on
    screen (season_id/season_number) is always offered and marked as
    selected, even if it isn't in `seasons` yet (e.g. a season with no games
    played). Nothing is added when there's only that one season to show.
    Holds the newest 25 seasons (a dropdown's limit).

    on_select: async callable(interaction, season_id, season_number),
    awaited when a different season is picked - the menu loads that
    season and redraws itself."""
    import discord  # local: utils is imported by non-discord tooling too

    options = {sid: number for sid, number in seasons}
    if season_id is not None:
        options[season_id] = season_number
    if len(options) <= 1:
        return
    ordered = sorted(options.items(), key=lambda item: -item[1])[:25]
    select = discord.ui.Select(
        placeholder="Switch season...",
        options=[
            discord.SelectOption(label=f"Season {number}", value=str(sid), default=(sid == season_id))
            for sid, number in ordered
        ],
        row=row,
    )

    async def callback(interaction):
        sid = int(select.values[0])
        await on_select(interaction, sid, options[sid])
    select.callback = callback
    view.add_item(select)


def build_team_options(bot, teams, selected=None, extra_options=None,
                       include_draft_pool=False):
    """Build a team dropdown's SelectOption list.

    Returns OPTIONS rather than a ready-made Select: the menus that show
    teams differ in placeholder, row, single-vs-multi select and callback
    behaviour, so only the parts that should be identical everywhere - the
    emoji, the Draft Pool rule, the option cap - are shared here.

    Args:
        bot: bot/client, for resolving each team's emoji.
        teams: rows of (team_id, team_name) or (team_id, team_name, emoji_id).
            A 2-tuple simply renders without an emoji.
        selected: a team_id, or an iterable of them, to mark as default.
            Accepts either so single- and multi-select menus can share this.
        extra_options: SelectOptions to place FIRST, for sentinel entries
            like "All teams" or "Delisted". Counted against the 25 cap.
        include_draft_pool: keep the Draft Pool if it appears in `teams`.
            Only for menus that genuinely act on it (the live draft).

    Returns:
        list[discord.SelectOption], never longer than Discord's 25-option
        limit - exceeding it makes Discord reject the whole message.
    """
    import discord  # local: utils is imported by non-discord tooling too

    if selected is None:
        selected_ids = set()
    elif isinstance(selected, (set, list, tuple, frozenset)):
        selected_ids = set(selected)
    else:
        selected_ids = {selected}

    options = list(extra_options or [])

    for row in teams:
        team_id, team_name = row[0], row[1]
        emoji_id = row[2] if len(row) > 2 else None

        if not include_draft_pool and team_name == DRAFT_POOL_TEAM_NAME:
            continue

        options.append(discord.SelectOption(
            label=str(team_name)[:100],
            value=str(team_id),
            emoji=get_team_emoji(bot, emoji_id),
            default=(team_id in selected_ids),
        ))

    return options[:DISCORD_SELECT_MAX_OPTIONS]


async def get_games_played(db, player_id, season_number=None):
    """
    Games played by a player, derived from player_match_stats - one row per
    player per simulated match (see commands/match_commands.py's
    _persist_match_result) is the durable record of "did this player take
    the field," so games played is just a count, not a separately
    maintained/incrementable column.

    Args:
        db: an open aiosqlite connection
        player_id: the player's ID
        season_number: if given, only count games from that season;
            otherwise counts career games played across every season

    Returns:
        int: games played (0 if the player has never appeared in a
            simulated match)
    """
    if season_number is not None:
        cursor = await db.execute(
            """SELECT COUNT(*) FROM player_match_stats pms
               JOIN matches m ON pms.match_id = m.match_id
               JOIN seasons s ON m.season_id = s.season_id
               WHERE pms.player_id = ? AND s.season_number = ?""",
            (player_id, season_number)
        )
    else:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM player_match_stats WHERE player_id = ?",
            (player_id,)
        )
    result = await cursor.fetchone()
    return result[0] if result else 0


async def ensure_fixture_schema(db):
    """Creates or upgrades everything /generatefixture stores - shared by
    bot.py's init_db and /migratedb so the two can't drift apart.

    - matches.timeslot: e.g. "Thursday night". /generatefixture saves each
      round in play order, so everything listing matches by match_id
      already shows them in timeslot order. NULL for finals.
    - matches.match_name: a marquee game's or forced double-up's name, e.g.
      "Anzac Day Clash", fixed when its fixture is saved. NULL for every
      other game, including finals (which have their own names).
    - fixture_derbies: forced double-ups - league-wide pairs that play twice
      every season (team_a_id < team_b_id), with an optional name for both
      of their games.
    - fixture_marquees: league-wide marquee games, each with a home team and
      an optional name. round_number pins the game to a round (with an
      optional timeslot); NULL means no set round - the pair's first
      meeting, wherever the generator puts it. ground_id optionally pins
      its ground. random_host = 1 lets the generator pick which of the two
      teams hosts it (home_team_id/away_team_id are then just the pair).
    - matches.ground: the ground's name, fixed when the fixture is saved
      (or the final is created). NULL when no grounds are set up.
    - grounds: name, capacity (picks a blockbuster's ground) and an
      optional state, e.g. "VIC".
    - team_grounds: each team's home ground, optional alternate ground,
      crowd draw (1-5), and how many home games a season to play at the
      alternate ground, against opponents of which crowd draws ("1,2,3,4,5").
    - state_finals_grounds: a state's finals ground - finals hosted by a
      team from that state are played there. States without one use the
      host's home ground.
    - settings 'grand_final_ground_id' and 'blockbuster_draw' (see
      commands.fixture_commands)."""
    cursor = await db.execute("PRAGMA table_info(matches)")
    match_columns = [column[1] for column in await cursor.fetchall()]
    if 'timeslot' not in match_columns:
        await db.execute("ALTER TABLE matches ADD COLUMN timeslot TEXT")
    if 'match_name' not in match_columns:
        await db.execute("ALTER TABLE matches ADD COLUMN match_name TEXT")
    if 'ground' not in match_columns:
        await db.execute("ALTER TABLE matches ADD COLUMN ground TEXT")

    await db.execute('''
        CREATE TABLE IF NOT EXISTS fixture_derbies (
            derby_id INTEGER PRIMARY KEY AUTOINCREMENT,
            team_a_id INTEGER NOT NULL,
            team_b_id INTEGER NOT NULL,
            UNIQUE(team_a_id, team_b_id),
            FOREIGN KEY (team_a_id) REFERENCES teams(team_id),
            FOREIGN KEY (team_b_id) REFERENCES teams(team_id)
        )
    ''')
    cursor = await db.execute("PRAGMA table_info(fixture_derbies)")
    if 'name' not in [column[1] for column in await cursor.fetchall()]:
        await db.execute("ALTER TABLE fixture_derbies ADD COLUMN name TEXT")
    await db.execute(_FIXTURE_MARQUEES_TABLE.format(name="fixture_marquees"))
    await _upgrade_fixture_marquees(db)
    cursor = await db.execute("PRAGMA table_info(fixture_marquees)")
    marquee_columns = [column[1] for column in await cursor.fetchall()]
    if 'ground_id' not in marquee_columns:
        await db.execute("ALTER TABLE fixture_marquees ADD COLUMN ground_id INTEGER REFERENCES grounds(ground_id)")
    if 'random_host' not in marquee_columns:
        await db.execute("ALTER TABLE fixture_marquees ADD COLUMN random_host INTEGER NOT NULL DEFAULT 0")

    await db.execute('''
        CREATE TABLE IF NOT EXISTS grounds (
            ground_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            capacity INTEGER,
            state TEXT
        )
    ''')
    await db.execute('''
        CREATE TABLE IF NOT EXISTS team_grounds (
            team_id INTEGER PRIMARY KEY,
            home_ground_id INTEGER,
            alternate_ground_id INTEGER,
            crowd_draw INTEGER NOT NULL DEFAULT 3,
            alternate_games INTEGER NOT NULL DEFAULT 0,
            alternate_opponents TEXT NOT NULL DEFAULT '1,2,3,4,5',
            FOREIGN KEY (team_id) REFERENCES teams(team_id),
            FOREIGN KEY (home_ground_id) REFERENCES grounds(ground_id),
            FOREIGN KEY (alternate_ground_id) REFERENCES grounds(ground_id)
        )
    ''')
    await db.execute('''
        CREATE TABLE IF NOT EXISTS state_finals_grounds (
            state TEXT PRIMARY KEY,
            ground_id INTEGER NOT NULL,
            FOREIGN KEY (ground_id) REFERENCES grounds(ground_id)
        )
    ''')

    # Named matchups were replaced by marquee games with no set round.
    await db.execute("DROP TABLE IF EXISTS fixture_matchup_names")


# Length of an offseason when /endseason isn't told otherwise.
DEFAULT_OFFSEASON_WEEKS = 23


async def ensure_season_schema(db):
    """Adds seasons.offseason_weeks - shared by bot.py's init_db and
    /migratedb. Set by /endseason on the season that's ending: how many
    weeks the offseason that follows it lasts. /startseason uses it to carry
    injuries into the new season, and the offseason injury lists use it to
    show the round of next season each player is back for. NULL (seasons
    ended before this existed) means DEFAULT_OFFSEASON_WEEKS."""
    cursor = await db.execute("PRAGMA table_info(seasons)")
    if 'offseason_weeks' not in [column[1] for column in await cursor.fetchall()]:
        await db.execute("ALTER TABLE seasons ADD COLUMN offseason_weeks INTEGER")


async def ensure_draft_pick_schema(db):
    """Adds draft_picks.pick_type - shared by bot.py's init_db and /migratedb.

    pick_type says what a pick IS, so nothing has to work it out from the
    pick_origin text (which admins can edit in /editdraft, and which went
    stale whenever a team was renamed):
      - 'ladder': a team's own pick in a round, positioned by the ladder.
        Free agency compensation finds a team's natural pick by this (see
        free_agency_commands.end_matching_period). Its origin label is
        built when shown - "R1 tied to <original team>" (see
        pick_origin_label) - so pick_origin is normally NULL; text typed in
        /editdraft overrides the label.
      - 'compensation': a free agency compensation pick.
      - 'father_son': a father-son match or its compensation pick.
      - 'custom': added by hand in /editdraft.
    Rows from before pick_type existed are backfilled from their origin
    text; a ladder pick still showing the old "<team> R<n>" text switches to
    the live label."""
    cursor = await db.execute("PRAGMA table_info(draft_picks)")
    if 'pick_type' not in [column[1] for column in await cursor.fetchall()]:
        await db.execute("ALTER TABLE draft_picks ADD COLUMN pick_type TEXT")
    await db.execute(
        """UPDATE draft_picks SET pick_type = 'compensation'
           WHERE pick_type IS NULL AND pick_origin LIKE '%Compensation'"""
    )
    await db.execute(
        """UPDATE draft_picks SET pick_type = 'father_son'
           WHERE pick_type IS NULL AND (pick_origin LIKE '% F/S Match' OR pick_origin LIKE '% F/S Comp')"""
    )
    await db.execute(
        """UPDATE draft_picks SET pick_type = 'ladder', pick_origin = NULL
           WHERE pick_type IS NULL AND pick_origin =
                 (SELECT team_name FROM teams WHERE team_id = draft_picks.original_team_id)
                 || ' R' || round_number"""
    )
    await db.execute("UPDATE draft_picks SET pick_type = 'custom' WHERE pick_type IS NULL")

    # What a live draft looked like each time a pick came on the clock, so
    # the draft control panel can rewind to any pick or abandon the draft
    # (see draft_commands.py's snapshot_draft / restore_draft_snapshot):
    # every pick row, and every Draft Pool player with their
    # contract_expiry. pick_number 1 is the state before the draft began.
    cursor = await db.execute("PRAGMA table_info(draft_snapshots)")
    snapshot_columns = [column[1] for column in await cursor.fetchall()]
    if snapshot_columns and 'pick_number' not in snapshot_columns:
        # The first version kept one snapshot per draft, taken at its start -
        # the same thing as the new pick 1 snapshot.
        await db.execute("ALTER TABLE draft_snapshots RENAME TO draft_snapshots_old")
    await db.execute('''
        CREATE TABLE IF NOT EXISTS draft_snapshots (
            draft_id INTEGER NOT NULL,
            pick_number INTEGER NOT NULL,
            picks TEXT NOT NULL,
            pool_players TEXT NOT NULL,
            taken_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (draft_id, pick_number),
            FOREIGN KEY (draft_id) REFERENCES drafts(draft_id)
        )
    ''')
    if snapshot_columns and 'pick_number' not in snapshot_columns:
        await db.execute(
            '''INSERT INTO draft_snapshots (draft_id, pick_number, picks, pool_players, taken_at)
               SELECT draft_id, 1, picks, pool_players, taken_at FROM draft_snapshots_old'''
        )
        await db.execute("DROP TABLE draft_snapshots_old")

    # Where a live draft's control panel was posted (see draft_commands.py's
    # post_draft_panel), so it can be kept up to date and its buttons
    # re-registered after a restart.
    cursor = await db.execute("PRAGMA table_info(drafts)")
    draft_columns = [column[1] for column in await cursor.fetchall()]
    for column in ('control_channel_id', 'control_message_id'):
        if column not in draft_columns:
            await db.execute(f"ALTER TABLE drafts ADD COLUMN {column} TEXT")


def pick_origin_label(pick_type, pick_origin, round_number, original_team):
    """The origin text to show for a pick: its own pick_origin if it has
    one, else for a ladder pick "R1 tied to <original_team>" - pass the
    team's emoji where custom emojis render, or its name where they don't
    (dropdowns). None if there's nothing to show."""
    if pick_origin:
        return pick_origin
    if pick_type == 'ladder' and original_team:
        return f"R{round_number} tied to {original_team}"
    return None


async def finals_ground_name(db, home_team_id, grand_final=False):
    """Where a final is played, as a ground name - or None if grounds
    aren't set up for it. The Grand Final is at the 'grand_final_ground_id'
    setting's ground. Any other final is at the host's state finals ground
    (state_finals_grounds, by its home ground's state) if that state has
    one, else at the host's home ground."""
    if grand_final:
        cursor = await db.execute(
            """SELECT g.name FROM settings s JOIN grounds g ON g.ground_id = CAST(s.setting_value AS INTEGER)
               WHERE s.setting_key = 'grand_final_ground_id'"""
        )
        row = await cursor.fetchone()
        return row[0] if row else None
    cursor = await db.execute(
        """SELECT home.name, finals.name FROM team_grounds tg
           JOIN grounds home ON home.ground_id = tg.home_ground_id
           LEFT JOIN state_finals_grounds sf ON sf.state = home.state
           LEFT JOIN grounds finals ON finals.ground_id = sf.ground_id
           WHERE tg.team_id = ?""",
        (home_team_id,)
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return row[1] or row[0]


_FIXTURE_MARQUEES_TABLE = '''
    CREATE TABLE IF NOT EXISTS {name} (
        marquee_id INTEGER PRIMARY KEY AUTOINCREMENT,
        round_number INTEGER,
        home_team_id INTEGER NOT NULL,
        away_team_id INTEGER NOT NULL,
        timeslot TEXT,
        name TEXT,
        FOREIGN KEY (home_team_id) REFERENCES teams(team_id),
        FOREIGN KEY (away_team_id) REFERENCES teams(team_id)
    )
'''


async def _upgrade_fixture_marquees(db):
    """Rebuilds a fixture_marquees table from an earlier version of the bot:
    one that saved marquee games per season (a season_id column), or one
    that required every marquee game to have a round (round_number NOT
    NULL). Each distinct marquee game is kept once, with its name if the
    old table had one."""
    cursor = await db.execute("PRAGMA table_info(fixture_marquees)")
    columns = {column[1]: column for column in await cursor.fetchall()}
    round_required = columns["round_number"][3] == 1
    if 'season_id' not in columns and not round_required:
        return
    name_column = "name" if "name" in columns else "NULL"
    await db.execute(_FIXTURE_MARQUEES_TABLE.format(name="fixture_marquees_new"))
    await db.execute(f'''
        INSERT INTO fixture_marquees_new (round_number, home_team_id, away_team_id, timeslot, name)
        SELECT round_number, home_team_id, away_team_id, timeslot, {name_column} FROM fixture_marquees
        WHERE marquee_id IN (
            SELECT MIN(marquee_id) FROM fixture_marquees
            GROUP BY round_number, home_team_id, away_team_id
        )
        ORDER BY marquee_id
    ''')
    await db.execute("DROP TABLE fixture_marquees")
    await db.execute("ALTER TABLE fixture_marquees_new RENAME TO fixture_marquees")
    print("Upgraded fixture_marquees (league-wide, round optional)")
