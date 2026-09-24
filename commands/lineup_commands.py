import random
import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
import json
from config import DB_PATH
from commands.season_commands import get_round_name
from utils import is_admin_user, get_team_emoji, get_team_emoji_str
from match_sim import (
    slot_group, POSITION_ALLOWED_GROUPS, RUCK_SLOT, RUCK_ELIGIBLE_POSITIONS, Player,
    _team_strengths, _resolve_bench_groups, INTERCHANGE_SLOTS,
    KEY_POSITION_TYPES, KEY_POSITION_COUNT_THRESHOLD,
)

# AFL lineup structure with 18 positions + 5 interchange
AFL_POSITIONS = [
    # Back 6
    "LBP", "FB", "RBP",
    "LHB", "CHB", "RHB",
    # Mid 3
    "LW", "C", "RW",
    # Forward 6
    "LHF", "CHF", "RHF",
    "LFP", "FF", "RFP",
    # Followers 3
    "R", "RR", "RO",
    # Interchange (5)
    "INT1", "INT2", "INT3", "INT4", "INT5"
]

# Row groupings used by format_lineup_description below - display only,
# distinct from AFL_POSITIONS which is the actual slot order used for
# lineup storage/validation.
_LINEUP_DISPLAY_ROWS = [
    ("FB", ["LBP", "FB", "RBP"]),
    ("HB", ["LHB", "CHB", "RHB"]),
    ("C", ["LW", "C", "RW"]),
    ("HF", ["LHF", "CHF", "RHF"]),
    ("FF", ["LFP", "FF", "RFP"]),
    ("Fol", ["R", "RR", "RO"]),
]


def format_lineup_description(lineup):
    """Renders a team's lineup rows (`SELECT position_name, player_id, name,
    position, overall_rating FROM lineups JOIN players ...` shaped tuples)
    into the same grouped-by-line text used in the lineup channel post.
    Shared by SeasonCommands._try_announce_lineups (season_commands.py,
    called from /matchsimulation's Announce Lineups button in
    match_commands.py) and, previously, the old submit-and-post flow here."""
    # OVRs shown are the ADJUSTED (effective) ones, matching the lineup
    # editor - a player out of position shows the rating they'll actually
    # play at, not their base rating. Interchange is never adjusted.
    lineup_dict = {
        pos_name: {'player_id': player_id, 'name': name, 'pos': pos, 'rating': rating}
        for pos_name, player_id, name, pos, rating in lineup
    }
    field_text = ""

    for line_name, positions in _LINEUP_DISPLAY_ROWS:
        row_text = []
        for pos_name in positions:
            if pos_name in lineup_dict:
                p = lineup_dict[pos_name]
                row_text.append(f"{p['name']} ({_display_ovr(pos_name, p)})")
            else:
                row_text.append("*Empty*")
        field_text += f"**{line_name}:**  {', '.join(row_text)}\n"

    field_text += "\n"

    int_players = []
    for pos_name in ["INT1", "INT2", "INT3", "INT4", "INT5"]:
        if pos_name in lineup_dict:
            p = lineup_dict[pos_name]
            int_players.append(f"{p['name']} ({_display_ovr(pos_name, p)})")
        else:
            int_players.append("*Empty*")
    field_text += f"**Int:**  {', '.join(int_players)}"

    return field_text


def _display_ovr(pos_name, player_info):
    """OVR to show for a player sitting in an on-field lineup slot - their
    match_sim.py effective_ovr (rounded), which is lower than their base
    overall_rating whenever they're out of position for that slot (wrong
    group entirely, wrong ruck/non-ruck role, or a key position player
    parked off their spine/pocket home slot - see Player._compute_effective_ovr).
    Interchange slots are never adjusted (a bench player is never actually
    "out of position" under the current position-group system - see
    Player._compute_effective_ovr's own note), so this returns the base
    rating unchanged for INT1-5."""
    if pos_name in INTERCHANGE_SLOTS or not player_info.get('player_id'):
        return player_info['rating']
    player = Player(player_info['player_id'], player_info['name'], player_info['pos'], player_info['rating'], pos_name)
    return round(player.effective_ovr)


def fits_without_penalty(position, slot):
    """True if a player of this natural position suffers zero
    Player.effective_ovr penalty in this slot (fully in-group, correct
    ruck/non-ruck role, and - for a key-position-equivalent player in a
    key-position line - the right spine/pocket home slot; a generalist in a
    pocket also qualifies, since GENERALIST_SPINE_PENALTY only applies at
    the 4 true spine slots FB/CHB/FF/CHF). Shared by auto_fill_lineup
    (prioritizing a slot's "ideal" position type before falling back to
    whoever improves team strength the most regardless of fit) and
    LineupView.get_sorted_roster (sorting the player-picker dropdown the
    same way, instead of the old flat "any defender for any defensive
    slot" bucket that didn't distinguish spine from pocket/flank).
    Interchange slots never have a penalty concept (see
    Player._compute_effective_ovr), so every position trivially qualifies
    there."""
    return Player(0, "", position, 100, slot).effective_ovr == 100


async def clear_departed_players_from_lineups(db, player_ids, old_team_id=None):
    """Strips players who have just left a team out of BOTH that team's
    live lineup (`lineups`) and its saved main lineup (`starting_lineups`).
    Call this from every path that moves a player off a team - trades,
    free agency, delisting - right after the players row is updated.

    Leaving these rows behind is not cosmetic: validate_lineup only counts
    lineup rows that still join to a player on that team, so a departed
    player's row reads as an EMPTY slot to validation, while anything
    reading `lineups` directly sees the slot as filled. That mismatch is
    what made force-submit refuse a lineup ("3 position(s) empty") that
    auto-fill insisted had nothing to fix.

    old_team_id is optional and only scopes the starting_lineups cleanup;
    the `lineups` delete is keyed on player_id alone, since a player can
    only ever hold a slot for the team they were on."""
    player_ids = [pid for pid in player_ids if pid is not None]
    if not player_ids:
        return

    placeholders = ','.join('?' * len(player_ids))
    await db.execute(
        f"DELETE FROM lineups WHERE player_id IN ({placeholders})",
        player_ids
    )

    if old_team_id is None:
        return

    cursor = await db.execute(
        "SELECT lineup_data FROM starting_lineups WHERE team_id = ?",
        (old_team_id,)
    )
    result = await cursor.fetchone()
    if not result:
        return

    lineup_data = json.loads(result[0])
    departed = {str(pid) for pid in player_ids}
    remaining = {pos: pid for pos, pid in lineup_data.items() if str(pid) not in departed}
    if len(remaining) != len(lineup_data):
        await db.execute(
            "UPDATE starting_lineups SET lineup_data = ? WHERE team_id = ?",
            (json.dumps(remaining), old_team_id)
        )


async def team_playing_this_round(db, team_id, round_number):
    """True if team_id has a fixture (as home or away) in round_number.
    Used to block lineup submission for teams on a bye or already
    eliminated from the finals - there's nothing to submit a lineup for."""
    cursor = await db.execute(
        """SELECT 1 FROM matches
           WHERE round_number = ? AND (home_team_id = ? OR away_team_id = ?)
           LIMIT 1""",
        (round_number, team_id, team_id)
    )
    return await cursor.fetchone() is not None


async def validate_lineup(db, team_id, current_round):
    """Shared lineup-readiness check - completeness, duplicates, injured/
    suspended players still out. Used by the Submit Lineup button
    (TeamLineupMenu.submit_lineup_callback) and by the force-submit path
    (season_commands.py's _try_announce_lineups/_AnnounceLineupsMissingView,
    triggered from /matchsimulation's Announce Lineups button), so the
    definition of "a valid lineup" stays in exactly one place. Returns
    (errors, player_ids) - errors is empty when the lineup is valid;
    player_ids is the lineup's player_id list regardless (callers that only
    care about validity can just check `if errors`)."""
    cursor = await db.execute(
        """SELECT l.position_name, p.player_id, p.name, p.position, p.overall_rating
           FROM lineups l
           JOIN players p ON l.player_id = p.player_id AND p.team_id = l.team_id
           WHERE l.team_id = ?
           ORDER BY l.slot_number""",
        (team_id,)
    )
    lineup = await cursor.fetchall()

    errors = []
    if len(lineup) < 23:
        empty_count = 23 - len(lineup)
        errors.append(f"❌ Lineup incomplete: {empty_count} position(s) empty")

    player_ids = [p[1] for p in lineup]
    if len(player_ids) != len(set(player_ids)):
        errors.append("❌ Duplicate players in lineup")

    if player_ids:
        placeholders = ','.join('?' * len(player_ids))

        slot_by_player_id = {p[1]: p[0] for p in lineup}

        cursor = await db.execute(
            f"""SELECT p.player_id, p.name, i.return_round
               FROM injuries i
               JOIN players p ON i.player_id = p.player_id
               WHERE i.player_id IN ({placeholders}) AND i.status = 'injured'""",
            player_ids
        )
        injuries = await cursor.fetchall()
        injured_players = []
        for player_id, name, return_round in injuries:
            # return_round is NULL while recovery length is still TBC (see
            # season_commands.py's _roll_pending_injury_recoveries) - the
            # player is still definitely out either way.
            if return_round is not None and return_round - current_round <= 0:
                continue
            slot = slot_by_player_id.get(player_id, "?")
            injured_players.append(f"{name} ({slot})")
        if injured_players:
            errors.append(f"❌ Injured players: {', '.join(injured_players)}")

        cursor = await db.execute(
            f"""SELECT p.player_id, p.name, s.games_remaining
               FROM suspensions s
               JOIN players p ON s.player_id = p.player_id
               WHERE s.player_id IN ({placeholders}) AND s.status = 'suspended'""",
            player_ids
        )
        suspensions = await cursor.fetchall()
        suspended_players = []
        for player_id, name, games_remaining in suspensions:
            # games_remaining is NULL while suspension length is still TBC
            # (see season_commands.py's _roll_pending_report_suspensions) -
            # the player is still definitely out either way.
            if games_remaining is None or games_remaining > 0:
                slot = slot_by_player_id.get(player_id, "?")
                suspended_players.append(f"{name} ({slot})")
        if suspended_players:
            errors.append(f"❌ Suspended players: {', '.join(suspended_players)}")

    return errors, player_ids


async def auto_fill_lineup(db, team_id, current_round):
    """Automatically repairs a team's full 23-slot lineup by maximizing
    overall team strength, reusing match_sim.py's own scoring
    (_team_strengths/Player.effective_ovr) - the same math the simulator
    itself uses, so this is automatically aware of out-of-position
    penalties, key-position spine/pocket penalties, AND key-position
    line-overload penalties, without needing any bespoke "does this player
    fit" heuristic of its own.

    Only currently-invalid slots are touched - valid slots (starting or
    interchange) are never disturbed, matching the original scope of this
    tool (a repair pass, not a full lineup optimizer).

    Invalid slots (empty, injured, suspended, or a duplicate of an earlier
    slot in the whole lineup - see below) are processed one at a time, in
    AFL_POSITIONS order (starting slots first, then interchange). For each,
    every legal candidate is considered:
      - any currently-unclaimed, available reserve - scored by the single
        resulting team strength of putting them in this slot
      - for a STARTING slot only, any player currently in a still-VALID
        interchange slot - scored as a COMBO: their move into this slot
        AND the best available reserve backfilling the interchange slot
        they vacate, evaluated together as one resulting team strength.
        Scoring the move alone (ignoring the backfill) would unfairly
        penalize a strong interchange candidate purely because the
        strength their vacated slot loses isn't priced back in - the combo
        score avoids that. If picked, the vacated interchange slot is
        appended to the back of the processing queue and filled for real
        the same way (from reserves only - interchange slots never borrow
        from each other, which would just chase the same vacancy in
        circles).
    Candidates are searched in two tiers (see fits_without_penalty):
    first, only candidates whose natural position suffers zero
    Player.effective_ovr penalty in this slot; if that tier is empty, every
    candidate regardless of fit. Whichever candidate yields the highest
    score WITHIN that tier is picked - still "whichever candidate improves
    team strength the most," just searched fit-first rather than across
    the whole roster every time. A large enough OVR gap can still win a
    slot fully out of position, but only once nobody who actually fits is
    left available - this is a search-order preference, not a hard
    positional-fit gate (not even the R/ruck slot is a hard block, since
    match_sim.py's out-of-position penalty is always just a multiplier).

    Duplicate detection runs across the WHOLE 23-slot lineup in
    AFL_POSITIONS order - a player_id appearing more than once (in any
    combination of starting/interchange slots) is only kept in the FIRST
    slot they appear in; every later occurrence is treated as invalid.

    A slot whose occupant is no longer on this team (traded or delisted
    since the lineup was last set, leaving the `lineups` row behind) counts
    as invalid too, exactly like an empty one. validate_lineup only counts
    rows that still join to a player ON this team, so without this such a
    slot reads as "empty" to validation but "filled" to this function -
    autofill would report nothing to do while force-submit kept refusing
    the lineup as incomplete.

    A player already used earlier in this same run (moved or pulled from
    reserves) is never reused for a later slot, and a player who is
    themselves injured/suspended is never moved or pulled from reserves.
    Mutates the `lineups` table directly and returns (changes, unfilled) -
    changes is a list of "{slot}: {player name} ({ovr})" strings describing
    what moved where (for admin visibility), unfilled is a list of slot
    names that still couldn't be filled (roster fully exhausted)."""
    cursor = await db.execute(
        "SELECT player_id, name, position, overall_rating FROM players WHERE team_id = ?",
        (team_id,)
    )
    roster = {row[0]: {"name": row[1], "position": row[2], "ovr": row[3]} for row in await cursor.fetchall()}

    cursor = await db.execute(
        "SELECT slot_number, position_name, player_id FROM lineups WHERE team_id = ? ORDER BY slot_number",
        (team_id,)
    )
    lineup_rows = await cursor.fetchall()
    slot_to_player = {position_name: player_id for _, position_name, player_id in lineup_rows}

    cursor = await db.execute(
        "SELECT player_id, return_round FROM injuries WHERE status = 'injured'"
    )
    injured_return = {row[0]: row[1] for row in await cursor.fetchall()}
    cursor = await db.execute(
        "SELECT player_id, games_remaining FROM suspensions WHERE status = 'suspended'"
    )
    suspended_games_remaining = {row[0]: row[1] for row in await cursor.fetchall()}

    def is_unavailable(player_id):
        # "Still actually out" - same test as validate_lineup, not just a
        # raw status flag (a recovered/served player's row can still say
        # status='injured'/'suspended' until Advance to Next Round clears
        # it). Suspensions use games_remaining (ticks down only on rounds
        # the team actually plays), not a round-number comparison like
        # injuries - a bye round doesn't serve any of the suspension.
        # A NULL return_round means recovery length is still TBC (see
        # _roll_pending_injury_recoveries) - definitely still unavailable
        # either way, so that's treated as True with no round-number math.
        # Same for a NULL games_remaining (see
        # _roll_pending_report_suspensions) - a still-TBC report.
        if player_id in injured_return:
            player_return_round = injured_return[player_id]
            if player_return_round is None or player_return_round - current_round > 0:
                return True
        if player_id in suspended_games_remaining:
            player_games_remaining = suspended_games_remaining[player_id]
            if player_games_remaining is None or player_games_remaining > 0:
                return True
        return False

    starting_slots = AFL_POSITIONS[:18]
    interchange_slots = AFL_POSITIONS[18:]

    # Which player_id occupies which slot right now, and which player_ids
    # are already "claimed" this run (assigned to a slot, or otherwise
    # unavailable) so the same reserve/interchange player can't be double-used.
    claimed_player_ids = {pid for pid in slot_to_player.values() if pid is not None}

    # Duplicate detection runs across the WHOLE 23-slot lineup (not just the
    # starting 18) in AFL_POSITIONS order, so a player appearing twice -
    # whether both times in the starting 18, both times on the interchange,
    # or once in each - is only ever kept in the FIRST slot they appear in;
    # every later occurrence is treated as vacant, regardless of which of
    # the two invalid-slot categories (starting vs. interchange) it falls in.
    seen_player_ids = set()
    invalid_starting_slots = []
    invalid_interchange_slots = []
    for slot in AFL_POSITIONS:
        player_id = slot_to_player.get(slot)
        target_list = invalid_starting_slots if slot in starting_slots else invalid_interchange_slots
        if player_id is None:
            target_list.append(slot)
        elif player_id not in roster:
            # Occupant has left the team (traded/delisted) but their
            # lineups row survived - the slot is effectively empty, and
            # validate_lineup already treats it that way.
            target_list.append(slot)
        elif player_id in seen_player_ids:
            target_list.append(slot)
        elif is_unavailable(player_id):
            target_list.append(slot)
        else:
            seen_player_ids.add(player_id)

    def reserve_pool():
        """Roster players not currently claimed by any slot and not
        themselves injured/suspended."""
        return [
            (pid, info) for pid, info in roster.items()
            if pid not in claimed_player_ids and not is_unavailable(pid)
        ]

    # Unseeded - each team_strength_of() call independently resolves
    # bench roles for whatever hypothetical lineup it's scoring, same as
    # a real match_sim.py simulation would (a fresh per-match roll, not
    # meant to be reproducible run to run).
    rng = random.Random()

    def team_strength_of(overrides):
        """Total team strength (sum of the three match_sim.py group
        strengths) of the current lineup with `overrides` (slot -> player_id
        or None) applied on top of slot_to_player. Empty slots are simply
        omitted from the Player list passed to match_sim.py - it only ever
        scores players actually on the strength sheet."""
        players = []
        for s in AFL_POSITIONS:
            effective_pid = overrides[s] if s in overrides else slot_to_player.get(s)
            if effective_pid is None or effective_pid not in roster:
                continue
            info = roster[effective_pid]
            players.append(Player(effective_pid, info["name"], info["position"], info["ovr"], s))
        # _team_strengths reads player_group(p) for each player, which for
        # a bench (interchange) Player requires .resolved_group to already
        # be set - see _resolve_bench_groups. Without this, every bench
        # hybrid/ruck would silently contribute 0 to every group's
        # strength instead of counting toward whichever line they'd
        # actually fill this match.
        _resolve_bench_groups([p for p in players if p.slot in INTERCHANGE_SLOTS], rng)
        return sum(_team_strengths(players).values())

    def best_reserve_for(slot, exclude_id=None):
        """Highest-team-strength reserve for `slot` (evaluated on its own,
        no further vacancy chain), excluding `exclude_id` if given. Returns
        (player_id, info, resulting_strength) or None if no reserves left."""
        pool = [item for item in reserve_pool() if item[0] != exclude_id]
        if not pool:
            return None
        pid, info = max(pool, key=lambda item: team_strength_of({slot: item[0]}))
        return pid, info, team_strength_of({slot: pid})

    changes = []
    unfilled = []

    # Slots still needing a fill, processed in order - starting slots first
    # (matches AFL_POSITIONS order), then interchange. Borrowing a valid
    # interchange player for a starting slot appends that player's now-
    # vacant interchange slot to the back of this queue, so it gets filled
    # afterward like any other empty slot (from reserves only - interchange
    # slots never borrow from each other, which would just chase the same
    # vacancy in circles).
    queue = list(invalid_starting_slots) + list(invalid_interchange_slots)
    queued = set(queue)

    while queue:
        slot = queue.pop(0)
        is_starting = slot in starting_slots

        # Each candidate is scored as (player_id, info, source_int_slot,
        # resulting_strength). A reserve candidate's strength is just that
        # one move; an interchange-borrow candidate's strength is the FULL
        # combo - their move AND the best reserve backfill for the slot
        # they vacate - scored together, since scoring the move alone
        # unfairly ignores the backfill's own contribution and can make a
        # much better borrow option look worse than a mediocre reserve.
        candidates = []
        for pid, info in reserve_pool():
            candidates.append((pid, info, None, team_strength_of({slot: pid})))

        if is_starting:
            for int_slot in interchange_slots:
                if int_slot in queued:
                    continue  # already empty/invalid itself, not a valid lender
                int_player_id = slot_to_player.get(int_slot)
                if int_player_id is None or int_player_id not in roster:
                    continue
                backfill = best_reserve_for(int_slot, exclude_id=int_player_id)
                overrides = {slot: int_player_id, int_slot: backfill[0] if backfill else None}
                combo_strength = team_strength_of(overrides)
                candidates.append((int_player_id, roster[int_player_id], int_slot, combo_strength))

        if not candidates:
            # Genuinely nobody left - this slot must end up empty, not keep
            # whatever invalid occupant it started with (e.g. a duplicate's
            # second occurrence, or an injured player), since that occupant
            # is exactly what made the slot invalid in the first place.
            slot_to_player[slot] = None
            unfilled.append(slot)
            continue

        # Prioritize candidates whose natural position suffers no
        # out-of-position penalty in this slot (see fits_without_penalty) -
        # only fall back to the full candidate pool if none fit. Within
        # whichever tier is used, still pick by best resulting team
        # strength - this only changes WHICH pool is searched, not the
        # underlying "biggest improvement wins" logic.
        fitting_candidates = [c for c in candidates if fits_without_penalty(c[1]["position"], slot)]
        pool = fitting_candidates if fitting_candidates else candidates

        best_id, best_info, source_int_slot, _ = max(pool, key=lambda item: item[3])

        slot_to_player[slot] = best_id
        claimed_player_ids.add(best_id)
        if source_int_slot is not None:
            changes.append(f"{slot}: {best_info['name']} ({best_info['ovr']}) - from {source_int_slot}")
            slot_to_player[source_int_slot] = None
            if source_int_slot not in queued:
                queue.append(source_int_slot)
                queued.add(source_int_slot)
        else:
            changes.append(f"{slot}: {best_info['name']} ({best_info['ovr']}) - reserve")

    # Write the final slot assignments back to the database.
    for slot, player_id in slot_to_player.items():
        await db.execute("DELETE FROM lineups WHERE team_id = ? AND position_name = ?", (team_id, slot))
        if player_id is not None:
            slot_number = AFL_POSITIONS.index(slot) + 1
            await db.execute(
                "INSERT INTO lineups (team_id, player_id, slot_number, position_name) VALUES (?, ?, ?, ?)",
                (team_id, player_id, slot_number, slot)
            )
    await db.commit()

    return changes, unfilled


async def lineups_locked(db):
    """True while the active season's lineups are locked (set by the
    Announce Lineups button on /matchsimulation, cleared by that panel's
    Advance to Next Round button - see season_commands.py). Every path that
    mutates the `lineups` table must check this first and refuse if locked,
    since a locked round's lineups are what actually gets simmed."""
    cursor = await db.execute(
        "SELECT lineups_locked FROM seasons WHERE status = 'active' LIMIT 1"
    )
    row = await cursor.fetchone()
    return bool(row and row[0])


async def unconfirm_lineup(db, team_id):
    """Clears a team's lineup_confirmed flag - called whenever their live
    lineup changes after they'd confirmed but before the round locks, so a
    stale confirmation can't slip through when the round gets announced."""
    await db.execute(
        "UPDATE teams SET lineup_confirmed = 0 WHERE team_id = ?",
        (team_id,)
    )


class LineupCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def get_user_team(self, user_id: int, guild) -> tuple:
        """Get the team for a Discord user. Returns (team_id, team_name) or (None, None)"""
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT team_id, team_name, role_id FROM teams WHERE role_id IS NOT NULL")
            teams = await cursor.fetchall()
            
            for team_id, team_name, role_id in teams:
                role = guild.get_role(int(role_id))
                if role:
                    member = guild.get_member(user_id)
                    if member and role in member.roles:
                        return team_id, team_name
            
            return None, None

    async def is_admin(self, interaction: discord.Interaction) -> bool:
        """Check if user is admin (owner or has admin role/permissions)"""
        return await is_admin_user(interaction)

    async def team_autocomplete(
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

    @app_commands.command(name="teamlineup", description="Open the lineup management menu")
    @app_commands.describe(team_name="[ADMIN ONLY] Team name to manage lineup for")
    @app_commands.autocomplete(team_name=team_autocomplete)
    async def team_lineup(self, interaction: discord.Interaction, team_name: str = None):
        # If team_name specified, check if user is admin
        if team_name:
            if not await self.is_admin(interaction):
                await interaction.response.send_message(
                    "❌ Only admins can manage other teams' lineups! "
                    "Type `/teamlineup` without a team parameter to access your own lineup.",
                    ephemeral=True
                )
                return

            # Look up specified team (exact match due to autocomplete)
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "SELECT team_id, team_name FROM teams WHERE team_name = ?",
                    (team_name,)
                )
                result = await cursor.fetchone()
                if not result:
                    await interaction.response.send_message(
                        f"❌ Team '{team_name}' not found. Please select from the autocomplete suggestions.",
                        ephemeral=True
                    )
                    return
                team_id, team_name = result
        else:
            # Get user's team
            team_id, team_name = await self.get_user_team(interaction.user.id, interaction.guild)

            if not team_id:
                await interaction.response.send_message(
                    "❌ You don't manage a team!",
                    ephemeral=True
                )
                return

        async with aiosqlite.connect(DB_PATH) as db:
            view, embed = await build_team_lineup_menu(db, self.bot, team_id, team_name)

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        # Store message reference so the view can edit it later
        view.message = await interaction.original_response()

    @app_commands.command(name="viewlineup", description="View your team's current lineup")
    @app_commands.autocomplete(team_name=team_autocomplete)
    async def view_lineup(self, interaction: discord.Interaction, team_name: str = None):
        # If no team specified, get user's team
        if not team_name:
            team_id, team_name = await self.get_user_team(interaction.user.id, interaction.guild)
            if not team_id:
                await interaction.response.send_message(
                    "❌ You don't manage a team! Specify a team name to view their lineup.",
                    ephemeral=True
                )
                return
        else:
            # Look up specified team (exact match due to autocomplete)
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "SELECT team_id FROM teams WHERE team_name = ?",
                    (team_name,)
                )
                result = await cursor.fetchone()
                if not result:
                    await interaction.response.send_message(
                        f"❌ Team '{team_name}' not found. Please select from the autocomplete suggestions.",
                        ephemeral=True
                    )
                    return
                team_id = result[0]
        
        # Get lineup
        async with aiosqlite.connect(DB_PATH) as db:
            # player_id is selected too because format_lineup_description
            # needs it to compute each slot's ADJUSTED (effective) OVR.
            cursor = await db.execute(
                """SELECT l.position_name, p.player_id, p.name, p.position, p.overall_rating
                   FROM lineups l
                   JOIN players p ON l.player_id = p.player_id AND p.team_id = l.team_id
                   WHERE l.team_id = ?
                   ORDER BY l.slot_number""",
                (team_id,)
            )
            lineup = await cursor.fetchall()
        
        # Get team emoji
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT emoji_id FROM teams WHERE team_id = ?",
                (team_id,)
            )
            result = await cursor.fetchone()
            emoji_id = result[0] if result else None
        
        # Get emoji
        emoji = get_team_emoji_str(interaction.client, emoji_id)

        # Create embed
        embed = discord.Embed(
            title=f"{emoji}{team_name} Lineup",
            color=discord.Color.blue()
        )
        
        # Same renderer as the lineup channel post, so both show the
        # adjusted (effective) OVR for anyone out of position.
        field_text = format_lineup_description(lineup)

        embed.description = field_text
        embed.set_footer(text=f"{len(lineup)}/23 players selected")

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="delist", description="Delist player(s) from your team (offseason only)")
    @app_commands.describe(
        player1="First player to delist",
        player2="Second player to delist (optional)",
        player3="Third player to delist (optional)",
        player4="Fourth player to delist (optional)",
        player5="Fifth player to delist (optional)",
        player6="Sixth player to delist (optional)",
        player7="Seventh player to delist (optional)",
        player8="Eighth player to delist (optional)",
        team_name="[ADMIN ONLY] Team name to delist players from"
    )
    @app_commands.autocomplete(
        player1=player_name_autocomplete,
        player2=player_name_autocomplete,
        player3=player_name_autocomplete,
        player4=player_name_autocomplete,
        player5=player_name_autocomplete,
        player6=player_name_autocomplete,
        player7=player_name_autocomplete,
        player8=player_name_autocomplete,
        team_name=team_autocomplete
    )
    async def delist_player(
        self,
        interaction: discord.Interaction,
        player1: str,
        player2: str = None,
        player3: str = None,
        player4: str = None,
        player5: str = None,
        player6: str = None,
        player7: str = None,
        player8: str = None,
        team_name: str = None
    ):
        # If team_name specified, check if user is admin
        if team_name:
            if not await self.is_admin(interaction):
                await interaction.response.send_message(
                    "❌ Only admins can delist players from other teams!",
                    ephemeral=True
                )
                return

            # Look up specified team (exact match due to autocomplete)
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "SELECT team_id, team_name FROM teams WHERE team_name = ?",
                    (team_name,)
                )
                result = await cursor.fetchone()
                if not result:
                    await interaction.response.send_message(
                        f"❌ Team '{team_name}' not found. Please select from the autocomplete suggestions.",
                        ephemeral=True
                    )
                    return
                team_id, team_name = result
        else:
            # Get user's team
            team_id, team_name = await self.get_user_team(interaction.user.id, interaction.guild)

            if not team_id:
                await interaction.response.send_message(
                    "❌ You don't manage a team!",
                    ephemeral=True
                )
                return

        # Check if it's offseason and get current season
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT season_number FROM seasons WHERE status = 'offseason' LIMIT 1"
            )
            season = await cursor.fetchone()

            if not season:
                await interaction.response.send_message(
                    "❌ Players can only be delisted during the offseason!",
                    ephemeral=True
                )
                return

            current_season = season[0]

            # Collect all player IDs (from autocomplete)
            player_ids = [player1, player2, player3, player4, player5, player6, player7, player8]
            player_ids = [p for p in player_ids if p is not None]  # Remove None values

            delisted_players = []
            errors = []

            for player_id_str in player_ids:
                try:
                    player_id = int(player_id_str)
                except ValueError:
                    errors.append(f"❌ Invalid player selection. Please use the autocomplete suggestions.")
                    continue

                # Find player by ID on this team
                cursor = await db.execute(
                    """SELECT player_id, name, position, overall_rating, age FROM players
                       WHERE player_id = ? AND team_id = ?""",
                    (player_id, team_id)
                )
                result = await cursor.fetchone()

                if not result:
                    errors.append(f"❌ Player not found on this team (ID: {player_id})")
                    continue

                player_id, full_name, position, rating, age = result

                # Delist the player (set team_id to NULL and contract_expiry to current season)
                await db.execute(
                    "UPDATE players SET team_id = NULL, contract_expiry = ? WHERE player_id = ?",
                    (current_season, player_id)
                )

                # Remove from the team's live lineup AND its saved main
                # lineup - previously only `lineups` was cleared, which
                # left the delisted player in starting_lineups to
                # reappear the next time it was restored.
                await clear_departed_players_from_lineups(db, [player_id], team_id)

                delisted_players.append((full_name, position, rating, age))

            await db.commit()

            # Get delist log channel
            cursor = await db.execute(
                "SELECT setting_value FROM settings WHERE setting_key = 'delist_log_channel_id'"
            )
            result = await cursor.fetchone()

        # Build response message
        response = ""
        if delisted_players:
            if len(delisted_players) == 1:
                response += f"✅ **{delisted_players[0][0]}** has been delisted from **{team_name}**."
            else:
                player_list = ", ".join([f"**{name}**" for name, _, _, _ in delisted_players])
                response += f"✅ {len(delisted_players)} players delisted from **{team_name}**: {player_list}"

        if errors:
            if response:
                response += "\n\n"
            response += "\n".join(errors)

        await interaction.response.send_message(response)

        # Log to delist channel if configured
        if result and result[0] and delisted_players:
            try:
                log_channel = interaction.guild.get_channel(int(result[0]))
                if log_channel:
                    if len(delisted_players) == 1:
                        full_name, position, rating, age = delisted_players[0]
                        embed = discord.Embed(
                            title="Player Delisted",
                            color=discord.Color.red(),
                            description=f"**{full_name}** ({position}, {rating} OVR, {age}yo) has been delisted from **{team_name}**."
                        )
                        embed.set_footer(text=f"Delisted by {interaction.user.display_name}")
                        await log_channel.send(embed=embed)
                    else:
                        embed = discord.Embed(
                            title=f"{len(delisted_players)} Players Delisted",
                            color=discord.Color.red(),
                            description=f"**{team_name}** delisted:"
                        )
                        for full_name, position, rating, age in delisted_players:
                            embed.add_field(
                                name=full_name,
                                value=f"{position}, {rating} OVR, {age}yo",
                                inline=True
                            )
                        embed.set_footer(text=f"Delisted by {interaction.user.display_name}")
                        await log_channel.send(embed=embed)
            except Exception as e:
                # Don't fail the command if logging fails
                print(f"Failed to log delist: {e}")


async def build_team_lineup_menu(db, bot, team_id, team_name=None):
    """Builds a ready-to-send (TeamLineupMenu, embed) pair for one team -
    factored out of /teamlineup's own command body so other code (e.g.
    season_commands.py's round-summary "Set Lineup for Next Round" button)
    can open the exact same menu directly, without going through the
    command itself. team_name is looked up if not already known by the
    caller. Caller owns the actual interaction.response/followup send -
    this only builds the view/embed."""
    if team_name is None:
        cursor = await db.execute("SELECT team_name FROM teams WHERE team_id = ?", (team_id,))
        row = await cursor.fetchone()
        team_name = row[0] if row else "Unknown Team"

    # Get current lineup
    cursor = await db.execute(
        """SELECT l.position_name, p.name, p.position, p.overall_rating, p.player_id
           FROM lineups l
           JOIN players p ON l.player_id = p.player_id AND p.team_id = l.team_id
           WHERE l.team_id = ?
           ORDER BY l.slot_number""",
        (team_id,)
    )
    lineup_data = await cursor.fetchall()

    # Get roster
    cursor = await db.execute(
        """SELECT player_id, name, position, overall_rating, age
           FROM players
           WHERE team_id = ?
           ORDER BY overall_rating DESC""",
        (team_id,)
    )
    roster = await cursor.fetchall()

    # Get team emoji and confirmation state
    cursor = await db.execute(
        "SELECT emoji_id, lineup_confirmed FROM teams WHERE team_id = ?",
        (team_id,)
    )
    result = await cursor.fetchone()
    emoji_id = result[0] if result else None
    is_confirmed = bool(result[1]) if result else False

    # Check if starting lineup exists
    cursor = await db.execute(
        "SELECT 1 FROM starting_lineups WHERE team_id = ?",
        (team_id,)
    )
    has_starting_lineup = await cursor.fetchone() is not None

    cursor = await db.execute(
        "SELECT current_round, lineups_locked FROM seasons WHERE status = 'active' LIMIT 1"
    )
    season_row = await cursor.fetchone()
    current_round = season_row[0] if season_row else 0
    season_lineups_locked = bool(season_row[1]) if season_row else False

    not_playing_this_round = (
        current_round > 0 and not await team_playing_this_round(db, team_id, current_round)
    )

    # Build lineup dict
    lineup = {}
    for pos_name, name, pos, rating, player_id in lineup_data:
        lineup[pos_name] = {'name': name, 'pos': pos, 'rating': rating, 'player_id': player_id}

    view = TeamLineupMenu(team_id, team_name, lineup, roster, bot, emoji_id, has_starting_lineup,
                           is_confirmed=is_confirmed, lineups_locked=season_lineups_locked,
                           not_playing_this_round=not_playing_this_round)
    embed = await view.create_menu_embed()
    return view, embed


class TeamLineupMenu(discord.ui.View):
    """Main menu for team lineup management"""
    def __init__(self, team_id, team_name, lineup, roster, bot, emoji_id=None, has_starting_lineup=False,
                 is_confirmed=False, lineups_locked=False, not_playing_this_round=False):
        super().__init__(timeout=300)
        self.team_id = team_id
        self.team_name = team_name
        self.lineup = lineup
        self.roster = roster
        self.bot = bot
        self.emoji_id = emoji_id
        self.has_starting_lineup = has_starting_lineup
        self.is_confirmed = is_confirmed
        self.lineups_locked = lineups_locked
        self.not_playing_this_round = not_playing_this_round
        self.warnings = []
        self.injured_slots = set()  # Populated by update_warnings() - which
        self.suspended_slots = set()  # slots to badge in create_menu_embed's field UI

        # Add buttons
        self.add_buttons()

    async def get_injured_players(self):
        """Check for injured players in lineup - returns list of
        (player_name, slot_position) tuples, so the lineup screen shows
        where the injured player currently sits rather than how long
        they're out."""
        injured = []
        player_ids = [p.get('player_id') for p in self.lineup.values() if p.get('player_id')]

        if not player_ids:
            return injured

        slot_by_player_id = {
            p['player_id']: pos_name
            for pos_name, p in self.lineup.items() if p.get('player_id')
        }

        async with aiosqlite.connect(DB_PATH) as db:
            placeholders = ','.join('?' * len(player_ids))
            cursor = await db.execute(
                f"""SELECT p.player_id, p.name
                   FROM injuries i
                   JOIN players p ON i.player_id = p.player_id
                   WHERE i.player_id IN ({placeholders}) AND i.status = 'injured'""",
                player_ids
            )
            injuries = await cursor.fetchall()

            for player_id, name in injuries:
                injured.append((name, slot_by_player_id.get(player_id, "?")))

        return injured

    async def get_suspended_players(self):
        """Check for suspended players in lineup - returns list of
        (player_name, slot_position) tuples, so the lineup screen shows
        where the suspended player currently sits rather than games remaining."""
        suspended = []
        player_ids = [p.get('player_id') for p in self.lineup.values() if p.get('player_id')]

        if not player_ids:
            return suspended

        slot_by_player_id = {
            p['player_id']: pos_name
            for pos_name, p in self.lineup.items() if p.get('player_id')
        }

        async with aiosqlite.connect(DB_PATH) as db:
            placeholders = ','.join('?' * len(player_ids))
            cursor = await db.execute(
                f"""SELECT p.player_id, p.name, s.games_remaining
                   FROM suspensions s
                   JOIN players p ON s.player_id = p.player_id
                   WHERE s.player_id IN ({placeholders}) AND s.status = 'suspended'""",
                player_ids
            )
            suspensions = await cursor.fetchall()

            for player_id, name, games_remaining in suspensions:
                # games_remaining is NULL while suspension length is still
                # TBC (see season_commands.py's _roll_pending_report_suspensions).
                if games_remaining is None or games_remaining > 0:
                    suspended.append((name, slot_by_player_id.get(player_id, "?")))

        return suspended

    def get_duplicate_players(self):
        """Check for duplicate players in lineup - returns list of player names that appear more than once"""
        player_ids = [p.get('player_id') for p in self.lineup.values() if p.get('player_id')]
        duplicates = []
        seen = set()
        for pos_name, player_info in self.lineup.items():
            player_id = player_info.get('player_id')
            if player_id and player_ids.count(player_id) > 1 and player_id not in seen:
                duplicates.append(player_info['name'])
                seen.add(player_id)
        return duplicates

    def get_key_position_overload(self):
        """Check for too many key-position-TYPE players genuinely on-field
        (never interchange) in the backline or forward line - mirrors
        match_sim.py's own in-sim penalty (see
        KEY_POSITION_COUNT_THRESHOLD/KEY_POSITION_OVERLOAD_PENALTY_PER_EXCESS/
        KEY_POSITION_OVERLOAD_GROUPS and _group_strength) so what the
        lineup screen warns about matches what actually costs the team
        strength when simulated. Counts ANY KEY_POSITION_TYPES player
        currently in that line, not just that line's own "natural" key
        positions - a KEY FWD misplaced in defense (or any other
        key-position player in the wrong line) still counts as a tall
        crowding that line, same as match_sim.py's own rule. Returns a
        list of (group_label, count, names) tuples for any line over the
        threshold - empty if neither line is overloaded."""
        line_labels = {"defense": "Backline", "forward": "Forward line"}
        overloads = []
        for group, group_label in line_labels.items():
            names = [
                p['name'] for pos_name, p in self.lineup.items()
                if pos_name not in INTERCHANGE_SLOTS
                and slot_group(pos_name) == group
                and p['pos'] in KEY_POSITION_TYPES
            ]
            if len(names) > KEY_POSITION_COUNT_THRESHOLD:
                overloads.append((group_label, len(names), names))
        return overloads

    async def update_warnings(self):
        """Update the warnings list based on current lineup"""
        self.warnings = []

        # Check for duplicates
        duplicates = self.get_duplicate_players()
        if duplicates:
            self.warnings.append(f"⚠️ **Duplicate players:** {', '.join(duplicates)}")

        # Check for injured players - also cached as a slot set (not just
        # the warnings text) so create_menu_embed can badge the field UI
        # itself, not just list names in the warnings block below it.
        injured = await self.get_injured_players()
        self.injured_slots = {slot for _, slot in injured}
        if injured:
            injured_str = ', '.join([f"{name} ({slot})" for name, slot in injured])
            self.warnings.append(f"🚑 **Injured players:** {injured_str}")

        # Check for suspended players - same slot-set caching as injured above.
        suspended = await self.get_suspended_players()
        self.suspended_slots = {slot for _, slot in suspended}
        if suspended:
            suspended_str = ', '.join([f"{name} ({slot})" for name, slot in suspended])
            self.warnings.append(f"🚫 **Suspended players:** {suspended_str}")

        # Check for too many key position players in one line
        for group_label, count, names in self.get_key_position_overload():
            self.warnings.append(f"❗ **Too many key position players in {group_label}:** {count} ({', '.join(names)})")

    def add_buttons(self):
        """Add all menu buttons"""
        # Once submitted (or once the whole round is locked via
        # /matchsimulation's Announce Lineups button), every editing action
        # is disabled - only Unsubmit Lineup (or, once locked, nothing) can
        # get you back to an editable state.
        locked_for_editing = self.is_confirmed or self.lineups_locked

        # Row 1: Primary actions
        edit_btn = discord.ui.Button(
            label="📝 Edit Lineup", style=discord.ButtonStyle.primary, custom_id="edit_lineup",
            disabled=locked_for_editing
        )
        edit_btn.callback = self.edit_lineup_callback
        self.add_item(edit_btn)

        if self.is_confirmed:
            submit_btn = discord.ui.Button(
                label="↩️ Unsubmit Lineup", style=discord.ButtonStyle.secondary, custom_id="unconfirm_lineup",
                disabled=self.lineups_locked
            )
            submit_btn.callback = self.unconfirm_lineup_callback
        else:
            submit_btn = discord.ui.Button(
                label="✅ Submit Lineup", style=discord.ButtonStyle.success, custom_id="submit_lineup",
                disabled=self.lineups_locked or self.not_playing_this_round
            )
            submit_btn.callback = self.submit_lineup_callback
        self.add_item(submit_btn)

        # Row 2: Main lineup management
        save_btn = discord.ui.Button(
            label="💾 Save as Main Lineup", style=discord.ButtonStyle.secondary, custom_id="save_starting",
            disabled=locked_for_editing
        )
        save_btn.callback = self.save_starting_lineup_callback
        self.add_item(save_btn)

        revert_btn = discord.ui.Button(
            label="🔄 Revert to Main Lineup",
            style=discord.ButtonStyle.secondary,
            custom_id="revert_starting",
            disabled=locked_for_editing or not self.has_starting_lineup
        )
        revert_btn.callback = self.revert_starting_lineup_callback
        self.add_item(revert_btn)

        # Row 3: View and Clear actions
        view_starting_btn = discord.ui.Button(
            label="👁️ View Main Lineup",
            style=discord.ButtonStyle.secondary,
            custom_id="view_starting",
            disabled=not self.has_starting_lineup
        )
        view_starting_btn.callback = self.view_starting_lineup_callback
        self.add_item(view_starting_btn)

        clear_btn = discord.ui.Button(
            label="🗑️ Clear Lineup", style=discord.ButtonStyle.danger, custom_id="clear_lineup",
            disabled=locked_for_editing
        )
        clear_btn.callback = self.clear_lineup_callback
        self.add_item(clear_btn)

    async def edit_lineup_callback(self, interaction: discord.Interaction):
        """Open the lineup editor"""
        if self.is_confirmed:
            await interaction.response.send_message(
                "❌ Your lineup is submitted - select **Unsubmit Lineup** first to make changes.",
                ephemeral=True
            )
            return
        if self.lineups_locked:
            await interaction.response.send_message(
                "❌ Lineups are locked for this round - the round has already been announced.",
                ephemeral=True
            )
            return

        # Create LineupView with current data
        view = LineupView(self.team_id, self.team_name, [], self.roster, self.bot, self.emoji_id)

        # Convert lineup dict to format expected by LineupView
        view.lineup = self.lineup.copy()

        await view.initialize()
        view.add_position_buttons()
        embed = view.create_embed()

        await interaction.response.edit_message(embed=embed, view=view)
        # Store message reference so AutofillButton's confirmation flow can
        # edit this same message later, once its own separate interaction
        # (the confirm button's) is the one live at that point - same
        # pattern as TeamLineupMenu.message above.
        view.message = await interaction.original_response()

    async def submit_lineup_callback(self, interaction: discord.Interaction):
        """Submit the lineup as ready for the current round - no longer
        posts anywhere itself; an admin locks in and posts every team's
        lineup at once via /matchsimulation's Announce Lineups button
        (season_commands.py's _try_announce_lineups). Logs a one-line entry
        to the bot logs channel, then flips the menu's Submit button to
        Unsubmit and locks further edits until that's
        pressed."""
        await interaction.response.defer(ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT current_round, regular_rounds, lineups_locked FROM seasons WHERE status = 'active' LIMIT 1"
            )
            season_info = await cursor.fetchone()
            if not season_info:
                await interaction.followup.send("❌ No active season!", ephemeral=True)
                return
            current_round, regular_rounds, lineups_locked = season_info

            if lineups_locked:
                await interaction.followup.send(
                    "❌ Lineups are locked for this round - the round has already been announced.",
                    ephemeral=True
                )
                return

            if current_round > 0 and not await team_playing_this_round(db, self.team_id, current_round):
                await interaction.followup.send(
                    "❌ Your team isn't playing this round - there's no fixture to submit a lineup for.",
                    ephemeral=True
                )
                return

            errors, player_ids = await validate_lineup(db, self.team_id, current_round)
            if errors:
                await interaction.followup.send("\n".join(errors), ephemeral=True)
                return

            await db.execute(
                "UPDATE teams SET lineup_confirmed = 1 WHERE team_id = ?",
                (self.team_id,)
            )
            await db.commit()

            cursor = await db.execute(
                "SELECT setting_value FROM settings WHERE setting_key = 'bot_logs_channel_id'"
            )
            log_setting = await cursor.fetchone()
            round_display = get_round_name(current_round, regular_rounds) if current_round > 0 else "Offseason"

        if log_setting and log_setting[0]:
            log_channel = self.bot.get_channel(int(log_setting[0]))
            if log_channel:
                emoji_str = get_team_emoji_str(self.bot, self.emoji_id)
                await log_channel.send(f"✅ {emoji_str}has submitted their lineup for {round_display} ({interaction.user.mention})")

        self.is_confirmed = True
        self.clear_items()
        self.add_buttons()
        embed = await self.create_menu_embed()
        await self.message.edit(embed=embed, view=self)

        await interaction.followup.send(
            f"✅ Lineup submitted and ready for Round {current_round}.",
            ephemeral=True
        )

    async def unconfirm_lineup_callback(self, interaction: discord.Interaction):
        """Reverses submit_lineup_callback - clears lineup_confirmed so the
        team's lineup is editable again. Logged the same way as submission,
        so bot logs show both halves of the round-planning back-and-forth."""
        await interaction.response.defer(ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT current_round, regular_rounds, lineups_locked FROM seasons WHERE status = 'active' LIMIT 1"
            )
            season_info = await cursor.fetchone()
            if season_info and season_info[2]:
                await interaction.followup.send(
                    "❌ Lineups are locked for this round - the round has already been announced.",
                    ephemeral=True
                )
                return
            current_round, regular_rounds = (season_info[0], season_info[1]) if season_info else (0, None)

            await db.execute(
                "UPDATE teams SET lineup_confirmed = 0 WHERE team_id = ?",
                (self.team_id,)
            )
            await db.commit()

            cursor = await db.execute(
                "SELECT setting_value FROM settings WHERE setting_key = 'bot_logs_channel_id'"
            )
            log_setting = await cursor.fetchone()
            round_display = get_round_name(current_round, regular_rounds) if current_round > 0 else "Offseason"

        if log_setting and log_setting[0]:
            log_channel = self.bot.get_channel(int(log_setting[0]))
            if log_channel:
                emoji_str = get_team_emoji_str(self.bot, self.emoji_id)
                await log_channel.send(f"↩️ {emoji_str}has unsubmitted their lineup for {round_display} ({interaction.user.mention})")

        self.is_confirmed = False
        self.clear_items()
        self.add_buttons()
        embed = await self.create_menu_embed()
        await self.message.edit(embed=embed, view=self)

        await interaction.followup.send("↩️ Lineup unsubmitted - you can make changes again.", ephemeral=True)

    async def save_starting_lineup_callback(self, interaction: discord.Interaction):
        """Save current lineup as starting lineup - show confirmation first"""
        # Create confirmation view
        confirmation_view = ConfirmActionView(self, 'do_save_starting_lineup', discord.ButtonStyle.success)

        if self.has_starting_lineup:
            message = "⚠️ **Are you sure?**\n\nThis will overwrite your previously saved main lineup with your current lineup."
        else:
            message = "💾 **Save as Main Lineup?**\n\nYour current lineup will be saved and can be restored later."

        await interaction.response.send_message(message, view=confirmation_view, ephemeral=True)

    async def do_save_starting_lineup(self, interaction: discord.Interaction):
        """Actually save the starting lineup after confirmation"""
        # Note: interaction was already responded to by the confirm button

        async with aiosqlite.connect(DB_PATH) as db:
            # Get current lineup from database
            cursor = await db.execute(
                """SELECT position_name, player_id
                   FROM lineups
                   WHERE team_id = ?""",
                (self.team_id,)
            )
            lineup_data = await cursor.fetchall()

            if not lineup_data:
                await interaction.edit_original_response(content="❌ Cannot save empty lineup!", view=None)
                return

            # Convert to JSON
            lineup_json = json.dumps(dict(lineup_data))

            # Save to starting_lineups table
            await db.execute(
                """INSERT OR REPLACE INTO starting_lineups (team_id, lineup_data, last_updated)
                   VALUES (?, ?, CURRENT_TIMESTAMP)""",
                (self.team_id, lineup_json)
            )
            await db.commit()

        # Update button state
        self.has_starting_lineup = True
        self.clear_items()
        self.add_buttons()

        # Refresh the original lineup menu message (not the confirmation message)
        embed = await self.create_menu_embed()
        await self.message.edit(embed=embed, view=self)

        # Edit the confirmation message itself (already edited once above,
        # in ConfirmActionView.confirm_button, to disable its buttons) to
        # its final result text, instead of sending a separate followup -
        # merges "Save as Main Lineup?" and "Main lineup saved!" into one
        # message that just updates in place.
        await interaction.edit_original_response(content="✅ Main lineup saved!", view=None)

    async def revert_starting_lineup_callback(self, interaction: discord.Interaction):
        """Revert to saved starting lineup"""
        await interaction.response.defer(ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as db:
            if await lineups_locked(db):
                await interaction.followup.send(
                    "❌ Lineups are locked for this round - the round has already been announced.",
                    ephemeral=True
                )
                return

            # Get saved starting lineup
            cursor = await db.execute(
                "SELECT lineup_data FROM starting_lineups WHERE team_id = ?",
                (self.team_id,)
            )
            result = await cursor.fetchone()

            if not result:
                await interaction.followup.send("❌ No main lineup saved!", ephemeral=True)
                return

            lineup_data = json.loads(result[0])

            # Clear current lineup
            await db.execute("DELETE FROM lineups WHERE team_id = ?", (self.team_id,))

            # Insert saved lineup
            for position_name, player_id in lineup_data.items():
                slot_number = AFL_POSITIONS.index(position_name) + 1
                await db.execute(
                    "INSERT INTO lineups (team_id, player_id, slot_number, position_name) VALUES (?, ?, ?, ?)",
                    (self.team_id, int(player_id), slot_number, position_name)
                )

            await unconfirm_lineup(db, self.team_id)
            await db.commit()

        await interaction.followup.send("✅ Reverted to main lineup!", ephemeral=True)

        # Refresh the menu with updated lineup
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                """SELECT l.position_name, p.name, p.position, p.overall_rating, p.player_id
                   FROM lineups l
                   JOIN players p ON l.player_id = p.player_id AND p.team_id = l.team_id
                   WHERE l.team_id = ?
                   ORDER BY l.slot_number""",
                (self.team_id,)
            )
            lineup = await cursor.fetchall()

        # Update lineup dict
        self.lineup = {}
        for pos_name, name, pos, rating, player_id in lineup:
            self.lineup[pos_name] = {'name': name, 'pos': pos, 'rating': rating, 'player_id': player_id}

        embed = await self.create_menu_embed()
        await interaction.edit_original_response(embed=embed, view=self)

    async def clear_lineup_callback(self, interaction: discord.Interaction):
        """Clear the entire lineup - show confirmation first"""
        # Create confirmation view
        confirmation_view = ConfirmActionView(self, 'do_clear_lineup', discord.ButtonStyle.danger)
        message = "⚠️ **Clear Lineup?**\n\nThis will remove all players from your lineup. This action cannot be undone."

        await interaction.response.send_message(message, view=confirmation_view, ephemeral=True)

    async def do_clear_lineup(self, interaction: discord.Interaction):
        """Actually clear the lineup after confirmation"""
        # Interaction has already been responded to by the confirmation view

        async with aiosqlite.connect(DB_PATH) as db:
            if await lineups_locked(db):
                await interaction.edit_original_response(
                    content="❌ Lineups are locked for this round - the round has already been announced.",
                    view=None
                )
                return

            await db.execute("DELETE FROM lineups WHERE team_id = ?", (self.team_id,))
            await unconfirm_lineup(db, self.team_id)
            await db.commit()

        self.lineup = {}

        # Get the original message from the parent menu and refresh it
        embed = await self.create_menu_embed()

        # Edit the original lineup menu message (not the confirmation message)
        await self.message.edit(embed=embed, view=self)

        # Edit the confirmation prompt itself into the result, rather than
        # leaving "Clear Lineup?" on screen next to a separate followup.
        await interaction.edit_original_response(content="✅ Lineup cleared!", view=None)

    async def view_starting_lineup_callback(self, interaction: discord.Interaction):
        """Display the saved starting lineup"""
        async with aiosqlite.connect(DB_PATH) as db:
            # Get saved starting lineup
            cursor = await db.execute(
                "SELECT lineup_data FROM starting_lineups WHERE team_id = ?",
                (self.team_id,)
            )
            result = await cursor.fetchone()

            if not result:
                await interaction.response.send_message("❌ No main lineup saved!", ephemeral=True)
                return

            lineup_data = json.loads(result[0])

            # Get player details for the saved lineup
            player_ids = list(lineup_data.values())
            if not player_ids:
                await interaction.response.send_message("❌ Main lineup is empty!", ephemeral=True)
                return

            placeholders = ','.join('?' * len(player_ids))
            cursor = await db.execute(
                f"""SELECT player_id, name, position, overall_rating
                   FROM players
                   WHERE player_id IN ({placeholders})""",
                [int(pid) for pid in player_ids]
            )
            players = await cursor.fetchall()

        # Build player lookup
        player_lookup = {pid: (name, pos, rating) for pid, name, pos, rating in players}

        # Create embed for main lineup
        emoji = get_team_emoji_str(self.bot, self.emoji_id)
        embed = discord.Embed(
            title=f"{emoji}{self.team_name} - Main Lineup",
            color=discord.Color.gold()
        )

        # Display lineup
        rows = [
            ("FB", ["LBP", "FB", "RBP"]),
            ("HB", ["LHB", "CHB", "RHB"]),
            ("C", ["LW", "C", "RW"]),
            ("HF", ["LHF", "CHF", "RHF"]),
            ("FF", ["LFP", "FF", "RFP"]),
            ("Fol", ["R", "RR", "RO"])
        ]

        field_text = ""
        for line_name, positions in rows:
            row_text = []
            for pos_name in positions:
                if pos_name in lineup_data:
                    player_id = int(lineup_data[pos_name])
                    if player_id in player_lookup:
                        name, pos, rating = player_lookup[player_id]
                        row_text.append(f"{name} ({rating})")
                    else:
                        row_text.append("*Unknown*")
                else:
                    row_text.append("*Empty*")
            field_text += f"**{line_name}:**  {', '.join(row_text)}\n"

        field_text += "\n"

        # Interchange
        # Interchange - all 5 on one line
        int_players = []
        for pos_name in ["INT1", "INT2", "INT3", "INT4", "INT5"]:
            if pos_name in lineup_data:
                player_id = int(lineup_data[pos_name])
                if player_id in player_lookup:
                    name, pos, rating = player_lookup[player_id]
                    int_players.append(f"{name} ({rating})")
                else:
                    int_players.append("*Unknown*")
            else:
                int_players.append("*Empty*")
        field_text += f"**Int:**  {', '.join(int_players)}"

        embed.description = field_text
        embed.set_footer(text=f"{len(lineup_data)}/23 positions filled")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def create_menu_embed(self):
        """Create the menu display embed"""
        # Update warnings before creating embed
        await self.update_warnings()

        emoji = get_team_emoji_str(self.bot, self.emoji_id)

        embed = discord.Embed(
            title=f"{emoji}{self.team_name} - Lineup Management",
            color=discord.Color.blue()
        )

        # Display current lineup
        rows = [
            ("FB", ["LBP", "FB", "RBP"]),
            ("HB", ["LHB", "CHB", "RHB"]),
            ("C", ["LW", "C", "RW"]),
            ("HF", ["LHF", "CHF", "RHF"]),
            ("FF", ["LFP", "FF", "RFP"]),
            ("Fol", ["R", "RR", "RO"])
        ]

        def status_badge(pos_name):
            if pos_name in self.injured_slots:
                return " 🚑"
            if pos_name in self.suspended_slots:
                return " 🚫"
            return ""

        field_text = ""
        for line_name, positions in rows:
            row_text = []
            for pos_name in positions:
                if pos_name in self.lineup:
                    p = self.lineup[pos_name]
                    row_text.append(f"{p['name']} ({_display_ovr(pos_name, p)}){status_badge(pos_name)}")
                else:
                    row_text.append("*Empty*")
            field_text += f"**{line_name}:**  {', '.join(row_text)}\n"

        field_text += "\n"

        # Interchange - all 5 on one line
        int_players = []
        for pos_name in ["INT1", "INT2", "INT3", "INT4", "INT5"]:
            if pos_name in self.lineup:
                p = self.lineup[pos_name]
                int_players.append(f"{p['name']} ({p['rating']}){status_badge(pos_name)}")
            else:
                int_players.append("*Empty*")
        field_text += f"**Int:**  {', '.join(int_players)}"

        embed.description = field_text

        # Add warnings if any exist
        if self.warnings:
            embed.add_field(name="\u200b", value="\n".join(self.warnings), inline=False)

        status = f"{len(self.lineup)}/23 positions filled"
        if self.lineups_locked:
            status += " • Lineups locked for this round"
        elif self.not_playing_this_round:
            status += " • Not playing this round - lineup can't be submitted"
        elif self.is_confirmed:
            status += " • Submitted - select Unsubmit Lineup to make changes"
        embed.set_footer(text=status)

        return embed


class ConfirmActionView(discord.ui.View):
    """Generic confirm/cancel view that calls a named async method on the parent menu when confirmed"""
    def __init__(self, parent_menu, action_method_name, confirm_style=discord.ButtonStyle.success):
        super().__init__(timeout=60)
        self.parent_menu = parent_menu
        self.action_method_name = action_method_name
        self.confirm_button.style = confirm_style

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.success)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Disable all buttons and respond to interaction - the action
        # method (e.g. do_save_starting_lineup) is expected to edit THIS
        # SAME confirmation message with its own result text via
        # interaction.edit_original_response, rather than sending a
        # separate followup - so the user sees one message update in
        # place ("Save as Main Lineup?" -> "Main lineup saved!") instead
        # of the prompt staying on screen next to a brand new message.
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

        # Call the parent's action method
        action = getattr(self.parent_menu, self.action_method_name)
        await action(interaction)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Disable all buttons
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="❌ Cancelled.", view=self)


class LineupView(discord.ui.View):
    def __init__(self, team_id, team_name, current_lineup, roster, bot, emoji_id=None):
        super().__init__(timeout=300)  # 5 minute timeout
        self.team_id = team_id
        self.team_name = team_name
        self.roster = roster
        self.bot = bot
        self.emoji_id = emoji_id
        self.selected_position = None  # Track which position is being edited
        self.player_page = 0  # Current page of players in dropdown
        self.warnings = []  # Store lineup warnings
        self.injured_player_ids = set()  # Whole-roster injury/suspension status,
        self.suspended_player_ids = set()  # populated by refresh_status_ids() - used
                                            # to badge PositionSelect/PlayerSelect options
        self.message = None  # Set once by whichever callback first opens this
                              # editor (via interaction.original_response(),
                              # an InteractionMessage bound to that interaction's
                              # own webhook token) - reused by do_autofill later
                              # to edit this same message from a different,
                              # later interaction (the confirm button's own).

        # Build lineup dict (position_name -> player info)
        self.lineup = {}
        for pos_name, name, pos, rating in current_lineup:
            self.lineup[pos_name] = {'name': name, 'pos': pos, 'rating': rating, 'player_id': None}

        # Add position dropdown
        self.add_position_buttons()

    async def initialize(self):
        """Initialize player IDs and warnings (call this after creating the view)"""
        await self.refresh_lineup_ids()
        await self.refresh_status_ids()
        await self.update_warnings()

    async def refresh_status_ids(self):
        """Get injured/suspended player IDs for the whole roster (not just
        players currently in the lineup) - used to badge PositionSelect and
        PlayerSelect options so a coach can see injury/suspension status
        while picking, not just after the fact in the warnings list."""
        roster_ids = [p[0] for p in self.roster]
        if not roster_ids:
            return

        async with aiosqlite.connect(DB_PATH) as db:
            placeholders = ','.join('?' * len(roster_ids))
            cursor = await db.execute(
                f"""SELECT player_id FROM injuries
                   WHERE player_id IN ({placeholders}) AND status = 'injured'""",
                roster_ids
            )
            self.injured_player_ids = {row[0] for row in await cursor.fetchall()}

            cursor = await db.execute(
                f"""SELECT player_id, games_remaining FROM suspensions
                   WHERE player_id IN ({placeholders}) AND status = 'suspended'""",
                roster_ids
            )
            # games_remaining is NULL while suspension length is still TBC
            # (see season_commands.py's _roll_pending_report_suspensions) -
            # treated as still suspended, same as get_suspended_players above.
            self.suspended_player_ids = {
                row[0] for row in await cursor.fetchall() if row[1] is None or row[1] > 0
            }

    async def refresh_lineup_ids(self):
        """Get player IDs for current lineup players"""
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                """SELECT l.position_name, p.player_id FROM lineups l
                   JOIN players p ON l.player_id = p.player_id AND p.team_id = l.team_id
                   WHERE l.team_id = ?""",
                (self.team_id,)
            )
            rows = await cursor.fetchall()
            player_id_by_position = {pos_name: player_id for pos_name, player_id in rows}

            for pos_name in self.lineup:
                if pos_name in player_id_by_position:
                    self.lineup[pos_name]['player_id'] = player_id_by_position[pos_name]
    
    def add_position_buttons(self):
        """Add the position dropdown, player dropdown (if a position is
        selected), and supporting buttons."""
        self.clear_items()

        # Position dropdown - all 23 slots fit in one Select (Discord's
        # cap is 25), replacing the old 4-page backs/mids/forwards/
        # interchange button groups with a single always-visible list.
        self.add_item(PositionSelect(self))

        # Add player select dropdown if a position is selected
        if self.selected_position:
            self.add_item(PlayerSelect(self.selected_position, self))
            # Add pagination buttons if needed
            total_players = self.get_sorted_roster_count()
            if total_players > 25:
                total_pages = (total_players + 24) // 25
                if self.player_page > 0:
                    self.add_item(PrevPageButton(self, total_pages))
                if (self.player_page + 1) * 25 < total_players:
                    self.add_item(NextPageButton(self, total_pages))

        # Add autofill, clear, and main menu buttons
        self.add_item(AutofillButton(self))
        if self.selected_position and self.selected_position in self.lineup:
            self.add_item(ClearPositionButton(self))
        self.add_item(MainMenuButton(self))
    
    def get_sorted_roster(self):
        """Get roster sorted by relevance to the selected slot - players
        whose natural position suffers zero effective_ovr penalty there
        (see fits_without_penalty) sort first, then everyone else, each
        group ordered by rating descending. Uses the exact same
        slot-specific check as auto_fill_lineup rather than a flat
        "any defender for any defensive slot" bucket - e.g. FB/CHB (true
        spine) only rank KEY DEF/RUCK-DEF/SWINGMAN penalty-free, while
        LBP/RBP/LHB/RHB (pockets/flanks) also rank GEN DEF/DEF-MID/UTILITY
        penalty-free, matching Player._compute_effective_ovr's own rules."""
        if not self.selected_position or self.selected_position in INTERCHANGE_SLOTS:
            # No penalty concept on the interchange (see
            # Player._compute_effective_ovr) - every position ties, so
            # there's nothing meaningful to sort by fit.
            return self.roster

        def sort_key(player):
            pos = player[2]  # position
            rating = player[3]  # overall_rating
            fits = fits_without_penalty(pos, self.selected_position)
            return (0 if fits else 1, -rating)

        return sorted(self.roster, key=sort_key)
    
    def get_sorted_roster_count(self):
        """Get count of all players (no filtering - all players can be moved)"""
        return len(self.roster)

    def get_duplicate_players(self):
        """Check for duplicate players in lineup - returns list of player names that appear more than once"""
        player_ids = [p.get('player_id') for p in self.lineup.values() if p.get('player_id')]
        duplicates = []
        seen = set()
        for pos_name, player_info in self.lineup.items():
            player_id = player_info.get('player_id')
            if player_id and player_ids.count(player_id) > 1 and player_id not in seen:
                duplicates.append(player_info['name'])
                seen.add(player_id)
        return duplicates

    def get_key_position_overload(self):
        """Check for too many key-position-TYPE players genuinely on-field
        (never interchange) in the backline or forward line - mirrors
        match_sim.py's own in-sim penalty (see
        KEY_POSITION_COUNT_THRESHOLD/KEY_POSITION_OVERLOAD_PENALTY_PER_EXCESS/
        KEY_POSITION_OVERLOAD_GROUPS and _group_strength) so what the
        lineup screen warns about matches what actually costs the team
        strength when simulated. Counts ANY KEY_POSITION_TYPES player
        currently in that line, not just that line's own "natural" key
        positions - a KEY FWD misplaced in defense (or any other
        key-position player in the wrong line) still counts as a tall
        crowding that line, same as match_sim.py's own rule. Returns a
        list of (group_label, count, names) tuples for any line over the
        threshold - empty if neither line is overloaded."""
        line_labels = {"defense": "Backline", "forward": "Forward line"}
        overloads = []
        for group, group_label in line_labels.items():
            names = [
                p['name'] for pos_name, p in self.lineup.items()
                if pos_name not in INTERCHANGE_SLOTS
                and slot_group(pos_name) == group
                and p['pos'] in KEY_POSITION_TYPES
            ]
            if len(names) > KEY_POSITION_COUNT_THRESHOLD:
                overloads.append((group_label, len(names), names))
        return overloads

    async def get_injured_players(self):
        """Check for injured players in lineup - returns list of
        (player_name, slot_position) tuples, so the lineup screen shows
        where the injured player currently sits rather than how long
        they're out."""
        injured = []
        player_ids = [p.get('player_id') for p in self.lineup.values() if p.get('player_id')]

        if not player_ids:
            return injured

        slot_by_player_id = {
            p['player_id']: pos_name
            for pos_name, p in self.lineup.items() if p.get('player_id')
        }

        async with aiosqlite.connect(DB_PATH) as db:
            placeholders = ','.join('?' * len(player_ids))
            cursor = await db.execute(
                f"""SELECT p.player_id, p.name
                   FROM injuries i
                   JOIN players p ON i.player_id = p.player_id
                   WHERE i.player_id IN ({placeholders}) AND i.status = 'injured'""",
                player_ids
            )
            injuries = await cursor.fetchall()

            for player_id, name in injuries:
                injured.append((name, slot_by_player_id.get(player_id, "?")))

        return injured

    async def get_suspended_players(self):
        """Check for suspended players in lineup - returns list of
        (player_name, slot_position) tuples, so the lineup screen shows
        where the suspended player currently sits rather than games remaining."""
        suspended = []
        player_ids = [p.get('player_id') for p in self.lineup.values() if p.get('player_id')]

        if not player_ids:
            return suspended

        slot_by_player_id = {
            p['player_id']: pos_name
            for pos_name, p in self.lineup.items() if p.get('player_id')
        }

        async with aiosqlite.connect(DB_PATH) as db:
            placeholders = ','.join('?' * len(player_ids))
            cursor = await db.execute(
                f"""SELECT p.player_id, p.name, s.games_remaining
                   FROM suspensions s
                   JOIN players p ON s.player_id = p.player_id
                   WHERE s.player_id IN ({placeholders}) AND s.status = 'suspended'""",
                player_ids
            )
            suspensions = await cursor.fetchall()

            for player_id, name, games_remaining in suspensions:
                # games_remaining is NULL while suspension length is still
                # TBC (see season_commands.py's _roll_pending_report_suspensions).
                if games_remaining is None or games_remaining > 0:
                    suspended.append((name, slot_by_player_id.get(player_id, "?")))

        return suspended

    async def update_warnings(self):
        """Update the warnings list based on current lineup"""
        self.warnings = []

        # Check for duplicates
        duplicates = self.get_duplicate_players()
        if duplicates:
            self.warnings.append(f"⚠️ **Duplicate players:** {', '.join(duplicates)}")

        # Check for injured players
        injured = await self.get_injured_players()
        if injured:
            injured_str = ', '.join([f"{name} ({slot})" for name, slot in injured])
            self.warnings.append(f"🚑 **Injured players:** {injured_str}")

        # Check for suspended players
        suspended = await self.get_suspended_players()
        if suspended:
            suspended_str = ', '.join([f"{name} ({slot})" for name, slot in suspended])
            self.warnings.append(f"🚫 **Suspended players:** {suspended_str}")

        # Check for too many key position players in one line
        for group_label, count, names in self.get_key_position_overload():
            self.warnings.append(f"❗ **Too many key position players in {group_label}:** {count} ({', '.join(names)})")

    async def do_autofill(self, interaction: discord.Interaction):
        """Actually run autofill after confirmation - called by
        ConfirmActionView via AutofillButton. Fills every empty slot and
        replaces every invalid occupant (injured, suspended, or a duplicate
        elsewhere in the lineup) using auto_fill_lineup - the same repair
        logic the Announce Lineups panel's Force-submit button uses. Valid
        slots are never touched. interaction here is the CONFIRM button's
        own interaction (already responded to by ConfirmActionView) - its
        edit_original_response updates the confirmation prompt itself,
        while self.message (captured once when this editor was first
        opened - see edit_lineup_callback) is what gets the real lineup
        editor update, from this separate, later interaction."""
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT current_round, lineups_locked FROM seasons WHERE status = 'active' LIMIT 1"
            )
            season_info = await cursor.fetchone()
            if not season_info:
                await interaction.edit_original_response(content="❌ No active season!", view=None)
                return
            current_round, is_locked = season_info

            if is_locked:
                await interaction.edit_original_response(
                    content="❌ Lineups are locked for this round - the round has already been announced.",
                    view=None
                )
                return

            changes, unfilled = await auto_fill_lineup(db, self.team_id, current_round)
            await unconfirm_lineup(db, self.team_id)
            await db.commit()

            # Rebuild self.lineup from the DB rather than patching it in
            # place - auto_fill_lineup can move players between slots (an
            # interchange-borrow backfill) beyond just the slots named in
            # `changes`' own text, so a full re-read is the only way to be
            # sure the view matches what's actually now in the lineups table.
            cursor = await db.execute(
                """SELECT l.position_name, p.name, p.position, p.overall_rating, p.player_id
                   FROM lineups l
                   JOIN players p ON l.player_id = p.player_id AND p.team_id = l.team_id
                   WHERE l.team_id = ?""",
                (self.team_id,)
            )
            lineup_rows = await cursor.fetchall()

        self.lineup = {
            pos_name: {'name': name, 'pos': pos, 'rating': rating, 'player_id': player_id}
            for pos_name, name, pos, rating, player_id in lineup_rows
        }

        await self.refresh_status_ids()
        await self.update_warnings()
        self.add_position_buttons()
        embed = self.create_embed()
        await self.message.edit(embed=embed, view=self)

        if not changes:
            result_text = "✅ Autofill made no changes - every slot was already valid."
        else:
            result_text = f"✅ Autofill updated {len(changes)} slot(s)."
            if unfilled:
                result_text += f" Could not fill: {', '.join(unfilled)} (roster exhausted)."
        await interaction.edit_original_response(content=result_text, view=None)

    def create_embed(self):
        """Create the lineup display embed"""
        rows = [
            ("FB", ["LBP", "FB", "RBP"]),
            ("HB", ["LHB", "CHB", "RHB"]),
            ("C", ["LW", "C", "RW"]),
            ("HF", ["LHF", "CHF", "RHF"]),
            ("FF", ["LFP", "FF", "RFP"]),
            ("Fol", ["R", "RR", "RO"])
        ]
        
        # Get team emoji
        emoji = get_team_emoji_str(self.bot, self.emoji_id)

        title = f"{emoji}{self.team_name} - Lineup Editor"
        
        embed = discord.Embed(
            title=title,
            description="Select a position from the dropdown, then choose a player to fill it.",
            color=discord.Color.green()
        )
        
        def status_badge(player_info):
            player_id = player_info.get('player_id')
            if player_id in self.injured_player_ids:
                return " 🚑"
            if player_id in self.suspended_player_ids:
                return " 🚫"
            return ""

        # Show field positions
        field_text = ""
        for line_name, positions in rows:
            row_text = []
            for pos_name in positions:
                prefix = "→ " if pos_name == self.selected_position else ""
                if pos_name in self.lineup:
                    p = self.lineup[pos_name]
                    row_text.append(f"{prefix}{p['name']} ({_display_ovr(pos_name, p)}){status_badge(p)}")
                else:
                    row_text.append(f"{prefix}*Empty*")
            field_text += f"**{line_name}:**  {', '.join(row_text)}\n"

        # Add spacing before interchange
        field_text += "\n"

        # Show interchange - all 5 on one line
        int_players = []
        for pos_name in ["INT1", "INT2", "INT3", "INT4", "INT5"]:
            prefix = "→ " if pos_name == self.selected_position else ""
            if pos_name in self.lineup:
                p = self.lineup[pos_name]
                int_players.append(f"{prefix}{p['name']} ({p['rating']}){status_badge(p)}")
            else:
                int_players.append(f"{prefix}*Empty*")

        field_text += f"**Int:**  {', '.join(int_players)}"
        
        embed.add_field(name="\u200b", value=field_text, inline=False)

        # Add warnings if any exist
        if self.warnings:
            embed.add_field(name="\u200b", value="\n".join(self.warnings), inline=False)

        embed.set_footer(text=f"{len(self.lineup)}/23 positions filled • {len(self.roster)} players available")

        return embed


class PositionSelect(discord.ui.Select):
    """Single dropdown listing all 23 lineup slots (fits Discord's 25-option
    cap), replacing the old 4-page backs/mids/forwards/interchange button
    groups. Each option shows the slot's current occupant (or "Empty") so
    the whole lineup's fill state is visible without extra clicks."""
    def __init__(self, parent_view):
        self.parent_view = parent_view

        # age isn't stored on parent_view.lineup entries - looked up from
        # the roster tuples (player_id, name, pos, rating, age) by player_id
        # instead, matching PlayerSelect's own "(pos, age, rating)" format.
        age_by_player_id = {p[0]: p[4] for p in parent_view.roster}

        options = []
        for pos_name in AFL_POSITIONS:
            if pos_name in parent_view.lineup:
                p = parent_view.lineup[pos_name]
                age = age_by_player_id.get(p.get('player_id'))
                age_part = f", {age}" if age is not None else ""
                description = f"{p['name']} ({p['pos']}{age_part}, {_display_ovr(pos_name, p)})"
                badge = ""
                player_id = p.get('player_id')
                if player_id in parent_view.injured_player_ids:
                    badge = " 🚑"
                elif player_id in parent_view.suspended_player_ids:
                    badge = " 🚫"
                description += badge
            else:
                description = "Empty"
            options.append(
                discord.SelectOption(
                    label=pos_name,
                    description=description,
                    value=pos_name,
                    default=(pos_name == parent_view.selected_position)
                )
            )

        super().__init__(
            placeholder="Select a position to edit...",
            options=options,
            custom_id="position_select"
        )

    async def callback(self, interaction: discord.Interaction):
        # Select this position for editing and reset to first page
        self.parent_view.selected_position = self.values[0]
        self.parent_view.player_page = 0
        self.parent_view.add_position_buttons()

        embed = self.parent_view.create_embed()
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class PrevPageButton(discord.ui.Button):
    def __init__(self, parent_view, total_pages):
        prev_page = parent_view.player_page  # 1-indexed page being navigated to
        super().__init__(label=f"◀ Page {prev_page}/{total_pages}", style=discord.ButtonStyle.secondary, row=2)
        self.parent_view = parent_view
    
    async def callback(self, interaction: discord.Interaction):
        self.parent_view.player_page -= 1
        self.parent_view.add_position_buttons()
        embed = self.parent_view.create_embed()
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class NextPageButton(discord.ui.Button):
    def __init__(self, parent_view, total_pages):
        next_page = parent_view.player_page + 2  # 1-indexed page being navigated to
        super().__init__(label=f"Page {next_page}/{total_pages} ▶", style=discord.ButtonStyle.secondary, row=2)
        self.parent_view = parent_view
    
    async def callback(self, interaction: discord.Interaction):
        self.parent_view.player_page += 1
        self.parent_view.add_position_buttons()
        embed = self.parent_view.create_embed()
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class AutofillButton(discord.ui.Button):
    def __init__(self, parent_view):
        super().__init__(label="⚡ Autofill", style=discord.ButtonStyle.success, row=3)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        """Shows a confirmation prompt before running auto_fill_lineup -
        the actual work happens in LineupView.do_autofill once confirmed.
        self.parent_view.message (captured once when this editor was first
        opened - see edit_lineup_callback) is what gets the real lineup
        editor update once confirmed, since the confirm button click is a
        separate, later interaction with no direct reference of its own
        back to this message."""
        confirmation_view = ConfirmActionView(self.parent_view, 'do_autofill', discord.ButtonStyle.success)
        message = (
            "⚡ **Autofill?**\n\nAutofill will automatically fill empty position slots "
            "and replace injured/suspended players. Are you sure you wish to continue?"
        )
        await interaction.response.send_message(message, view=confirmation_view, ephemeral=True)


class ClearPositionButton(discord.ui.Button):
    def __init__(self, parent_view):
        super().__init__(label="✗ Clear", style=discord.ButtonStyle.danger, row=3)
        self.parent_view = parent_view
    
    async def callback(self, interaction: discord.Interaction):
        pos_name = self.parent_view.selected_position

        # Remove from database
        async with aiosqlite.connect(DB_PATH) as db:
            if await lineups_locked(db):
                await interaction.response.send_message(
                    "❌ Lineups are locked for this round - the round has already been announced.",
                    ephemeral=True
                )
                return

            await db.execute(
                "DELETE FROM lineups WHERE team_id = ? AND position_name = ?",
                (self.parent_view.team_id, pos_name)
            )
            await unconfirm_lineup(db, self.parent_view.team_id)
            await db.commit()

        # Remove from lineup
        if pos_name in self.parent_view.lineup:
            del self.parent_view.lineup[pos_name]

        # Update warnings and refresh view
        await self.parent_view.update_warnings()
        self.parent_view.add_position_buttons()
        embed = self.parent_view.create_embed()

        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class PlayerSelect(discord.ui.Select):
    def __init__(self, position_name, parent_view):
        self.position_name = position_name
        self.parent_view = parent_view
        
        # Get sorted roster
        sorted_roster = parent_view.get_sorted_roster()

        # Get players already in lineup (for display purposes)
        used_ids = {p.get('player_id') for p in parent_view.lineup.values() if p.get('player_id')}

        # Build options from all players with pagination
        options = []
        start_idx = parent_view.player_page * 25
        end_idx = start_idx + 25
        count = 0
        added = 0

        for player_id, name, pos, rating, age in sorted_roster:
            # Show all players - they can be moved between positions
            # Check if this player is in the current page
            if count >= start_idx and added < 25:
                # Label (1st line) - name + "Currently in X" if applicable.
                label = name

                # Mark if player is currently in lineup - including the
                # slot being edited itself, so it's clear who currently
                # holds that position (not just where everyone else is).
                if player_id in used_ids:
                    # Find which position they're in
                    current_pos = None
                    for pos_name, player_info in parent_view.lineup.items():
                        if player_info.get('player_id') == player_id:
                            current_pos = pos_name
                            break
                    if current_pos:
                        label += f" - Currently in {current_pos}"

                # Description (2nd line) - stats, then an injury/suspension
                # badge if applicable.
                description = f"{pos}, {age}, {rating}"
                if player_id in parent_view.injured_player_ids:
                    description += " - 🚑 Injured"
                elif player_id in parent_view.suspended_player_ids:
                    description += " - 🚫 Suspended"

                options.append(
                    discord.SelectOption(
                        label=label,
                        description=description,
                        value=str(player_id)
                    )
                )
                added += 1

            count += 1
            if added >= 25:
                break
        
        if not options:
            options.append(discord.SelectOption(label="No players available", value="none"))

        # Add page indicator to placeholder
        total_available = len(sorted_roster)  # Total number of players in roster
        current_page = parent_view.player_page + 1
        total_pages = (total_available + 24) // 25
        placeholder = f"Select player for {position_name} (Page {current_page}/{total_pages})"
        
        super().__init__(
            placeholder=placeholder,
            options=options,
            custom_id=f"player_select_{position_name}"
        )
    
    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.defer()
            return

        player_id = int(self.values[0])

        # Get slot number for this position
        slot_number = AFL_POSITIONS.index(self.position_name) + 1

        # Update lineup in database
        async with aiosqlite.connect(DB_PATH) as db:
            if await lineups_locked(db):
                await interaction.response.send_message(
                    "❌ Lineups are locked for this round - the round has already been announced.",
                    ephemeral=True
                )
                return

            # Remove player from any existing position
            await db.execute(
                "DELETE FROM lineups WHERE team_id = ? AND player_id = ?",
                (self.parent_view.team_id, player_id)
            )

            # Add to new position
            await db.execute(
                "INSERT OR REPLACE INTO lineups (team_id, player_id, slot_number, position_name) VALUES (?, ?, ?, ?)",
                (self.parent_view.team_id, player_id, slot_number, self.position_name)
            )
            await unconfirm_lineup(db, self.parent_view.team_id)
            await db.commit()
            
            # Get player info
            cursor = await db.execute(
                "SELECT name, position, overall_rating FROM players WHERE player_id = ?",
                (player_id,)
            )
            name, pos, rating = await cursor.fetchone()

        # Remove player from old position in lineup dict (if they were in a different position)
        for pos_name, player_info in list(self.parent_view.lineup.items()):
            if player_info.get('player_id') == player_id and pos_name != self.position_name:
                # Delete the old position entry so it shows as "Empty"
                del self.parent_view.lineup[pos_name]

        # Update parent view with new position
        self.parent_view.lineup[self.position_name] = {
            'name': name,
            'pos': pos,
            'rating': rating,
            'player_id': player_id
        }

        # Reset to first page, update warnings, and refresh view
        self.parent_view.player_page = 0
        await self.parent_view.update_warnings()
        self.parent_view.add_position_buttons()
        embed = self.parent_view.create_embed()

        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class MainMenuButton(discord.ui.Button):
    def __init__(self, parent_view):
        super().__init__(label="🏠 Main Menu", style=discord.ButtonStyle.primary, row=3)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        # Return to TeamLineupMenu
        async with aiosqlite.connect(DB_PATH) as db:
            # Get updated lineup
            cursor = await db.execute(
                """SELECT l.position_name, p.name, p.position, p.overall_rating, p.player_id
                   FROM lineups l
                   JOIN players p ON l.player_id = p.player_id AND p.team_id = l.team_id
                   WHERE l.team_id = ?
                   ORDER BY l.slot_number""",
                (self.parent_view.team_id,)
            )
            lineup_data = await cursor.fetchall()

            # Check if starting lineup exists
            cursor = await db.execute(
                "SELECT 1 FROM starting_lineups WHERE team_id = ?",
                (self.parent_view.team_id,)
            )
            has_starting_lineup = await cursor.fetchone() is not None

        # Build lineup dict
        lineup = {}
        for pos_name, name, pos, rating, player_id in lineup_data:
            lineup[pos_name] = {'name': name, 'pos': pos, 'rating': rating, 'player_id': player_id}

        # Create TeamLineupMenu
        menu_view = TeamLineupMenu(
            self.parent_view.team_id,
            self.parent_view.team_name,
            lineup,
            self.parent_view.roster,
            self.parent_view.bot,
            self.parent_view.emoji_id,
            has_starting_lineup
        )
        embed = await menu_view.create_menu_embed()

        await interaction.response.edit_message(embed=embed, view=menu_view)

        # Store message reference so the view can edit it later
        menu_view.message = await interaction.original_response()


async def setup(bot):
    await bot.add_cog(LineupCommands(bot))