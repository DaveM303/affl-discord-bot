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
        season_number: The draft's season_number
        rookie_years: The draft's rookie_contract_years

    Returns:
        int: The contract_expiry that was assigned
    """
    contract_expiry = calculate_contract_expiry(season_number, rookie_years)
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

