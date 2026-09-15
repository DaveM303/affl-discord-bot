import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from config import DB_PATH, ADMIN_ROLE_ID
from utils import is_admin_user, get_team_emoji_str
from ladder_image import fetch_team_icons, render_ladder_image, split_ladder_for_images
import finals_bracket as finals_bracket_module
from match_sim import INJURY_DIAGNOSES, REPORT_REASONS

# Finals structure - a 10-team, 5-week AFL finals series starting
# immediately after the last regular-season round (no pre-finals bye). Each
# entry is the WEEK-level label (used for round headers/panel titles) - see
# FINALS_SLOT_LABELS below for each individual MATCH's own full name.
FINALS_ROUNDS = [
    "Wildcard Finals",
    "Qualifying & Elimination Finals",
    "Semi Finals",
    "Preliminary Finals",
    "Grand Final",
]

# Individual finals MATCH names, keyed by their bracket slot_code (see
# finals_bracket.py) - these are the real AFL names for each match, not
# abbreviations of the week-level FINALS_ROUNDS labels above. Qualifying
# and Elimination Finals both fall in the same week but are genuinely
# different match names, not two instances of one generic label. Used
# anywhere a specific match (not a whole round) needs a title - the live
# match intro embed, and each fixture line on /matchsimulation's panel.
FINALS_SLOT_LABELS = {
    "WC1": "Wildcard Final 1",
    "WC2": "Wildcard Final 2",
    "QF1": "Qualifying Final 1",
    "QF2": "Qualifying Final 2",
    "EF1": "Elimination Final 1",
    "EF2": "Elimination Final 2",
    "SF1": "Semi Final 1",
    "SF2": "Semi Final 2",
    "PF1": "Preliminary Final 1",
    "PF2": "Preliminary Final 2",
    "GF": "Grand Final",
}

def get_round_name(round_num, regular_season_rounds):
    """Get the name for a given round number"""
    if round_num <= regular_season_rounds:
        return f"Round {round_num}"
    else:
        # Finals rounds
        finals_index = round_num - regular_season_rounds - 1
        if finals_index < len(FINALS_ROUNDS):
            return FINALS_ROUNDS[finals_index]
        else:
            return f"Round {round_num}"


class _LadderRow:
    """Same AFL ladder math as sim_season.py's LadderRow (that script's
    in-memory season-preview tool) - ported here as the real thing, computed
    from actual simulated matches rather than a hypothetical run."""
    def __init__(self, team_id, team_name):
        self.team_id = team_id
        self.team_name = team_name
        self.wins = 0
        self.losses = 0
        self.draws = 0
        self.points_for = 0
        self.points_against = 0
        # Last 5 results THIS SEASON ONLY, oldest to newest, each "W"/"L"/"D"
        # - see compute_and_store_ladder, which scopes its whole match query
        # to season_id so this can never bleed in a prior season's form.
        self.form = []

    @property
    def percentage(self):
        if self.points_against == 0:
            return 100.0 if self.points_for == 0 else float("inf")
        return 100.0 * self.points_for / self.points_against

    @property
    def premiership_points(self):
        # Standard AFL premiership points: 4 for a win, 2 for a draw, 0 for
        # a loss - distinct from points_for/points_against (the SCORE
        # totals used for percentage), despite the similar name.
        return self.wins * 4 + self.draws * 2

    @property
    def sort_key(self):
        # Wins first (draws count as half a win for ranking), percentage
        # as the tiebreaker - standard AFL ladder ordering.
        return (-(self.wins + 0.5 * self.draws), -self.percentage)


async def compute_and_store_ladder(db, season_id):
    """Recomputes the competitive ladder from every simulated REGULAR-SEASON
    match (round_number <= regular_rounds) and writes it into
    ladder_positions, replacing that season's rows - the same table the
    indicative draft order (update_indicative_draft_order) and free
    agency's ladder-position tiebreak/compensation-order logic
    (draft_commands.py, free_agency_commands.py) both read, so they pick
    this up with no changes on their end. Called once a round's matches are all
    simulated (see MatchCommands.sim_round) and from /ladder. Finals
    matches are deliberately excluded - the AFL finals series doesn't
    change the competitive ladder, only the season's final DRAFT order
    (see finalize_finals_ladder, called separately once the Grand Final
    concludes). Returns the ranked list of _LadderRow, in ladder order (1st
    place first), for the caller to use when building round-summary
    embeds."""
    cursor = await db.execute(
        "SELECT team_id, team_name FROM teams WHERE team_name != 'Draft Pool'"
    )
    teams = await cursor.fetchall()
    ladder = {team_id: _LadderRow(team_id, team_name) for team_id, team_name in teams}

    cursor = await db.execute(
        "SELECT regular_rounds FROM seasons WHERE season_id = ?", (season_id,)
    )
    season_row = await cursor.fetchone()
    regular_rounds = season_row[0] if season_row else None

    cursor = await db.execute(
        """SELECT home_team_id, away_team_id, home_score, away_score
           FROM matches WHERE season_id = ? AND simulated = 1
           AND (? IS NULL OR round_number <= ?)
           ORDER BY round_number""",
        (season_id, regular_rounds, regular_rounds)
    )
    matches = await cursor.fetchall()

    for home_team_id, away_team_id, home_score, away_score in matches:
        home_row = ladder.get(home_team_id)
        away_row = ladder.get(away_team_id)
        if home_row is None or away_row is None:
            continue  # a team no longer exists - skip rather than crash

        home_row.points_for += home_score
        home_row.points_against += away_score
        away_row.points_for += away_score
        away_row.points_against += home_score

        if home_score > away_score:
            home_row.wins += 1
            away_row.losses += 1
            home_result, away_result = "W", "L"
        elif away_score > home_score:
            away_row.wins += 1
            home_row.losses += 1
            home_result, away_result = "L", "W"
        else:
            home_row.draws += 1
            away_row.draws += 1
            home_result = away_result = "D"

        # Rolling last-5 window, oldest to newest - matches are processed in
        # round order (see ORDER BY above), so appending-then-trimming here
        # always leaves exactly the 5 most recent results, THIS SEASON ONLY
        # (the query above is already scoped to season_id).
        home_row.form.append(home_result)
        home_row.form = home_row.form[-5:]
        away_row.form.append(away_result)
        away_row.form = away_row.form[-5:]

    ranked = sorted(ladder.values(), key=lambda row: row.sort_key)

    await db.execute("DELETE FROM ladder_positions WHERE season_id = ?", (season_id,))
    for position, row in enumerate(ranked, start=1):
        await db.execute(
            "INSERT INTO ladder_positions (season_id, team_id, position) VALUES (?, ?, ?)",
            (season_id, row.team_id, position)
        )
    await db.commit()

    return ranked


async def _insert_finals_match(db, season_id, round_number, slot_code, home_team_id, away_team_id):
    """Inserts one finals fixture - a `matches` row (unsimulated, 0-0)
    plus its `finals_bracket` row recording which slot this is, with
    `match_id` filled in immediately so later rounds can look the result
    back up by slot_code. Returns the new match_id."""
    cursor = await db.execute(
        """INSERT INTO matches (season_id, round_number, home_team_id, away_team_id, home_score, away_score, simulated)
           VALUES (?, ?, ?, ?, 0, 0, 0)""",
        (season_id, round_number, home_team_id, away_team_id)
    )
    match_id = cursor.lastrowid
    await db.execute(
        """INSERT INTO finals_bracket (season_id, round_number, slot_code, home_team_id, away_team_id, match_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (season_id, round_number, slot_code, home_team_id, away_team_id, match_id)
    )
    return match_id


async def _finals_slot_result(db, season_id, slot_code):
    """Reads back one finals slot's decided result as (winner_team_id,
    loser_team_id) - joins finals_bracket to matches on match_id. Assumes
    the match is simulated (callers only reach this once
    advance_to_next_round's own unsimulated-matches guard has already
    confirmed the whole round is done) and decisive (finals matches are
    live-mode-only with Start Extra Time mandatory on a draw - see
    LiveMatchControlView - so simulated=1 here always means a real winner)."""
    cursor = await db.execute(
        """SELECT fb.home_team_id, fb.away_team_id, m.home_score, m.away_score
           FROM finals_bracket fb
           JOIN matches m ON fb.match_id = m.match_id
           WHERE fb.season_id = ? AND fb.slot_code = ?""",
        (season_id, slot_code)
    )
    row = await cursor.fetchone()
    home_team_id, away_team_id, home_score, away_score = row
    if home_score > away_score:
        return home_team_id, away_team_id
    return away_team_id, home_team_id


async def get_eliminated_finals_team_ids(db, season_id):
    """Returns the set of team_ids no longer alive in the current finals
    series - i.e. out of premiership contention. Two ways in:

    1. Never qualified at all - finished 11th or lower on the frozen
       regular-season ladder (ladder_positions), so they have no bracket
       slot this finals series regardless of what's been simulated so far.
       Without this, a team that missed finals entirely would incorrectly
       keep appearing in league-wide lists all the way through Wildcard
       week, since it's never actually PLAYED a finals match to lose - the
       loss-based check below only ever excludes a team that has already
       been eliminated FROM the bracket, not one that was never in it.
    2. LOSES a Wildcard, Elimination, Semi, or Preliminary final - a
       QUALIFYING final loss is the only one that's NOT eliminating (that
       team drops into the Semis instead, per generate_semis_round in
       finals_bracket.py: "SF1: QF1 loser vs EF1 winner"), so only QF1/QF2
       are excluded here. Only checks slots that have actually been
       simulated so far, so this naturally grows as each terminal round is
       decided.

    Returns an empty set outside the finals entirely (regular_rounds/
    current_round gate this at the caller - see build_injury_suspension_list)."""
    cursor = await db.execute(
        "SELECT team_id FROM ladder_positions WHERE season_id = ? AND position > 10",
        (season_id,)
    )
    eliminated = {row[0] for row in await cursor.fetchall()}

    for slot_code in ("WC1", "WC2", "EF1", "EF2", "SF1", "SF2", "PF1", "PF2"):
        cursor = await db.execute(
            """SELECT fb.home_team_id, fb.away_team_id, m.home_score, m.away_score
               FROM finals_bracket fb
               JOIN matches m ON fb.match_id = m.match_id
               WHERE fb.season_id = ? AND fb.slot_code = ? AND m.simulated = 1""",
            (season_id, slot_code)
        )
        row = await cursor.fetchone()
        if row is None:
            continue
        home_team_id, away_team_id, home_score, away_score = row
        loser_team_id = away_team_id if home_score > away_score else home_team_id
        eliminated.add(loser_team_id)
    return eliminated


# Elimination stages in worst-to-best draft-pick order, once finals are
# underway - a team eliminated at an earlier stage picks EARLIER (worse
# draft slot for them, since they're worse) than one eliminated later.
# Non-qualifiers (11th+) are their own implicit stage, worse than every
# real finals loss - see _indicative_draft_order below. QF1/QF2 are
# deliberately excluded (a Qualifying Final loss isn't eliminating - that
# team drops into the Semis instead, same reasoning as
# get_eliminated_finals_team_ids).
_FINALS_ELIMINATION_STAGES = (
    ("WC1", "WC2"),
    ("EF1", "EF2"),
    ("SF1", "SF2"),
    ("PF1", "PF2"),
    ("GF",),
)


async def _indicative_draft_order(db, season_id, regular_rounds, current_round):
    """Returns the CURRENT best-guess reverse order (worst team first, i.e.
    pick 1) for the National Draft based on this season's ladder as it
    stands RIGHT NOW - the frozen regular-season order before finals start,
    or an indicative order that updates as finals results land once
    they're underway. Used to keep next season's 'future' draft's
    draft_picks.pick_number in sync every round (see
    update_indicative_draft_order) so /draftorder is meaningfully
    browsable mid-season, not just after the real thing is decided.

    Regular season (current_round <= regular_rounds): just the frozen
    ladder_positions order - nothing finals-specific to account for yet.

    Finals underway: any team STILL ALIVE (not eliminated, including one
    that hasn't played its next match yet) is ranked ahead of every
    eliminated team, regardless of finals stage - deliberately not trying
    to predict whose bracket path is "easier" for a team that hasn't
    played yet. Eliminated teams are ordered worst-to-best by the STAGE
    they were knocked out at (non-qualifiers first, then Wildcard-round
    losers, then Elimination-final losers, etc - see
    _FINALS_ELIMINATION_STAGES), with same-stage ties (both Wildcard
    losers, both Elimination-final losers, etc, and all non-qualifiers)
    broken by ORIGINAL regular-season ladder rank - worse original rank
    picks earlier, matching compute_final_finish_order's own tie-break
    convention for consistency once the real thing IS decided.

    Returns a list of team_ids, pick-1 (worst) first."""
    cursor = await db.execute(
        "SELECT team_id FROM ladder_positions WHERE season_id = ? ORDER BY position", (season_id,)
    )
    ranked_team_ids = [row[0] for row in await cursor.fetchall()]

    if current_round <= regular_rounds or not ranked_team_ids:
        return list(reversed(ranked_team_ids))

    original_rank = {team_id: i for i, team_id in enumerate(ranked_team_ids)}
    non_qualifiers = [team_id for team_id in ranked_team_ids if original_rank[team_id] >= 10]

    # Stage-by-stage losers, in the SAME worst-to-best order the final
    # draft order will eventually use (see compute_final_finish_order) -
    # only ever includes a stage's losers once that stage's matches have
    # actually been simulated.
    stage_loser_groups = [sorted(non_qualifiers, key=lambda t: -original_rank[t])]
    already_placed = set(non_qualifiers)

    for stage_slots in _FINALS_ELIMINATION_STAGES:
        stage_losers = []
        for slot_code in stage_slots:
            cursor = await db.execute(
                """SELECT fb.home_team_id, fb.away_team_id, m.home_score, m.away_score
                   FROM finals_bracket fb
                   JOIN matches m ON fb.match_id = m.match_id
                   WHERE fb.season_id = ? AND fb.slot_code = ? AND m.simulated = 1""",
                (season_id, slot_code)
            )
            row = await cursor.fetchone()
            if row is None:
                continue
            home_team_id, away_team_id, home_score, away_score = row
            loser_team_id = away_team_id if home_score > away_score else home_team_id
            stage_losers.append(loser_team_id)
        stage_losers.sort(key=lambda t: -original_rank[t])
        stage_loser_groups.append(stage_losers)
        already_placed.update(stage_losers)

    still_alive = sorted(
        (team_id for team_id in ranked_team_ids if team_id not in already_placed),
        key=lambda t: -original_rank[t]
    )

    order = []
    for group in stage_loser_groups:
        order.extend(group)
    order.extend(still_alive)
    return order


async def _ensure_draft_promoted_to_current(db, season_number):
    """Finds the National Draft indicative for the season currently being
    played (season_number) - i.e. the one named after it, stored as
    drafts.season_number = season_number + 1 per ensure_future_seasons_exist's
    naming convention - and promotes it from 'future' to 'current' if it
    isn't already, so /draftorder becomes browsable and
    update_indicative_draft_order (which only ever touches a 'future'
    draft) starts actually updating it.

    Called from /startseason (the normal, expected trigger) AND
    defensively from advance_to_next_round every round, so a season
    started before this feature existed - or one where the promotion was
    somehow missed - self-heals the first time a round advances instead of
    silently staying stuck on 'future' forever with no indicative order
    ever showing.

    Guarded to only ever touch the ONE draft with the exact season_number
    match, so free_agency_commands.py's own `WHERE status = 'current'`
    compensation-pick lookup can never find two candidates at once even if
    a previous season's draft was somehow left un-started.

    Returns the draft's name if it was JUST promoted this call (so a
    caller can announce "now indicative"), or None if it was already
    'current'/'in_progress'/'completed' or doesn't exist at all - a caller
    that doesn't care about the distinction can just check truthiness."""
    draft_season_number = season_number + 1
    cursor = await db.execute(
        "SELECT draft_id, draft_name, status FROM drafts WHERE season_number = ?",
        (draft_season_number,)
    )
    draft = await cursor.fetchone()
    if not draft:
        return None
    draft_id, draft_name, status = draft
    if status != 'future':
        return None

    await db.execute(
        "UPDATE drafts SET status = 'current', ladder_set_at = CURRENT_TIMESTAMP WHERE draft_id = ?",
        (draft_id,)
    )
    await db.commit()
    return draft_name


async def update_indicative_draft_order(db, season_id, season_number, regular_rounds, current_round):
    """Keeps next season's National Draft's pick_number in sync with THIS
    season's ladder every round, so /draftorder shows a meaningful
    "indicative" order all season long rather than a fixed order set only
    once. Called from advance_to_next_round, right after
    compute_and_store_ladder (regular season) / each finals result (see
    _indicative_draft_order above for what "this season's ladder" means
    once finals are underway).

    Deliberately does NOT touch current_team_id, round_number, or insert/
    delete any draft_picks rows - only pick_number is re-ranked in place.
    Wiping and regenerating picks (as /createcustomdraft does once, at
    creation, for a one-off custom draft) would silently undo any
    draft-pick TRADE made against this draft while it's still
    'future'/'current' (draft_picks has no status guard of its own - see
    trade_commands.py, which happily trades a future draft's picks).
    Re-ranking in place preserves both trades and the pick's own
    round_number untouched, only ever changing WHERE in the order it
    falls as the ladder shifts.

    A pick's rank is keyed on original_team_id (who EARNED the pick by
    ladder position), never current_team_id (who owns it now via trades) -
    a traded pick keeps tracking its original team's ladder position
    throughout the season, per the pick's own pick_origin naming.

    No-ops safely if next season's draft doesn't exist yet, has already
    STARTED ('in_progress'/'completed' - an admin is actively drafting or
    finished, so pick order is hands-off from here on), or this season's
    ladder_positions is empty (nothing simulated yet).

    Runs against BOTH 'future' and 'current' drafts - 'current' means
    _ensure_draft_promoted_to_current has already flipped it once the
    season it's indicative for actually started (see that function,
    called from advance_to_next_round immediately before this). Excluding
    'current' here would silently stop the very auto-updates that
    promotion is supposed to enable."""
    next_season_number = season_number + 1
    cursor = await db.execute(
        "SELECT draft_id, rounds FROM drafts WHERE season_number = ? AND status IN ('future', 'current')",
        (next_season_number,)
    )
    draft_row = await cursor.fetchone()
    if not draft_row:
        return
    draft_id, rounds = draft_row

    order = await _indicative_draft_order(db, season_id, regular_rounds, current_round)
    if not order:
        return
    position_of = {team_id: i for i, team_id in enumerate(order)}  # 0 = pick 1 (worst)
    num_teams = len(order)

    cursor = await db.execute(
        "SELECT pick_id, round_number, original_team_id FROM draft_picks WHERE draft_id = ?",
        (draft_id,)
    )
    picks = await cursor.fetchall()

    for pick_id, round_number, original_team_id in picks:
        slot = position_of.get(original_team_id)
        if slot is None or round_number is None:
            # A team with no ladder position at all (shouldn't happen for
            # a real team, but never crash a per-round background update
            # over it) or a pick whose round_number somehow isn't set yet.
            continue
        new_pick_number = (round_number - 1) * num_teams + slot + 1
        await db.execute(
            "UPDATE draft_picks SET pick_number = ? WHERE pick_id = ?",
            (new_pick_number, pick_id)
        )
    await db.commit()


async def _generate_finals_round(db, season_id, next_round_num, regular_rounds):
    """Auto-generates the finals fixture for `next_round_num`, if it's one
    of the 5 finals weeks - called from advance_to_next_round right after
    the round number itself advances. No-ops (returns False) for a regular
    round or anything past the Grand Final. Returns True if a fixture was
    generated."""
    finals_week = next_round_num - regular_rounds  # 1=Wildcard, 2=QF/EF, 3=Semis, 4=Prelims, 5=GF

    if finals_week == 1:
        ranked_ladder = await compute_and_store_ladder(db, season_id)
        ranked_team_ids = [row.team_id for row in ranked_ladder]
        matches = finals_bracket_module.generate_wildcard_round(ranked_team_ids)
    elif finals_week == 2:
        cursor = await db.execute(
            "SELECT team_id FROM ladder_positions WHERE season_id = ? ORDER BY position", (season_id,)
        )
        ranked_team_ids = [row[0] for row in await cursor.fetchall()]
        wc_results = {
            "WC1": await _finals_slot_result(db, season_id, "WC1"),
            "WC2": await _finals_slot_result(db, season_id, "WC2"),
        }
        matches = finals_bracket_module.generate_qf_ef_round(ranked_team_ids, wc_results)
    elif finals_week == 3:
        qf_ef_results = {
            slot: await _finals_slot_result(db, season_id, slot)
            for slot in ("QF1", "QF2", "EF1", "EF2")
        }
        matches = finals_bracket_module.generate_semis_round(qf_ef_results)
    elif finals_week == 4:
        qf_ef_results = {
            slot: await _finals_slot_result(db, season_id, slot)
            for slot in ("QF1", "QF2", "EF1", "EF2")
        }
        semis_results = {
            slot: await _finals_slot_result(db, season_id, slot)
            for slot in ("SF1", "SF2")
        }
        matches = finals_bracket_module.generate_prelims_round(qf_ef_results, semis_results)
    elif finals_week == 5:
        prelims_results = {
            slot: await _finals_slot_result(db, season_id, slot)
            for slot in ("PF1", "PF2")
        }
        matches = finals_bracket_module.generate_grand_final(prelims_results)
    else:
        return False

    for slot_code, home_team_id, away_team_id in matches:
        await _insert_finals_match(db, season_id, next_round_num, slot_code, home_team_id, away_team_id)
    await db.commit()
    return True


async def finalize_finals_ladder(db, season_id):
    """Called once the Grand Final's match has just been simulated (see
    MatchCommands._post_final_result in match_commands.py, the only place
    finals matches can finish since they're live-mode-only) - computes the
    season's true final 1-10 finish (GF winner=1st, GF loser=2nd, then each
    same-round-loser pair ordered by original regular-season rank - see
    finals_bracket.compute_final_finish_order) and rewrites ladder_positions
    with it. This is the ONE place ladder_positions changes after the
    regular season ends; from here on both the draft and free agency's
    tiebreak/compensation logic see this finals-adjusted order, not the
    frozen regular-season one. Safe to call even if finals_bracket is
    somehow incomplete for this season (e.g. a scratch/test setup) - simply
    does nothing in that case rather than raising."""
    cursor = await db.execute(
        "SELECT team_id FROM ladder_positions WHERE season_id = ? ORDER BY position", (season_id,)
    )
    ranked_team_ids = [row[0] for row in await cursor.fetchall()]

    all_slots = ("WC1", "WC2", "QF1", "QF2", "EF1", "EF2", "SF1", "SF2", "PF1", "PF2", "GF")
    cursor = await db.execute(
        "SELECT COUNT(*) FROM finals_bracket WHERE season_id = ? AND slot_code IN (%s)"
        % ",".join("?" * len(all_slots)),
        (season_id, *all_slots)
    )
    if (await cursor.fetchone())[0] != len(all_slots):
        return  # bracket incomplete for this season - nothing to finalize

    all_round_results = {
        slot: await _finals_slot_result(db, season_id, slot) for slot in all_slots
    }

    final_order = finals_bracket_module.compute_final_finish_order(ranked_team_ids, all_round_results)

    # final_order only covers the 10 teams that actually made the finals
    # (see compute_final_finish_order's own docstring) - a team that
    # finished 11th+ on the frozen regular-season ladder never got a
    # bracket slot at all, so it keeps its ORIGINAL regular-season
    # position rather than being deleted outright. Previously this wiped
    # every team's row for the season and re-inserted only the 10
    # finalists, which left non-finalists with no ladder_positions row at
    # all post-GF - post_season_summaries (and anything else reading this
    # table after finals) then showed "unavailable" for them instead of
    # their real final placing.
    await db.execute(
        "DELETE FROM ladder_positions WHERE season_id = ? AND team_id IN (%s)"
        % ",".join("?" * len(final_order)),
        (season_id, *final_order)
    )
    for position, team_id in enumerate(final_order, start=1):
        await db.execute(
            "INSERT INTO ladder_positions (season_id, team_id, position) VALUES (?, ?, ?)",
            (season_id, team_id, position)
        )
    await db.commit()


# Flat diagnosis-text -> (min_weeks, max_weeks) lookup, built once from
# match_sim.py's own INJURY_DIAGNOSES table. Diagnosis names are unique
# across every category in that table, so this is safe - a natural
# in-match injury's persisted injury_type IS one of these diagnosis
# strings (see MatchCommands._persist_match_result), letting the deferred
# recovery-weeks roll below look up the right range without needing its
# own separate column.
_INJURY_WEEKS_RANGE_BY_DIAGNOSIS = {
    diagnosis: weeks_range
    for diagnoses in INJURY_DIAGNOSES.values()
    for diagnosis, weeks_range in diagnoses
}


async def _roll_pending_injury_recoveries(db, next_round_num):
    """Rolls the real recovery length for every injury still marked TBC
    (recovery_rounds IS NULL) whose injury_round has now fully ended - i.e.
    injury_round < next_round_num, so this only reveals injuries from the
    round that JUST finished, not ones from a round still in progress (that
    shouldn't be possible given when this is called, but the guard costs
    nothing). Called from advance_to_next_round, before the recovered-
    players check, so a freshly-revealed injury's return_round is already
    in place by the time that check runs.

    Once injury_round has already ended, no +1 correction is needed the
    way /addinjury's immediate-roll does - next_round_num IS already one
    round past injury_round, so return_round = injury_round + 1 +
    recovery_weeks (using the round we're advancing INTO, not
    next_round_num itself, to stay exact regardless of how much later this
    ends up running) gives the same "miss injury_round + 1..N, return
    round N+1" shape as a fresh injury's +1 formula - see
    MatchCommands._persist_match_result's comment for the worked example.

    A diagnosis whose weeks-range can roll 0 (some mild diagnoses can - see
    match_sim.py's INJURY_DIAGNOSES) resolves return_round == next_round_num,
    i.e. already-recovered the instant it's revealed - deleted immediately
    here rather than left for the later recovered-players sweep in
    advance_to_next_round, since that sweep runs AFTER post_round_summaries
    and would otherwise leave the injury visible (as "ready to return")
    in that round's summary for one round it never should have appeared
    in at all."""
    import random

    cursor = await db.execute(
        "SELECT injury_id, injury_type, injury_round FROM injuries "
        "WHERE status = 'injured' AND recovery_rounds IS NULL AND injury_round < ?",
        (next_round_num,)
    )
    pending = await cursor.fetchall()

    for injury_id, injury_type, injury_round in pending:
        min_weeks, max_weeks = _INJURY_WEEKS_RANGE_BY_DIAGNOSIS.get(injury_type, (1, 2))
        recovery_weeks = random.randint(min_weeks, max_weeks)
        return_round = injury_round + recovery_weeks + 1
        if return_round <= next_round_num:
            await db.execute("DELETE FROM injuries WHERE injury_id = ?", (injury_id,))
            continue
        await db.execute(
            "UPDATE injuries SET recovery_rounds = ?, return_round = ? WHERE injury_id = ?",
            (recovery_weeks, return_round, injury_id)
        )

    if pending:
        await db.commit()


# Flat charge-text -> (min_games, max_games) lookup, built once from
# match_sim.py's own REPORT_REASONS table - same reasoning as
# _INJURY_WEEKS_RANGE_BY_DIAGNOSIS above (charge names are unique across
# every category, and a report's persisted suspension_reason IS one of
# these charge strings - see MatchCommands._persist_match_result).
_REPORT_GAMES_RANGE_BY_CHARGE = {
    charge: games_range
    for charges in REPORT_REASONS.values()
    for charge, games_range in charges
}


async def _roll_pending_report_suspensions(db, next_round_num):
    """Rolls the real suspension length for every report still marked TBC
    (games_missed IS NULL) whose suspension_round has now fully ended -
    mirrors _roll_pending_injury_recoveries, but simpler: unlike injuries'
    flat return_round arithmetic, a suspension's games_remaining just ticks
    down on rounds the player's team actually plays (see
    advance_to_next_round's later suspension-tick-down block).

    Returns the set of suspension_ids just revealed here, so the caller can
    exclude them from THIS SAME advance_to_next_round call's tick-down pass
    below - a natural report's suspension_round is the round the player was
    ALREADY REPORTED IN (a round they already played, same as an injury's
    injury_round), so that round must not also count as served, mirroring
    exactly why a natural injury's return_round needs its own +1 (see
    MatchCommands._persist_match_result's comment). This is deliberately
    NOT applied to every suspension whose suspension_round == current_round
    - only ones revealed by THIS call - so /addsuspension's existing,
    separately-tested "starting round counts as served" behavior (an admin
    entering a suspension mid-round, same as /editinjury resetting an
    injury's clock inclusive of the current round) is untouched."""
    import random

    cursor = await db.execute(
        "SELECT suspension_id, suspension_reason, suspension_round FROM suspensions "
        "WHERE status = 'suspended' AND games_missed IS NULL AND suspension_round < ?",
        (next_round_num,)
    )
    pending = await cursor.fetchall()

    for suspension_id, suspension_reason, suspension_round in pending:
        min_games, max_games = _REPORT_GAMES_RANGE_BY_CHARGE.get(suspension_reason, (1, 2))
        suspension_games = random.randint(min_games, max_games)
        if suspension_games == 0:
            # Cleared with no suspension - some reports/charges (a low
            # enough grading) can roll 0 games via their diagnosis range's
            # min (see match_sim.py's REPORT_REASONS). Deleted immediately
            # rather than left sitting at games_remaining=0 - matches how
            # an injury that rolls 0 weeks is caught by the very next
            # recovered-players sweep below in the same call (its
            # return_round is already <= next_round_num the moment it's
            # revealed), so neither ever actually appears on a list showing
            # "0 games"/"ready to return" for a report nobody needs to see
            # resolved that way.
            await db.execute("DELETE FROM suspensions WHERE suspension_id = ?", (suspension_id,))
            continue
        await db.execute(
            "UPDATE suspensions SET games_missed = ?, games_remaining = ? WHERE suspension_id = ?",
            (suspension_games, suspension_games, suspension_id)
        )

    if pending:
        await db.commit()

    return {suspension_id for suspension_id, _, _ in pending}


async def post_round_summaries(bot, db, season_id, season_number, current_round, regular_rounds, total_rounds, ranked_ladder, next_round_num):
    """Posts a personalized round-summary embed to each team's own channel
    once every match in the round has been simulated - their result, their
    new ladder position (from the just-computed ranked_ladder - see
    compute_and_store_ladder), and their current injury/suspension list
    (build_injury_suspension_list, injury_commands.py - deferred import to
    avoid a season_commands<->injury_commands circular import at module
    load time, same reasoning as the lineup_commands deferred imports
    elsewhere in this file). Called once per round, right after ladder
    computation, from SeasonCommands.advance_to_next_round.

    next_round_num (current_round + 1) is passed as build_injury_suspension_list's
    own current_round argument, NOT current_round itself - weeks-remaining
    there is "how many rounds from the round about to start," and by the
    time this posts, advance_to_next_round has already rolled this round's
    TBC injuries to their real recovery length (see
    _roll_pending_injury_recoveries), so a freshly-injured player's line
    shows real weeks-remaining instead of "TBC". new_this_round stays
    current_round (the round that just ended, matching an injury's own
    injury_round) so that player's line is still bolded as new."""
    from commands.injury_commands import build_injury_suspension_list

    position_by_team = {row.team_id: i for i, row in enumerate(ranked_ladder, start=1)}
    round_display = get_round_name(current_round, regular_rounds) if current_round > 0 else "Offseason"

    cursor = await db.execute(
        """SELECT m.match_id, m.home_team_id, m.away_team_id, m.home_score, m.away_score,
                  h.team_name, h.emoji_id, a.team_name, a.emoji_id
           FROM matches m
           JOIN teams h ON m.home_team_id = h.team_id
           JOIN teams a ON m.away_team_id = a.team_id
           WHERE m.season_id = ? AND m.round_number = ? AND m.simulated = 1""",
        (season_id, current_round)
    )
    matches = await cursor.fetchall()

    for match_id, home_team_id, away_team_id, home_score, away_score, home_name, home_emoji_id, away_name, away_emoji_id in matches:
        home_emoji = get_team_emoji_str(bot, home_emoji_id)
        away_emoji = get_team_emoji_str(bot, away_emoji_id)
        margin = abs(home_score - away_score)

        cursor = await db.execute(
            """SELECT pms.team_id, p.name, pms.goals, pms.behinds, pms.best_fairest_votes
               FROM player_match_stats pms
               JOIN players p ON pms.player_id = p.player_id
               WHERE pms.match_id = ?""",
            (match_id,)
        )
        stat_rows = await cursor.fetchall()

        for team_id, team_name, opponent_name, own_score, opp_score, was_home in (
            (home_team_id, home_name, away_name, home_score, away_score, True),
            (away_team_id, away_name, home_name, away_score, home_score, False),
        ):
            cursor = await db.execute("SELECT channel_id, emoji_id FROM teams WHERE team_id = ?", (team_id,))
            team_row = await cursor.fetchone()
            if not team_row or not team_row[0]:
                continue
            channel_id, emoji_id = team_row
            channel = bot.get_channel(int(channel_id))
            if not channel:
                continue

            if own_score > opp_score:
                color = discord.Color.green()
                margin_note = f"won by {margin}"
            elif opp_score > own_score:
                color = discord.Color.red()
                margin_note = f"lost by {margin}"
            else:
                color = discord.Color.greyple()
                margin_note = "match drawn"

            result_line = f"{home_emoji}{home_score} - {away_score}{away_emoji} ({margin_note})"

            # Ladder position only shown for a regular-season round - the
            # competitive ladder is frozen once finals start (see the
            # current_round <= regular_rounds check at this function's own
            # call site in advance_to_next_round), so there's no meaningful
            # position to show during finals; the line is omitted entirely
            # rather than showing a stale/misleading "unavailable".
            ladder_line = None
            if current_round <= regular_rounds:
                position = position_by_team.get(team_id)
                ladder_line = f"Ladder position: **{position}{_ordinal_suffix(position)}**" if position else "Ladder position: unavailable"

            # This team's own goalkickers + best-performed players, scoped
            # to just team_id (not both sides of the match) - "BEST" is the
            # top 5 best & fairest vote-getters (5-4-3-2-1, the only players
            # with a nonzero pms.best_fairest_votes - see
            # TeamMatchResult.best_and_fairest_votes in match_sim.py),
            # ordered by votes rather than shown as numbers.
            team_players = [row for row in stat_rows if row[0] == team_id]
            stat_lines_text = ""

            top_scorers = sorted(
                (row for row in team_players if row[2] > 0),
                key=lambda row: (-row[2], -row[3])
            )
            if top_scorers:
                stat_lines_text += "\n\n**GOALS:** " + ", ".join(f"{name} {goals}" for _, name, goals, _, _ in top_scorers)

            best_players = sorted(
                (row for row in team_players if row[4] > 0),
                key=lambda row: -row[4]
            )
            if best_players:
                stat_lines_text += "\n\n**BEST:** " + ", ".join(name for _, name, _, _, _ in best_players)

            injury_lines = await build_injury_suspension_list(
                bot, db, next_round_num, total_rounds, filter_team_id=team_id,
                season_id=season_id, regular_rounds=regular_rounds, new_this_round=current_round,
            )

            # ladder_line -> GOALS -> BEST -> injuries all separated by the
            # same blank-line gap (matching build_injury_suspension_list's
            # own "" separator between its Injuries/Suspensions sections) -
            # stat_lines_text already leads with "\n\n" per section above.
            description = f"{result_line}\n{ladder_line}" if ladder_line else result_line
            if stat_lines_text:
                description += stat_lines_text
            if injury_lines:
                description += "\n\n" + "\n".join(injury_lines)

            team_emoji_str = get_team_emoji_str(bot, emoji_id)
            embed = discord.Embed(
                title=f"{team_emoji_str}{round_display} Summary",
                description=description,
                color=color,
            )

            view = _RoundSummaryView(bot, match_id, team_id)
            bot.add_view(view)
            await channel.send(embed=embed, view=view)


class _RoundSummaryReturnView(discord.ui.View):
    """Minimal stand-in "parent view" for _MatchStatsView's Main menu
    button - _MatchStatsView only ever calls parent_view.create_embed(),
    it doesn't need a real MatchCentreView/_TeamMatchesView, just
    something exposing that one method (see _MatchStatsView's own
    docstring in match_commands.py). Returns to a copy of the round
    summary embed this was opened from (the ephemeral box-score message
    it's attached to, not the original public channel post)."""
    def __init__(self, summary_embed):
        super().__init__(timeout=1800)
        self.summary_embed = summary_embed

    def create_embed(self):
        return self.summary_embed


class _RoundSummaryView(discord.ui.View):
    """Two buttons on each team's round-summary post: jump straight to
    that match's box score (/matchcentre's per-match stats view, opened
    directly for the known match_id - bypassing the round/team picker
    entirely), and open that team's own lineup menu for the next round
    (/teamlineup's own menu, built directly rather than going through the
    command - this view already knows exactly which team it's for).

    Persistent (timeout=None + per-match/team custom_ids) since the round
    summary is a permanent public post people may revisit well after a
    1800s timeout would have expired - e.g. to set their lineup right
    before next round locks. Requires SeasonCommands.cog_load to
    re-register one instance per current-round match on every bot
    restart (see register_persistent_round_summary_views), the same
    pattern trade_commands.py uses for its own persistent views."""
    def __init__(self, bot, match_id, team_id):
        super().__init__(timeout=None)
        self.bot = bot
        self.match_id = match_id
        self.team_id = team_id
        # custom_ids must be unique per (match_id, team_id) - discord.py
        # dispatches a persistent-view interaction to whichever registered
        # view instance owns the matching custom_id, so a static id shared
        # across every round-summary post would route every click to
        # whichever instance happened to be registered last.
        self.view_player_stats.custom_id = f"round_summary_stats_{match_id}_{team_id}"
        self.set_lineup.custom_id = f"round_summary_lineup_{match_id}_{team_id}"

    @discord.ui.button(label="View Player Stats", style=discord.ButtonStyle.primary)
    async def view_player_stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        from commands.match_commands import _MatchStatsView

        # Ephemeral, not an edit of the round summary itself - that's a
        # public, permanent post to the whole channel; box-score browsing
        # (stat switching/paging via _MatchStatsView's own buttons) should
        # stay private to whoever clicked, not repeatedly overwrite the
        # shared summary message for everyone in the channel.
        await interaction.response.defer(ephemeral=True)
        match_cog = self.bot.get_cog('MatchCommands')
        async with aiosqlite.connect(DB_PATH) as db:
            data = await match_cog._fetch_box_score_data(db, self.match_id)
        if not data:
            await interaction.followup.send("❌ Stats for this match aren't available.", ephemeral=True)
            return

        # interaction.message is the round-summary post itself - read live
        # rather than a captured embed, since this view no longer stores
        # one (a persistent view rebuilt at startup has no send-time state).
        summary_embed = interaction.message.embeds[0]
        return_view = _RoundSummaryReturnView(summary_embed)
        stats_view = _MatchStatsView(return_view, data)
        await interaction.followup.send(embed=stats_view.create_embed(), view=stats_view, ephemeral=True)

    @discord.ui.button(label="Set Lineup for Next Round", style=discord.ButtonStyle.secondary)
    async def set_lineup(self, interaction: discord.Interaction, button: discord.ui.Button):
        from commands.lineup_commands import build_team_lineup_menu

        await interaction.response.defer(ephemeral=True)
        async with aiosqlite.connect(DB_PATH) as db:
            lineup_view, lineup_embed = await build_team_lineup_menu(db, self.bot, self.team_id)
        await interaction.followup.send(embed=lineup_embed, view=lineup_view, ephemeral=True)


def _ordinal_suffix(n):
    if 11 <= (n % 100) <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


async def post_season_summaries(bot, db, season_id, season_number):
    """Posts a personalized end-of-season summary embed to each team's own
    channel - final ladder position, leading goalkicker, and best &
    fairest winner + top 10 (best & fairest is inherently a per-team award
    - see match_sim.py's TeamMatchResult.best_and_fairest_votes - so both
    the goalkicker and B&F sections here are scoped to that one team's own
    players/matches, not league-wide). Called once from /endseason, after
    the season is marked offseason.

    Reads ladder_positions AS-IS rather than recomputing it - by the time
    /endseason runs, that table already holds the true finals-adjusted
    final order (finalize_finals_ladder ran when the Grand Final was
    simulated - see that function's own docstring), so this is a plain
    read, not a fresh ladder computation. A team with no row there (e.g.
    /endseason run before any round was ever played) simply shows
    "unavailable", same fallback post_round_summaries uses for a missing
    position.

    Goals/best & fairest votes are summed straight from player_match_stats,
    joined through matches on season_id (matches.season_id, not a fresh
    join through the seasons table - see the Excel export's equivalent
    query in admin_commands.py for the same JOIN path) - filtered by
    pms.team_id (the team a player played FOR in each match), not
    players.team_id, so a since-traded player's votes/goals still count
    toward the team they actually earned them for, consistent with how
    the Excel export already treats this column."""
    cursor = await db.execute(
        "SELECT team_id, team_name, channel_id, emoji_id FROM teams WHERE team_name != 'Draft Pool'"
    )
    teams = await cursor.fetchall()

    cursor = await db.execute(
        "SELECT team_id, position FROM ladder_positions WHERE season_id = ?", (season_id,)
    )
    position_by_team = {team_id: position for team_id, position in await cursor.fetchall()}

    for team_id, team_name, channel_id, emoji_id in teams:
        if not channel_id:
            continue
        channel = bot.get_channel(int(channel_id))
        if not channel:
            continue

        position = position_by_team.get(team_id)
        ladder_line = f"Final ladder position: **{position}{_ordinal_suffix(position)}**" if position else "Final ladder position: unavailable"

        cursor = await db.execute(
            """SELECT p.name, SUM(pms.goals) as total_goals
               FROM player_match_stats pms
               JOIN matches m ON pms.match_id = m.match_id
               JOIN players p ON pms.player_id = p.player_id
               WHERE m.season_id = ? AND pms.team_id = ?
               GROUP BY pms.player_id
               ORDER BY total_goals DESC
               LIMIT 1""",
            (season_id, team_id)
        )
        top_goalkicker = await cursor.fetchone()
        if top_goalkicker and top_goalkicker[1]:
            goalkicker_line = f"Leading goalkicker: **{top_goalkicker[0]}** ({top_goalkicker[1]} goals)"
        else:
            goalkicker_line = "Leading goalkicker: unavailable"

        cursor = await db.execute(
            """SELECT p.name, SUM(pms.best_fairest_votes) as total_votes
               FROM player_match_stats pms
               JOIN matches m ON pms.match_id = m.match_id
               JOIN players p ON pms.player_id = p.player_id
               WHERE m.season_id = ? AND pms.team_id = ?
               GROUP BY pms.player_id
               HAVING total_votes > 0
               ORDER BY total_votes DESC
               LIMIT 10""",
            (season_id, team_id)
        )
        bf_top_10 = await cursor.fetchall()

        if bf_top_10:
            winner_name, winner_votes = bf_top_10[0]
            bf_winner_line = f"Best & Fairest winner: **{winner_name}**"
        else:
            bf_winner_line = "Best & Fairest: unavailable"

        description = f"{ladder_line}\n{goalkicker_line}\n{bf_winner_line}"

        if bf_top_10:
            # Blank line between the header and the first ranked line, not
            # just a single "\n" - Discord's mobile renderer can otherwise
            # crowd the first item right up against a bolded header line
            # with no visible line break between them.
            description += "\n\n**Best & Fairest - Top 10:**\n"
            for rank, (name, votes) in enumerate(bf_top_10, 1):
                description += f"\n{rank}. {name} — {votes} votes"

        emoji_str = get_team_emoji_str(bot, emoji_id)
        embed = discord.Embed(
            title=f"{emoji_str}Season {season_number} Summary",
            description=description,
            color=discord.Color.gold(),
        )
        await channel.send(embed=embed)


async def build_ladder_image_files(bot, db, ranked_ladder):
    """Fetches each team's Discord emoji as a small icon (ladder_image.py's
    fetch_team_icons) and each team's configured primary/secondary colors
    (/updateteam's primary_color/secondary_color params), splits the ladder
    into top/bottom halves (split_ladder_for_images), then renders EACH
    half as its own PNG (render_ladder_image) - returns a list of 1 or 2
    ready-to-send discord.File objects, in ladder order (top half first).
    Splitting lets each image use a taller row height / bigger team emoji
    without the combined ladder's total height blowing past Discord's
    display height cap - replaced the earlier single-image attempt for
    exactly that reason. Also replaced the even-earlier text/box-drawn
    table attempts, which couldn't align well or show real team emoji
    icons at all (Discord code blocks can't render custom emojis - they
    show as raw <:name:id> text). Shared by post_ladder (the auto-post on
    round advance) and the /ladder command, so both always look identical.
    Neither image has a title bar - see post_ladder for the round-advance
    auto-post's separate "SEASON x ROUND y" text message. Only the FIRST
    (top-half) image gets the Pos/Team/W/L/... column-header bar - the
    second image posts directly under it with no gap (see post_ladder), so
    a second header there would just be redundant."""
    cursor = await db.execute("SELECT team_id, emoji_id, color, color_secondary FROM teams WHERE team_name != 'Draft Pool'")
    team_rows = await cursor.fetchall()
    emoji_by_team = {team_id: emoji_id for team_id, emoji_id, _, _ in team_rows}
    primary_color_by_team = {team_id: color for team_id, _, color, _ in team_rows if color}
    secondary_color_by_team = {team_id: color_secondary for team_id, _, _, color_secondary in team_rows if color_secondary}

    team_icons = await fetch_team_icons(bot, emoji_by_team)

    top_half, bottom_half = split_ladder_for_images(ranked_ladder)
    files = []
    for half, start_position, show_header in ((top_half, 1, True), (bottom_half, len(top_half) + 1, False)):
        if not half:
            continue
        buffer = render_ladder_image(
            half, team_icons, primary_color_by_team, secondary_color_by_team,
            start_position=start_position, show_header=show_header,
        )
        files.append(discord.File(buffer, filename="ladder.png"))
    return files


async def post_ladder(bot, db, season_id, season_number, current_round, regular_rounds, ranked_ladder):
    """Posts the full ladder (from the just-computed ranked_ladder - see
    compute_and_store_ladder) to the configured ladder channel, if one is
    set - a no-op otherwise. Called once per round, right after ladder
    computation, from SeasonCommands.advance_to_next_round. Precedes the
    ladder images with a plain "SEASON x ROUND y" text message (uppercased
    round name, so a finals round like "Grand Final" still reads
    correctly) - this label lives here, NOT in either image, and only
    appears on this auto-post, not on /ladder's on-demand response. The
    top-half and bottom-half images (build_ladder_image_files) are posted
    as two SEPARATE messages, one after the other, so they stack vertically
    rather than side by side in a single message."""
    cursor = await db.execute(
        "SELECT setting_value FROM settings WHERE setting_key = 'ladder_channel_id'"
    )
    channel_row = await cursor.fetchone()
    if not channel_row or not channel_row[0]:
        return
    channel = bot.get_channel(int(channel_row[0]))
    if not channel:
        return

    round_display = get_round_name(current_round, regular_rounds) if current_round > 0 else "Offseason"
    files = await build_ladder_image_files(bot, db, ranked_ladder)
    await channel.send(f"**SEASON {season_number} — {round_display.upper()}**")
    # Each image as its own send() call, NOT combined into one files=[...]
    # message - Discord lays multiple attachments on a single message out
    # side by side (a gallery grid), not stacked vertically. Separate
    # messages is the only way to get them to actually stack top-to-bottom.
    for file in files:
        await channel.send(file=file)


async def _ensure_season_draft_and_picks(db, season_num, teams, default_rounds=4):
    """Ensures ONE season number has its linked National Draft, and that
    draft has its picks generated - the per-season backfill body shared by
    ensure_future_seasons_exist's loop and /endseason's own inline "create
    next season" step (see that command - it inserts the next season's
    `seasons` row itself, with its own admin-chosen regular_rounds, so it
    can't just delegate the whole season+draft+picks bundle to
    ensure_future_seasons_exist; it only needed this half).

    Checks the draft row and its picks INDEPENDENTLY, rather than assuming
    one implies the other - a draft can exist with zero picks if it was
    created by a different code path that never got around to generating
    them (this is exactly the bug that motivated splitting this out: a
    season's `seasons` row being created elsewhere left its draft entirely
    unmade, and separately a draft could exist without picks). Checks
    picks by COUNT on draft_id, not by whether the draft row itself is
    new, so a pre-existing but empty draft still gets backfilled.

    teams is the pre-fetched (team_id, team_name) list (ORDER BY team_name)
    - fetched once by the caller rather than re-queried per season in a
    loop.

    Returns True if anything was newly created (the draft row and/or its
    picks), False if everything was already in place."""
    touched = False

    # Draft is named after the PREVIOUS season (e.g., Season 10 uses "Season 9 National Draft")
    draft_name = f"Season {season_num - 1} National Draft"

    cursor = await db.execute(
        "SELECT draft_id FROM drafts WHERE draft_name = ?",
        (draft_name,)
    )
    draft_row = await cursor.fetchone()
    if draft_row:
        draft_id = draft_row[0]
    else:
        cursor = await db.execute(
            """INSERT INTO drafts (draft_name, season_number, status, rounds)
               VALUES (?, ?, 'future', ?)""",
            (draft_name, season_num, default_rounds)
        )
        draft_id = cursor.lastrowid
        touched = True

    cursor = await db.execute(
        "SELECT COUNT(*) FROM draft_picks WHERE draft_id = ?", (draft_id,)
    )
    if (await cursor.fetchone())[0] == 0:
        for team_id, team_name in teams:
            for round_num in range(1, default_rounds + 1):
                pick_origin = f"{team_name} R{round_num}"
                await db.execute(
                    """INSERT INTO draft_picks (draft_id, draft_name, season_number, round_number,
                                                pick_number, pick_origin, original_team_id, current_team_id)
                       VALUES (?, ?, ?, ?, NULL, ?, ?, ?)""",
                    (draft_id, draft_name, season_num, round_num, pick_origin, team_id, team_id)
                )
        touched = True

    return touched


async def ensure_future_seasons_exist(db, current_season_number, num_future=2, default_rounds=4):
    """
    Ensure the next N future seasons exist, each with a linked draft that
    actually has its picks generated.

    The season row and its draft+picks (see _ensure_season_draft_and_picks)
    are checked INDEPENDENTLY for each future season - a season row and its
    draft can come from different code paths and at different times (e.g.
    /endseason's own inline "create next season" block only ever inserted
    the seasons row, with no draft of its own, before this was split out -
    see _ensure_season_draft_and_picks's docstring), so a season existing
    already does NOT imply its draft/picks exist.

    Args:
        db: Database connection
        current_season_number: The current/latest season number
        num_future: How many future seasons to ensure exist (default 2)
        default_rounds: Number of draft rounds (default 4)

    Returns:
        List of season numbers that had anything newly created (a season
        row, a draft row, or backfilled picks) - not just brand-new seasons.
    """
    created_seasons = []

    # Get all teams for pick generation
    cursor = await db.execute("SELECT team_id, team_name FROM teams ORDER BY team_name")
    teams = await cursor.fetchall()

    if not teams:
        return created_seasons

    for offset in range(1, num_future + 1):
        future_season_num = current_season_number + offset
        season_touched = False

        cursor = await db.execute(
            "SELECT season_id FROM seasons WHERE season_number = ?",
            (future_season_num,)
        )
        if not await cursor.fetchone():
            await db.execute(
                """INSERT INTO seasons (season_number, current_round, regular_rounds, total_rounds, round_name, status)
                   VALUES (?, 0, 24, 29, 'Future', 'future')""",
                (future_season_num,)
            )
            season_touched = True

        if await _ensure_season_draft_and_picks(db, future_season_num, teams, default_rounds):
            season_touched = True

        if season_touched:
            created_seasons.append(future_season_num)

    await db.commit()
    return created_seasons

class SeasonCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        """Called when the cog is loaded - re-register persistent round
        summary views (same pattern as TradeCommands.register_persistent_views
        in trade_commands.py)."""
        await self.register_persistent_round_summary_views()

    async def register_persistent_round_summary_views(self):
        """Re-registers a _RoundSummaryView for every match in the active
        season's CURRENT round on bot startup - not every round summary
        ever posted, since older rounds' "Set Lineup for Next Round"
        button is no longer actionable (that round has already locked)
        and re-registering every historical match would grow unbounded
        for no benefit."""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "SELECT season_id, current_round FROM seasons WHERE status = 'active' LIMIT 1"
                )
                season_row = await cursor.fetchone()
                if not season_row:
                    return
                season_id, current_round = season_row

                cursor = await db.execute(
                    "SELECT match_id, home_team_id, away_team_id FROM matches WHERE season_id = ? AND round_number = ? AND simulated = 1",
                    (season_id, current_round)
                )
                matches = await cursor.fetchall()

                count = 0
                for match_id, home_team_id, away_team_id in matches:
                    for team_id in (home_team_id, away_team_id):
                        self.bot.add_view(_RoundSummaryView(self.bot, match_id, team_id))
                        count += 1

                print(f"Re-registered {count} round summary views")
        except Exception as e:
            print(f"Error registering round summary persistent views: {e}")
            import traceback
            traceback.print_exc()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Check if user has admin permissions for admin commands"""
        # Check if command is a public command
        if interaction.command.name in ['seasonstatus']:
            return True

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

    @app_commands.command(name="migratedb", description="[ADMIN] Migrate database tables (run once after updates)")
    async def migrate_db(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as db:
            try:
                # Migrate seasons table - preserve existing data
                await db.execute('''
                    CREATE TABLE IF NOT EXISTS seasons (
                        season_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        season_number INTEGER NOT NULL UNIQUE,
                        current_round INTEGER DEFAULT 0,
                        regular_rounds INTEGER DEFAULT 24,
                        total_rounds INTEGER DEFAULT 29,
                        round_name TEXT DEFAULT 'Offseason',
                        status TEXT DEFAULT 'offseason'
                    )
                ''')

                # Create injuries table. recovery_rounds/return_round are
                # nullable - see bot.py's init_db comment on this table for
                # why (NULL = recovery time not yet determined for a
                # natural in-match injury, revealed at advance_to_next_round).
                await db.execute('''
                    CREATE TABLE IF NOT EXISTS injuries (
                        injury_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        player_id INTEGER NOT NULL,
                        injury_type TEXT NOT NULL,
                        injury_round INTEGER NOT NULL,
                        recovery_rounds INTEGER,
                        return_round INTEGER,
                        status TEXT DEFAULT 'injured',
                        FOREIGN KEY (player_id) REFERENCES players(player_id)
                    )
                ''')

                # Migrate an existing DB's injuries table if recovery_rounds/
                # return_round are still NOT NULL (predates the TBC-until-
                # advance_to_next_round change) - SQLite can't ALTER COLUMN
                # to relax a NOT NULL constraint, so rebuild the table, same
                # create-new/copy/drop/rename pattern used elsewhere in this
                # migration (see the free_agency_* re-key above).
                cursor = await db.execute("PRAGMA table_info(injuries)")
                injury_columns = await cursor.fetchall()
                recovery_rounds_col = next((c for c in injury_columns if c[1] == 'recovery_rounds'), None)
                if recovery_rounds_col is not None and recovery_rounds_col[3]:  # col[3] = notnull flag
                    await db.execute("DROP TABLE IF EXISTS injuries_new")
                    await db.execute('''
                        CREATE TABLE injuries_new (
                            injury_id INTEGER PRIMARY KEY AUTOINCREMENT,
                            player_id INTEGER NOT NULL,
                            injury_type TEXT NOT NULL,
                            injury_round INTEGER NOT NULL,
                            recovery_rounds INTEGER,
                            return_round INTEGER,
                            status TEXT DEFAULT 'injured',
                            FOREIGN KEY (player_id) REFERENCES players(player_id)
                        )
                    ''')
                    await db.execute('''
                        INSERT INTO injuries_new
                            (injury_id, player_id, injury_type, injury_round, recovery_rounds, return_round, status)
                        SELECT injury_id, player_id, injury_type, injury_round, recovery_rounds, return_round, status
                        FROM injuries
                    ''')
                    await db.execute("DROP TABLE injuries")
                    await db.execute("ALTER TABLE injuries_new RENAME TO injuries")

                # Create suspensions table. games_missed/games_remaining are
                # nullable - NULL means "suspension length not yet
                # determined" (a natural in-match report's actual sanction
                # isn't rolled until the round it happened in is fully over -
                # see advance_to_next_round's _roll_pending_report_suspensions),
                # same TBC reasoning as injuries.recovery_rounds/return_round.
                await db.execute('''
                    CREATE TABLE IF NOT EXISTS suspensions (
                        suspension_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        player_id INTEGER NOT NULL,
                        suspension_reason TEXT NOT NULL,
                        suspension_round INTEGER NOT NULL,
                        games_missed INTEGER,
                        games_remaining INTEGER,
                        status TEXT DEFAULT 'suspended',
                        FOREIGN KEY (player_id) REFERENCES players(player_id)
                    )
                ''')

                # Add games_remaining column to suspensions if migrating from
                # an older DB - the real "still serving" counter (only ticks
                # down on rounds the player's team actually plays). Backfill
                # from the existing games_missed so already-active
                # suspensions aren't silently cleared.
                cursor = await db.execute("PRAGMA table_info(suspensions)")
                columns = await cursor.fetchall()
                column_names = [col[1] for col in columns]

                if 'games_remaining' not in column_names:
                    await db.execute("ALTER TABLE suspensions ADD COLUMN games_remaining INTEGER")
                    await db.execute(
                        """UPDATE suspensions SET games_remaining = MAX(games_missed, 0)
                           WHERE games_remaining IS NULL"""
                    )

                # Drop the now-unused return_round column from an older DB -
                # it was a round-number "expected back" estimate that assumed
                # no byes in between, which games_remaining made both
                # inaccurate and redundant.
                if 'return_round' in column_names:
                    await db.execute("ALTER TABLE suspensions DROP COLUMN return_round")

                # Migrate an existing DB's suspensions table if games_missed/
                # games_remaining are still NOT NULL (predates report-driven
                # TBC suspensions) - SQLite can't ALTER COLUMN to relax a
                # NOT NULL constraint, so rebuild the table, same
                # create-new/copy/drop/rename pattern used for injuries above.
                cursor = await db.execute("PRAGMA table_info(suspensions)")
                suspension_columns = await cursor.fetchall()
                games_missed_col = next((c for c in suspension_columns if c[1] == 'games_missed'), None)
                if games_missed_col is not None and games_missed_col[3]:  # col[3] = notnull flag
                    await db.execute("DROP TABLE IF EXISTS suspensions_new")
                    await db.execute('''
                        CREATE TABLE suspensions_new (
                            suspension_id INTEGER PRIMARY KEY AUTOINCREMENT,
                            player_id INTEGER NOT NULL,
                            suspension_reason TEXT NOT NULL,
                            suspension_round INTEGER NOT NULL,
                            games_missed INTEGER,
                            games_remaining INTEGER,
                            status TEXT DEFAULT 'suspended',
                            FOREIGN KEY (player_id) REFERENCES players(player_id)
                        )
                    ''')
                    await db.execute('''
                        INSERT INTO suspensions_new
                            (suspension_id, player_id, suspension_reason, suspension_round, games_missed, games_remaining, status)
                        SELECT suspension_id, player_id, suspension_reason, suspension_round, games_missed, games_remaining, status
                        FROM suspensions
                    ''')
                    await db.execute("DROP TABLE suspensions")
                    await db.execute("ALTER TABLE suspensions_new RENAME TO suspensions")

                # Create settings table for global bot settings
                await db.execute('''
                    CREATE TABLE IF NOT EXISTS settings (
                        setting_key TEXT PRIMARY KEY,
                        setting_value TEXT
                    )
                ''')

                # Create Starting Lineups table (for saved lineup presets)
                await db.execute('''
                    CREATE TABLE IF NOT EXISTS starting_lineups (
                        team_id INTEGER PRIMARY KEY,
                        lineup_data TEXT NOT NULL,
                        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (team_id) REFERENCES teams(team_id)
                    )
                ''')

                # Create Ladder Positions table (for draft order)
                await db.execute('''
                    CREATE TABLE IF NOT EXISTS ladder_positions (
                        ladder_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        season_id INTEGER NOT NULL,
                        team_id INTEGER NOT NULL,
                        position INTEGER NOT NULL,
                        FOREIGN KEY (season_id) REFERENCES seasons(season_id),
                        FOREIGN KEY (team_id) REFERENCES teams(team_id),
                        UNIQUE(season_id, team_id),
                        UNIQUE(season_id, position)
                    )
                ''')

                # Create Finals Bracket table (tracks each finals week's slots)
                await db.execute('''
                    CREATE TABLE IF NOT EXISTS finals_bracket (
                        bracket_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        season_id INTEGER NOT NULL,
                        round_number INTEGER NOT NULL,
                        slot_code TEXT NOT NULL,
                        home_team_id INTEGER,
                        away_team_id INTEGER,
                        match_id INTEGER,
                        FOREIGN KEY (season_id) REFERENCES seasons(season_id),
                        FOREIGN KEY (match_id) REFERENCES matches(match_id),
                        UNIQUE(season_id, slot_code)
                    )
                ''')

                # Create trades table (preserve existing data)
                await db.execute('''
                    CREATE TABLE IF NOT EXISTS trades (
                        trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        initiating_team_id INTEGER NOT NULL,
                        receiving_team_id INTEGER NOT NULL,
                        initiating_players TEXT,
                        receiving_players TEXT,
                        initiating_picks TEXT,
                        receiving_picks TEXT,
                        status TEXT DEFAULT 'pending',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        responded_at TIMESTAMP,
                        approved_at TIMESTAMP,
                        created_by_user_id TEXT,
                        responded_by_user_id TEXT,
                        approved_by_user_id TEXT,
                        original_trade_id INTEGER,
                        FOREIGN KEY (initiating_team_id) REFERENCES teams(team_id),
                        FOREIGN KEY (receiving_team_id) REFERENCES teams(team_id),
                        FOREIGN KEY (original_trade_id) REFERENCES trades(trade_id)
                    )
                ''')

                # Create drafts table
                await db.execute('''
                    CREATE TABLE IF NOT EXISTS drafts (
                        draft_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        draft_name TEXT UNIQUE NOT NULL,
                        season_number INTEGER NOT NULL,
                        status TEXT DEFAULT 'future',
                        rounds INTEGER DEFAULT 4,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        ladder_set_at TIMESTAMP NULL,
                        FOREIGN KEY (season_number) REFERENCES seasons(season_number)
                    )
                ''')

                # Create draft_picks table (preserve existing data)
                await db.execute('''
                    CREATE TABLE IF NOT EXISTS draft_picks (
                        pick_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        draft_id INTEGER NOT NULL,
                        draft_name TEXT NOT NULL,
                        season_number INTEGER NOT NULL,
                        round_number INTEGER,
                        pick_number INTEGER,
                        pick_origin TEXT,
                        original_team_id INTEGER,
                        current_team_id INTEGER,
                        player_selected_id INTEGER,
                        FOREIGN KEY (draft_id) REFERENCES drafts(draft_id),
                        FOREIGN KEY (season_number) REFERENCES seasons(season_number),
                        FOREIGN KEY (original_team_id) REFERENCES teams(team_id),
                        FOREIGN KEY (current_team_id) REFERENCES teams(team_id),
                        FOREIGN KEY (player_selected_id) REFERENCES players(player_id)
                    )
                ''')

                # Remove lineup_channel_id from teams if it exists (moved to settings)
                cursor = await db.execute("PRAGMA table_info(teams)")
                columns = await cursor.fetchall()
                column_names = [col[1] for col in columns]

                if 'lineup_channel_id' in column_names:
                    # Can't drop column in SQLite, so just notify user it's deprecated
                    pass

                # Add contract_expiry column to players if it doesn't exist
                cursor = await db.execute("PRAGMA table_info(players)")
                columns = await cursor.fetchall()
                column_names = [col[1] for col in columns]

                if 'contract_expiry' not in column_names:
                    await db.execute('ALTER TABLE players ADD COLUMN contract_expiry INTEGER')

                    # Set default contracts for existing players (current_season + 2)
                    cursor = await db.execute(
                        "SELECT season_number FROM seasons ORDER BY season_number DESC LIMIT 1"
                    )
                    season_result = await cursor.fetchone()
                    if season_result:
                        current_season = season_result[0]
                        await db.execute(
                            "UPDATE players SET contract_expiry = ? WHERE contract_expiry IS NULL",
                            (current_season + 2,)
                        )

                # Add birth_year column to players if it doesn't exist
                cursor = await db.execute("PRAGMA table_info(players)")
                columns = await cursor.fetchall()
                column_names = [col[1] for col in columns]

                if 'birth_year' not in column_names:
                    await db.execute('ALTER TABLE players ADD COLUMN birth_year INTEGER')

                    # Calculate birth_year from existing age column if it exists
                    if 'age' in column_names:
                        # Get current season
                        cursor = await db.execute(
                            "SELECT season_number FROM seasons ORDER BY season_number DESC LIMIT 1"
                        )
                        season_result = await cursor.fetchone()
                        if season_result:
                            current_season = season_result[0]

                            # Get season_1_year setting (default to current_season if not set)
                            cursor = await db.execute(
                                "SELECT setting_value FROM settings WHERE setting_key = 'season_1_year'"
                            )
                            setting_result = await cursor.fetchone()
                            if setting_result:
                                season_1_year = int(setting_result[0])
                                current_year = season_1_year + (current_season - 1)
                            else:
                                # Default: assume current season year equals season number for migration
                                current_year = current_season

                            # Calculate birth_year = current_year - age for all players
                            await db.execute(
                                "UPDATE players SET birth_year = ? - age WHERE age IS NOT NULL",
                                (current_year,)
                            )

                # Add father_son_club_id column to players if it doesn't exist
                cursor = await db.execute("PRAGMA table_info(players)")
                columns = await cursor.fetchall()
                column_names = [col[1] for col in columns]

                if 'father_son_club_id' not in column_names:
                    await db.execute('ALTER TABLE players ADD COLUMN father_son_club_id INTEGER REFERENCES teams(team_id)')

                # Add season_1_year setting if it doesn't exist
                cursor = await db.execute(
                    "SELECT setting_value FROM settings WHERE setting_key = 'season_1_year'"
                )
                if not await cursor.fetchone():
                    # Get current season number
                    cursor = await db.execute(
                        "SELECT season_number FROM seasons ORDER BY season_number DESC LIMIT 1"
                    )
                    season_result = await cursor.fetchone()
                    if season_result:
                        current_season = season_result[0]
                        # Default to 2016 for Season 1 (adjust as needed)
                        await db.execute(
                            "INSERT INTO settings (setting_key, setting_value) VALUES ('season_1_year', '2016')"
                        )

                # Add rookie_contract_years to drafts table if it doesn't exist
                cursor = await db.execute("PRAGMA table_info(drafts)")
                columns = await cursor.fetchall()
                column_names = [col[1] for col in columns]

                if 'rookie_contract_years' not in column_names:
                    await db.execute('ALTER TABLE drafts ADD COLUMN rookie_contract_years INTEGER DEFAULT 3')

                # Create Contract Config table
                await db.execute('''
                    CREATE TABLE IF NOT EXISTS contract_config (
                        config_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        min_age INTEGER NOT NULL,
                        max_age INTEGER,
                        contract_years INTEGER NOT NULL,
                        UNIQUE(min_age, max_age)
                    )
                ''')

                # Insert default contract config
                await db.execute('''
                    INSERT OR IGNORE INTO contract_config (min_age, max_age, contract_years) VALUES
                    (0, 20, 3),
                    (21, 23, 5),
                    (24, 26, 4),
                    (27, 30, 3),
                    (31, 99, 2)
                ''')

                # Create Compensation Chart table
                await db.execute('''
                    CREATE TABLE IF NOT EXISTS compensation_chart (
                        chart_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        min_age INTEGER NOT NULL,
                        max_age INTEGER,
                        min_ovr INTEGER NOT NULL,
                        max_ovr INTEGER,
                        compensation_band INTEGER NOT NULL,
                        UNIQUE(min_age, max_age, min_ovr, max_ovr)
                    )
                ''')

                # Create Free Agency Bids table
                # (The free agency period itself lives in the settings table:
                #  fa_period_status / fa_period_season / fa_period_auction_points)
                await db.execute('''
                    CREATE TABLE IF NOT EXISTS free_agency_bids (
                        bid_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        season_number INTEGER NOT NULL,
                        team_id INTEGER NOT NULL,
                        player_id INTEGER NOT NULL,
                        bid_amount INTEGER NOT NULL,
                        status TEXT DEFAULT 'active',
                        placed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (season_number) REFERENCES seasons(season_number),
                        FOREIGN KEY (team_id) REFERENCES teams(team_id),
                        FOREIGN KEY (player_id) REFERENCES players(player_id),
                        UNIQUE(season_number, team_id, player_id)
                    )
                ''')

                # Create Free Agency Results table
                await db.execute('''
                    CREATE TABLE IF NOT EXISTS free_agency_results (
                        result_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        season_number INTEGER NOT NULL,
                        player_id INTEGER NOT NULL,
                        original_team_id INTEGER NOT NULL,
                        winning_team_id INTEGER,
                        winning_bid INTEGER,
                        matched BOOLEAN DEFAULT 0,
                        confirmed_at TIMESTAMP,
                        compensation_band INTEGER,
                        compensation_pick_id INTEGER,
                        FOREIGN KEY (season_number) REFERENCES seasons(season_number),
                        FOREIGN KEY (player_id) REFERENCES players(player_id),
                        FOREIGN KEY (original_team_id) REFERENCES teams(team_id),
                        FOREIGN KEY (winning_team_id) REFERENCES teams(team_id),
                        FOREIGN KEY (compensation_pick_id) REFERENCES draft_picks(pick_id),
                        UNIQUE(season_number, player_id)
                    )
                ''')

                # Create Free Re-Signs table
                await db.execute('''
                    CREATE TABLE IF NOT EXISTS free_agency_resigns (
                        resign_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        season_number INTEGER NOT NULL,
                        team_id INTEGER NOT NULL,
                        player_id INTEGER NOT NULL,
                        confirmed BOOLEAN DEFAULT 0,
                        confirmed_at TIMESTAMP,
                        FOREIGN KEY (season_number) REFERENCES seasons(season_number),
                        FOREIGN KEY (team_id) REFERENCES teams(team_id),
                        FOREIGN KEY (player_id) REFERENCES players(player_id),
                        UNIQUE(season_number, team_id, player_id)
                    )
                ''')

                # Add confirmed_at column to free_agency_results if it doesn't exist
                # (must run BEFORE the period_id -> season_number migration below,
                #  which copies confirmed_at across)
                cursor = await db.execute("PRAGMA table_info(free_agency_results)")
                columns = await cursor.fetchall()
                column_names = [col[1] for col in columns]

                if 'confirmed_at' not in column_names:
                    await db.execute("ALTER TABLE free_agency_results ADD COLUMN confirmed_at TIMESTAMP")

                # --- One-time migration: free_agency_periods -> settings + season_number ---
                # Older databases keyed the free agency child tables off
                # free_agency_periods.period_id. The period itself now lives in the
                # settings table and the child tables are keyed by season_number.
                # This block is idempotent: it only does work while the old table
                # (or an old period_id column) is still present.
                cursor = await db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='free_agency_periods'"
                )
                old_periods_table_exists = await cursor.fetchone() is not None

                if old_periods_table_exists:
                    # 1. Copy the most recent period's state into settings
                    cursor = await db.execute(
                        """SELECT status, season_number, auction_points
                           FROM free_agency_periods
                           ORDER BY season_number DESC
                           LIMIT 1"""
                    )
                    latest_period = await cursor.fetchone()

                    if latest_period:
                        old_status, old_season, old_auction_points = latest_period
                        await db.execute(
                            "INSERT OR REPLACE INTO settings (setting_key, setting_value) VALUES (?, ?)",
                            ("fa_period_status", str(old_status) if old_status is not None else "")
                        )
                        await db.execute(
                            "INSERT OR REPLACE INTO settings (setting_key, setting_value) VALUES (?, ?)",
                            ("fa_period_season", str(old_season) if old_season is not None else "")
                        )
                        await db.execute(
                            "INSERT OR REPLACE INTO settings (setting_key, setting_value) VALUES (?, ?)",
                            ("fa_period_auction_points", str(old_auction_points) if old_auction_points is not None else "300")
                        )

                    # 2. Re-key each child table from period_id to season_number.
                    #    SQLite before 3.35 can't DROP COLUMN, so use the standard
                    #    create-new / copy / drop / rename pattern.
                    child_tables = {
                        'free_agency_bids': (
                            '''CREATE TABLE free_agency_bids_new (
                                bid_id INTEGER PRIMARY KEY AUTOINCREMENT,
                                season_number INTEGER NOT NULL,
                                team_id INTEGER NOT NULL,
                                player_id INTEGER NOT NULL,
                                bid_amount INTEGER NOT NULL,
                                status TEXT DEFAULT 'active',
                                placed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                                FOREIGN KEY (season_number) REFERENCES seasons(season_number),
                                FOREIGN KEY (team_id) REFERENCES teams(team_id),
                                FOREIGN KEY (player_id) REFERENCES players(player_id),
                                UNIQUE(season_number, team_id, player_id)
                            )''',
                            '''INSERT INTO free_agency_bids_new
                                   (bid_id, season_number, team_id, player_id, bid_amount, status, placed_at, updated_at)
                               SELECT b.bid_id, fap.season_number, b.team_id, b.player_id,
                                      b.bid_amount, b.status, b.placed_at, b.updated_at
                               FROM free_agency_bids b
                               JOIN free_agency_periods fap ON b.period_id = fap.period_id'''
                        ),
                        'free_agency_results': (
                            '''CREATE TABLE free_agency_results_new (
                                result_id INTEGER PRIMARY KEY AUTOINCREMENT,
                                season_number INTEGER NOT NULL,
                                player_id INTEGER NOT NULL,
                                original_team_id INTEGER NOT NULL,
                                winning_team_id INTEGER,
                                winning_bid INTEGER,
                                matched BOOLEAN DEFAULT 0,
                                confirmed_at TIMESTAMP,
                                compensation_band INTEGER,
                                compensation_pick_id INTEGER,
                                FOREIGN KEY (season_number) REFERENCES seasons(season_number),
                                FOREIGN KEY (player_id) REFERENCES players(player_id),
                                FOREIGN KEY (original_team_id) REFERENCES teams(team_id),
                                FOREIGN KEY (winning_team_id) REFERENCES teams(team_id),
                                FOREIGN KEY (compensation_pick_id) REFERENCES draft_picks(pick_id),
                                UNIQUE(season_number, player_id)
                            )''',
                            '''INSERT INTO free_agency_results_new
                                   (result_id, season_number, player_id, original_team_id, winning_team_id,
                                    winning_bid, matched, confirmed_at, compensation_band, compensation_pick_id)
                               SELECT r.result_id, fap.season_number, r.player_id, r.original_team_id,
                                      r.winning_team_id, r.winning_bid, r.matched, r.confirmed_at,
                                      r.compensation_band, r.compensation_pick_id
                               FROM free_agency_results r
                               JOIN free_agency_periods fap ON r.period_id = fap.period_id'''
                        ),
                        'free_agency_resigns': (
                            '''CREATE TABLE free_agency_resigns_new (
                                resign_id INTEGER PRIMARY KEY AUTOINCREMENT,
                                season_number INTEGER NOT NULL,
                                team_id INTEGER NOT NULL,
                                player_id INTEGER NOT NULL,
                                confirmed BOOLEAN DEFAULT 0,
                                confirmed_at TIMESTAMP,
                                FOREIGN KEY (season_number) REFERENCES seasons(season_number),
                                FOREIGN KEY (team_id) REFERENCES teams(team_id),
                                FOREIGN KEY (player_id) REFERENCES players(player_id),
                                UNIQUE(season_number, team_id, player_id)
                            )''',
                            '''INSERT INTO free_agency_resigns_new
                                   (resign_id, season_number, team_id, player_id, confirmed, confirmed_at)
                               SELECT fr.resign_id, fap.season_number, fr.team_id, fr.player_id,
                                      fr.confirmed, fr.confirmed_at
                               FROM free_agency_resigns fr
                               JOIN free_agency_periods fap ON fr.period_id = fap.period_id'''
                        ),
                    }

                    for table_name, (create_sql, copy_sql) in child_tables.items():
                        cursor = await db.execute(f"PRAGMA table_info({table_name})")
                        cols = [col[1] for col in await cursor.fetchall()]
                        if 'period_id' not in cols:
                            # Already migrated (or freshly created with the new schema)
                            continue

                        await db.execute(f"DROP TABLE IF EXISTS {table_name}_new")
                        await db.execute(create_sql)
                        await db.execute(copy_sql)
                        await db.execute(f"DROP TABLE {table_name}")
                        await db.execute(f"ALTER TABLE {table_name}_new RENAME TO {table_name}")

                    # 3. Finally drop the now-unused periods table
                    await db.execute("DROP TABLE free_agency_periods")

                # Add live draft columns to drafts table if they don't exist
                cursor = await db.execute("PRAGMA table_info(drafts)")
                columns = await cursor.fetchall()
                column_names = [col[1] for col in columns]

                if 'started_at' not in column_names:
                    await db.execute("ALTER TABLE drafts ADD COLUMN started_at TIMESTAMP")
                if 'completed_at' not in column_names:
                    await db.execute("ALTER TABLE drafts ADD COLUMN completed_at TIMESTAMP")
                if 'current_pick_number' not in column_names:
                    await db.execute("ALTER TABLE drafts ADD COLUMN current_pick_number INTEGER DEFAULT 0")

                # Add live draft columns to draft_picks table if they don't exist
                cursor = await db.execute("PRAGMA table_info(draft_picks)")
                columns = await cursor.fetchall()
                column_names = [col[1] for col in columns]

                if 'passed' not in column_names:
                    await db.execute("ALTER TABLE draft_picks ADD COLUMN passed INTEGER DEFAULT 0")
                if 'picked_at' not in column_names:
                    await db.execute("ALTER TABLE draft_picks ADD COLUMN picked_at TIMESTAMP")

                # Add lineups_locked column to seasons table if it doesn't exist
                cursor = await db.execute("PRAGMA table_info(seasons)")
                columns = await cursor.fetchall()
                column_names = [col[1] for col in columns]

                if 'lineups_locked' not in column_names:
                    await db.execute("ALTER TABLE seasons ADD COLUMN lineups_locked INTEGER DEFAULT 0")

                # Add lineup_confirmed column to teams table if it doesn't exist
                cursor = await db.execute("PRAGMA table_info(teams)")
                columns = await cursor.fetchall()
                column_names = [col[1] for col in columns]

                if 'lineup_confirmed' not in column_names:
                    await db.execute("ALTER TABLE teams ADD COLUMN lineup_confirmed INTEGER DEFAULT 0")

                # Add color/color_secondary columns to teams table if they don't exist
                if 'color' not in column_names:
                    await db.execute("ALTER TABLE teams ADD COLUMN color TEXT")

                if 'color_secondary' not in column_names:
                    await db.execute("ALTER TABLE teams ADD COLUMN color_secondary TEXT")

                # Create Player Match Stats table (one row per player per
                # simulated match - replaces the old submitted_lineups
                # snapshot table as the historical "who played round N" record)
                await db.execute('''
                    CREATE TABLE IF NOT EXISTS player_match_stats (
                        stat_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        match_id INTEGER NOT NULL,
                        player_id INTEGER NOT NULL,
                        team_id INTEGER NOT NULL,
                        disposals INTEGER DEFAULT 0,
                        goals INTEGER DEFAULT 0,
                        behinds INTEGER DEFAULT 0,
                        marks INTEGER DEFAULT 0,
                        tackles INTEGER DEFAULT 0,
                        spoils INTEGER DEFAULT 0,
                        hitouts INTEGER DEFAULT 0,
                        brownlow_votes INTEGER DEFAULT 0,
                        best_fairest_votes INTEGER DEFAULT 0,
                        FOREIGN KEY (match_id) REFERENCES matches(match_id),
                        FOREIGN KEY (player_id) REFERENCES players(player_id),
                        FOREIGN KEY (team_id) REFERENCES teams(team_id),
                        UNIQUE(match_id, player_id)
                    )
                ''')

                # Add brownlow_votes column if migrating from an older DB -
                # 3-2-1 votes to the best 3 players in the match (see
                # match_sim.py's MatchResult.brownlow_votes), 0 for
                # everyone else. Existing rows backfill to 0 (the column
                # default), not re-derivable after the fact since the
                # original per-match stat lines aren't kept around.
                cursor = await db.execute("PRAGMA table_info(player_match_stats)")
                pms_columns = [col[1] for col in await cursor.fetchall()]
                if 'brownlow_votes' not in pms_columns:
                    await db.execute("ALTER TABLE player_match_stats ADD COLUMN brownlow_votes INTEGER DEFAULT 0")

                # Add best_fairest_votes column if migrating from an older
                # DB - 5-4-3-2-1 club best & fairest votes among a team's
                # own 23 players (see match_sim.py's
                # TeamMatchResult.best_and_fairest_votes), 0 for everyone
                # else. Same backfill-to-0 reasoning as brownlow_votes above.
                if 'best_fairest_votes' not in pms_columns:
                    await db.execute("ALTER TABLE player_match_stats ADD COLUMN best_fairest_votes INTEGER DEFAULT 0")

                await db.commit()

                await interaction.followup.send(
                    "✅ Database migrated successfully!\n"
                    "• Seasons table created (existing data preserved)\n"
                    "• Injuries table created\n"
                    "• Trades table recreated with new schema\n"
                    "• Draft Picks table recreated with new schema (draft_name)\n"
                    "• Suspensions table created\n"
                    "• Starting Lineups table created\n"
                    "• Ladder Positions table created\n"
                    "• Finals Bracket table created\n"
                    "• Settings table created\n"
                    "• **Players table**: Added contract_expiry column\n"
                    "• **Drafts table**: Added rookie_contract_years column\n"
                    "• **Free Agency tables**: Created all FA/contract tables\n"
                    "• **Free Agency period**: Moved from the free_agency_periods table into settings\n"
                    "• **Seasons table**: Added lineups_locked column\n"
                    "• **Teams table**: Added lineup_confirmed column\n"
                    "• **Teams table**: Added color and color_secondary columns\n"
                    "• Player Match Stats table created\n\n"
                    "You can now use all season, injury, suspension, lineup, and free agency commands.\n"
                    "Use `/config lineups_channel:<#channel>` to configure where lineups are posted.",
                    ephemeral=True
                )
            except Exception as e:
                await interaction.followup.send(
                    f"❌ Migration failed: {str(e)}",
                    ephemeral=True
                )

    @app_commands.command(name="startseason", description="[ADMIN] End the current off-season and begin the next season")
    @app_commands.describe(
        offseason_weeks="Number of weeks in offseason (default: 23)",
        next_season_rounds="Number of regular-season rounds for the season being started (default: 24)"
    )
    async def start_season(self, interaction: discord.Interaction, offseason_weeks: int = 23, next_season_rounds: int = None):
        await interaction.response.defer(ephemeral=True)

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Find the offseason
                cursor = await db.execute(
                    """SELECT season_id, season_number, regular_rounds, total_rounds FROM seasons
                       WHERE status = 'offseason'
                       ORDER BY season_number DESC LIMIT 1"""
                )
                season = await cursor.fetchone()

                if not season:
                    # No offseason to advance from - either the very first
                    # season the league has ever run (bootstrap Season 1
                    # directly, since there's no /createseason anymore to
                    # do it separately), or a genuine setup error if a
                    # season already exists in some OTHER status.
                    cursor = await db.execute("SELECT COUNT(*) FROM seasons")
                    if (await cursor.fetchone())[0] > 0:
                        await interaction.followup.send(
                            "❌ No offseason found to advance from, but seasons already exist - "
                            "check `/seasonstatus` for the current state.",
                            ephemeral=True
                        )
                        return

                    bootstrap_rounds = next_season_rounds if next_season_rounds is not None else 24
                    total_rounds = bootstrap_rounds + len(FINALS_ROUNDS)
                    round_name = get_round_name(1, bootstrap_rounds)
                    cursor = await db.execute(
                        """INSERT INTO seasons (season_number, current_round, regular_rounds, total_rounds, round_name, status)
                           VALUES (1, 1, ?, ?, ?, 'active')""",
                        (bootstrap_rounds, total_rounds, round_name)
                    )
                    await db.commit()

                    created_seasons = await ensure_future_seasons_exist(db, 1, num_future=2)
                    indicative_draft = await _ensure_draft_promoted_to_current(db, 1)

                    message = f"✅ **Season 1** has started! ({bootstrap_rounds} regular-season rounds)\nCurrent: {round_name}"
                    if indicative_draft:
                        message += f"\n\n📋 **{indicative_draft}** is now indicative - `/draftorder` shows the live order, updated every round."
                    if created_seasons:
                        message += f"\n\n🔮 **Auto-created future seasons for trading:**"
                        for future_season in created_seasons:
                            draft_name = f"Season {future_season - 1} National Draft"
                            message += f"\n• Season {future_season} with **{draft_name}**"

                    await interaction.followup.send(message, ephemeral=True)
                    return

                season_id, season_number, regular_rounds, total_rounds = season

                # Get previous season's final round to calculate injury carryover
                cursor = await db.execute(
                    """SELECT season_id, total_rounds FROM seasons
                       WHERE status = 'completed'
                       ORDER BY season_number DESC LIMIT 1"""
                )
                prev_season = await cursor.fetchone()

                carried_over = 0
                healed_during_offseason = 0
                suspensions_carried_over = 0

                if prev_season:
                    prev_season_id, prev_total_rounds = prev_season

                    # Find injuries that were still active at end of previous season
                    cursor = await db.execute(
                        """SELECT injury_id, player_id, injury_type, return_round
                           FROM injuries
                           WHERE status = 'injured' AND return_round > ?""",
                        (prev_total_rounds,)
                    )
                    active_injuries = await cursor.fetchall()

                    for injury_id, player_id, injury_type, old_return_round in active_injuries:
                        # Calculate weeks remaining from end of last season
                        weeks_remaining = old_return_round - prev_total_rounds

                        # Subtract offseason weeks
                        weeks_into_new_season = weeks_remaining - offseason_weeks

                        if weeks_into_new_season <= 0:
                            # Injury healed during offseason - remove the record
                            await db.execute(
                                "DELETE FROM injuries WHERE injury_id = ?",
                                (injury_id,)
                            )
                            healed_during_offseason += 1
                        else:
                            # Injury carries over - update return round for new season
                            new_return_round = weeks_into_new_season
                            await db.execute(
                                """UPDATE injuries
                                   SET return_round = ?
                                   WHERE injury_id = ?""",
                                (new_return_round, injury_id)
                            )
                            carried_over += 1

                    # Suspensions carry their games_remaining straight into
                    # the new season UNCHANGED - unlike injuries, they only
                    # tick down on rounds the player's team actually plays,
                    # and the offseason has zero games, so nothing is served
                    # during it. Any row still status='suspended' here
                    # genuinely still has games owing (the per-round sweep in
                    # advance_to_next_round already deletes finished ones),
                    # so every active suspension carries over unchanged -
                    # none can complete purely from time passing, and there's
                    # nothing to actually update on the row.
                    cursor = await db.execute(
                        "SELECT COUNT(*) FROM suspensions WHERE status = 'suspended'"
                    )
                    suspensions_carried_over = (await cursor.fetchone())[0]

                # Update player ages at START of new season
                # The new season (season_number + 1) is starting, so age players for that year
                cursor = await db.execute(
                    "SELECT setting_value FROM settings WHERE setting_key = 'season_1_year'"
                )
                setting_result = await cursor.fetchone()
                if setting_result:
                    season_1_year = int(setting_result[0])
                    # Calculate the year for the NEW season that's about to start
                    new_season_year = season_1_year + season_number  # season_number is the offseason, +1 is the new season
                    await db.execute(
                        "UPDATE players SET age = ? - birth_year WHERE birth_year IS NOT NULL",
                        (new_season_year,)
                    )

                # Mark the offseason season as completed
                await db.execute(
                    """UPDATE seasons
                       SET status = 'completed', round_name = 'Season Complete'
                       WHERE season_id = ?""",
                    (season_id,)
                )

                # Get or create the NEXT season (season_number + 1). Its
                # regular_rounds is always taken from THIS command's own
                # next_season_rounds param (default 24) - overriding
                # whatever the row already had, whether it's a brand-new
                # row created right here or a pre-existing 'future' row
                # seeded earlier by ensure_future_seasons_exist's own
                # hardcoded default. This is deliberately the ONE place
                # a season's length is set - moved here from the old
                # /endseason (removed) since it makes more sense to choose
                # a season's length when it's actually starting.
                next_season_num = season_number + 1
                next_regular_rounds = next_season_rounds if next_season_rounds is not None else 24
                next_total_rounds = next_regular_rounds + len(FINALS_ROUNDS)

                cursor = await db.execute(
                    "SELECT season_id, status FROM seasons WHERE season_number = ?",
                    (next_season_num,)
                )
                next_season_result = await cursor.fetchone()

                if not next_season_result:
                    # Next season doesn't exist, create it
                    cursor = await db.execute(
                        """INSERT INTO seasons (season_number, current_round, regular_rounds, total_rounds, round_name, status)
                           VALUES (?, 1, ?, ?, 'Round 1', 'active')""",
                        (next_season_num, next_regular_rounds, next_total_rounds)
                    )
                    next_season_id = cursor.lastrowid
                else:
                    next_season_id, next_status = next_season_result
                    if next_status != 'future':
                        await interaction.followup.send(
                            f"❌ Season {next_season_num} has unexpected status '{next_status}' (expected 'future')",
                            ephemeral=True
                        )
                        return

                # Start the next season
                round_name = get_round_name(1, next_regular_rounds)
                await db.execute(
                    """UPDATE seasons
                       SET current_round = 1, round_name = ?, status = 'active',
                           regular_rounds = ?, total_rounds = ?
                       WHERE season_id = ?""",
                    (round_name, next_regular_rounds, next_total_rounds, next_season_id)
                )
                await db.commit()

                # Ensure next 2 future seasons exist for draft pick trading
                created_seasons = await ensure_future_seasons_exist(db, next_season_num, num_future=2)

                # Promote THIS season's own National Draft from 'future' to
                # 'current' now that its ladder is live and worth showing -
                # /draftorder becomes browsable as an "indicative" order for
                # the whole season, updated every round by
                # update_indicative_draft_order. See
                # _ensure_draft_promoted_to_current's own docstring - the
                # same helper is also called defensively from
                # advance_to_next_round in case a season was ever started
                # before this feature existed (or the promotion was somehow
                # missed here), so the draft self-heals to 'current' the
                # first time a round advances rather than staying stuck.
                indicative_draft = await _ensure_draft_promoted_to_current(db, next_season_num)

                message = f"✅ **Season {next_season_num}** has started! ({next_regular_rounds} regular-season rounds)\nCurrent: {round_name}\n"
                message += f"**Previous:** Offseason {season_number} → Completed"

                if indicative_draft:
                    message += f"\n\n📋 **{indicative_draft}** is now indicative - `/draftorder` shows the live order, updated every round."

                if created_seasons:
                    message += f"\n\n🔮 **Auto-created future seasons for trading:**"
                    for future_season in created_seasons:
                        draft_name = f"Season {future_season - 1} National Draft"
                        message += f"\n• Season {future_season} with **{draft_name}**"

                if carried_over > 0 or healed_during_offseason > 0:
                    message += f"\n\n**Injury Updates:**"
                    if healed_during_offseason > 0:
                        message += f"\n• {healed_during_offseason} player(s) healed during offseason"
                    if carried_over > 0:
                        message += f"\n• {carried_over} injury(ies) carried over to new season"

                if suspensions_carried_over > 0:
                    message += f"\n\n**Suspension Updates:**"
                    message += f"\n• {suspensions_carried_over} suspension(s) carried over to new season"

                # Post injury list to configured channel
                cursor = await db.execute(
                    "SELECT setting_value FROM settings WHERE setting_key = 'injury_list_channel_id'"
                )
                result = await cursor.fetchone()
                if result and result[0]:
                    injury_commands = self.bot.get_cog('InjuryCommands')
                    if injury_commands:
                        await injury_commands.post_injury_list_to_channel(db, int(result[0]))

                await interaction.followup.send(message, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    async def _try_announce_lineups(self, db):
        """Locks in and posts every team's lineup for the round - called
        from the "Announce Lineups" button on /matchsimulation's panel
        (match_commands.py, via self.bot.get_cog('SeasonCommands')) and
        from _AnnounceLineupsMissingView's force-submit button below.
        Returns either a success/failure message (str) or, when one or more
        teams are blocking the round, (blocking_team_ids, blocking_team_emojis)
        for the caller to offer the force-submit (and, via that same button,
        auto-lineup) option. "Blocking" covers BOTH teams that never
        confirmed at all AND teams that confirmed but are no longer valid
        (e.g. a player got injured after they submitted) - both are
        recoverable the same way, so they're not distinguished in the
        return value. See lineup_commands.py's validate_lineup/
        format_lineup_description for the shared logic this reuses -
        deferred import to avoid a season_commands<->lineup_commands
        circular import at module load time."""
        from commands.lineup_commands import validate_lineup, format_lineup_description, team_playing_this_round

        cursor = await db.execute(
            "SELECT season_id, season_number, current_round, lineups_locked FROM seasons WHERE status = 'active' LIMIT 1"
        )
        season = await cursor.fetchone()
        if not season:
            return "❌ No active season!"
        season_id, season_number, current_round, lineups_locked = season

        if lineups_locked:
            return "❌ Lineups are already locked for this round."

        cursor = await db.execute(
            "SELECT team_id, team_name, emoji_id, lineup_confirmed FROM teams WHERE team_name != 'Draft Pool' ORDER BY team_name"
        )
        all_teams = await cursor.fetchall()

        # Teams without a fixture this round (a bye, or eliminated from the
        # finals) have nothing to submit - skip them entirely so they don't
        # block the round or get a lineup posted for a match that doesn't exist.
        teams = [
            t for t in all_teams
            if await team_playing_this_round(db, t[0], current_round)
        ]

        # A team blocks the round if it never confirmed, OR if it confirmed
        # but its CURRENT live lineup is no longer valid (state can drift
        # between confirming and the admin running this command - e.g. a
        # late injury). Both cases get the same recovery path.
        blocking = []
        for team_id, team_name, emoji_id, confirmed in teams:
            if not confirmed:
                blocking.append((team_id, emoji_id))
                continue
            errors, player_ids = await validate_lineup(db, team_id, current_round)
            if errors:
                blocking.append((team_id, emoji_id))

        if blocking:
            blocking_emojis = [get_team_emoji_str(self.bot, emoji_id) for _, emoji_id in blocking]
            return [t[0] for t in blocking], blocking_emojis

        # Lineups channel is optional - if unset (or no longer resolvable),
        # lineups still lock in below, just without posting each team's
        # lineup embed anywhere. Lets testing skip the per-team posting
        # loop entirely (useful when simming rounds repeatedly and the
        # posted lineups aren't needed) rather than blocking on `/config`.
        cursor = await db.execute(
            "SELECT setting_value FROM settings WHERE setting_key = 'lineups_channel_id'"
        )
        channel_row = await cursor.fetchone()
        lineup_channel = self.bot.get_channel(int(channel_row[0])) if channel_row and channel_row[0] else None

        cursor = await db.execute(
            "SELECT regular_rounds FROM seasons WHERE season_id = ?", (season_id,)
        )
        regular_rounds = (await cursor.fetchone())[0]
        round_display = get_round_name(current_round, regular_rounds) if current_round > 0 else "Offseason"

        async def _build_team_lineup_section(team_id):
            """Fetches one team's lineup plus its IN/OUT diff against the
            last round it actually played (sourced from player_match_stats -
            the historical "who played" record), and renders both into the
            text block used for that team's half of the match embed."""
            cursor = await db.execute(
                """SELECT l.position_name, p.player_id, p.name, p.position, p.overall_rating
                   FROM lineups l
                   JOIN players p ON l.player_id = p.player_id AND p.team_id = l.team_id
                   WHERE l.team_id = ?
                   ORDER BY l.slot_number""",
                (team_id,)
            )
            lineup = await cursor.fetchall()

            # IN/OUT diff - the subquery finds this team's most recent
            # SIMULATED round before this one; the outer query pulls every
            # player_id from exactly that round in one round-trip.
            ins_names, outs_names = [], []
            cursor = await db.execute(
                """SELECT pms.player_id FROM player_match_stats pms
                   JOIN matches m ON pms.match_id = m.match_id
                   WHERE m.season_id = ? AND pms.team_id = ? AND m.round_number = (
                       SELECT MAX(m2.round_number) FROM player_match_stats pms2
                       JOIN matches m2 ON pms2.match_id = m2.match_id
                       WHERE m2.season_id = ? AND pms2.team_id = ? AND m2.round_number < ? AND m2.simulated = 1
                   )""",
                (season_id, team_id, season_id, team_id, current_round)
            )
            previous_rows = await cursor.fetchall()
            if previous_rows:
                previous_player_ids = {row[0] for row in previous_rows}
                current_player_ids = {p[1] for p in lineup}
                ins = current_player_ids - previous_player_ids
                outs = previous_player_ids - current_player_ids

                if ins:
                    placeholders = ','.join('?' * len(ins))
                    cursor = await db.execute(
                        f"SELECT name, overall_rating FROM players WHERE player_id IN ({placeholders})",
                        list(ins)
                    )
                    ins_names = [f"{name} ({ovr})" for name, ovr in await cursor.fetchall()]

                if outs:
                    placeholders = ','.join('?' * len(outs))
                    cursor = await db.execute(
                        f"SELECT player_id, name, overall_rating FROM players WHERE player_id IN ({placeholders})",
                        list(outs)
                    )
                    out_players = await cursor.fetchall()

                    cursor = await db.execute(
                        f"""SELECT player_id FROM injuries
                            WHERE player_id IN ({placeholders}) AND status = 'injured' AND return_round > ?""",
                        list(outs) + [current_round]
                    )
                    injured_ids = {row[0] for row in await cursor.fetchall()}

                    cursor = await db.execute(
                        f"""SELECT player_id FROM suspensions
                            WHERE player_id IN ({placeholders}) AND status = 'suspended'""",
                        list(outs)
                    )
                    suspended_ids = {row[0] for row in await cursor.fetchall()}

                    for player_id, name, ovr in out_players:
                        if player_id in injured_ids:
                            outs_names.append(f"{name} ({ovr}) (injured)")
                        elif player_id in suspended_ids:
                            outs_names.append(f"{name} ({ovr}) (suspended)")
                        else:
                            outs_names.append(f"{name} ({ovr}) (omitted)")

            section_text = format_lineup_description(lineup)
            if ins_names or outs_names:
                changes_text = "\n\n"
                if ins_names:
                    changes_text += f"**IN:** {', '.join(ins_names)}\n"
                if outs_names:
                    changes_text += f"**OUT:** {', '.join(outs_names)}"
                section_text += changes_text

            return section_text

        if lineup_channel is not None:
            team_by_id = {t[0]: (t[1], t[2]) for t in teams}
            cursor = await db.execute(
                """SELECT match_id, home_team_id, away_team_id FROM matches
                   WHERE season_id = ? AND round_number = ?
                   ORDER BY match_id""",
                (season_id, current_round)
            )
            round_matches = await cursor.fetchall()

            for match_id, home_team_id, away_team_id in round_matches:
                # Both sides of a fixture are always in `teams` together (a
                # bye/finals-eliminated team has no fixture row at all), so
                # this lookup can't partially miss - if home isn't in the
                # dict, away won't be either, and vice versa.
                if home_team_id not in team_by_id or away_team_id not in team_by_id:
                    continue

                home_name, home_emoji_id = team_by_id[home_team_id]
                away_name, away_emoji_id = team_by_id[away_team_id]

                # One embed per team (not one combined embed per match -
                # tried that, looked messy with both lineups stacked in one
                # embed), but still posted in fixture/match order rather
                # than alphabetically, home then away, so the lineups
                # channel reads round-by-round the way the fixture does.
                for team_id, team_name, emoji_id in (
                    (home_team_id, home_name, home_emoji_id),
                    (away_team_id, away_name, away_emoji_id),
                ):
                    emoji = get_team_emoji_str(self.bot, emoji_id)
                    embed = discord.Embed(
                        title=f"{emoji}{team_name} - {round_display} Lineup",
                        color=discord.Color.green()
                    )
                    embed.description = await _build_team_lineup_section(team_id)
                    await lineup_channel.send(embed=embed)

        await db.execute("UPDATE seasons SET lineups_locked = 1 WHERE season_id = ?", (season_id,))
        await db.commit()

        if lineup_channel is not None:
            return f"✅ All {len(teams)} lineups locked in and posted to {lineup_channel.mention} for **{round_display}**."
        return f"✅ All {len(teams)} lineups locked in for **{round_display}** (no lineups channel set - nothing posted)."

    async def advance_to_next_round(self, db) -> str:
        """Shared logic behind the Match Simulation panel's "Advance to Next
        Round" button (previously the standalone /nextround command). Returns
        a response string - either a ❌ guard-clause refusal or a ✅ success
        summary - never raises for expected failure cases."""
        # Get active season
        cursor = await db.execute(
            """SELECT season_id, season_number, current_round, regular_rounds, total_rounds
               FROM seasons WHERE status = 'active' LIMIT 1"""
        )
        season = await cursor.fetchone()

        if not season:
            return "❌ No active season! Start a season first with `/startseason`."

        season_id, season_number, current_round, regular_rounds, total_rounds = season

        # Check if season is complete
        if current_round >= total_rounds:
            return f"❌ Season {season_number} is complete! Use `/endseason` to finish it."

        # Refuse to advance while this round still has unsimulated
        # fixture matches - a fixture that was never entered at all
        # (no rows for this round) is NOT the same thing and does
        # not block, since fixtures are manual-entry-only for now.
        cursor = await db.execute(
            "SELECT COUNT(*) FROM matches WHERE season_id = ? AND round_number = ? AND simulated = 0",
            (season_id, current_round)
        )
        unsimulated_count = (await cursor.fetchone())[0]
        if unsimulated_count > 0:
            return (
                f"❌ {unsimulated_count} match(es) in the current round haven't been simulated yet - "
                f"use `/matchsimulation` first."
            )

        # Reveal the real recovery length for any injury that happened
        # during the round that's about to end (still TBC until now - see
        # MatchCommands._persist_match_result) - must run BEFORE
        # post_round_summaries below so its build_injury_suspension_list
        # call shows the actual weeks-remaining instead of "TBC" for an
        # injury that just happened this round, and BEFORE the
        # recovered-players check further down so a freshly-revealed
        # return_round is already in place for it to read. Same reasoning
        # for reports -> suspensions below it.
        next_round_num = current_round + 1
        await _roll_pending_injury_recoveries(db, next_round_num)
        just_revealed_suspension_ids = await _roll_pending_report_suspensions(db, next_round_num)

        # Tick down suspensions for the round that's ENDING - unlike
        # injuries (flat weeks), a suspension only counts down for a
        # player whose TEAM actually played a simulated match in
        # current_round. Must run BEFORE post_round_summaries below so its
        # build_injury_suspension_list call shows the correct
        # games-remaining for a suspension that's fully served by the
        # round just ending (previously this ran AFTER post_round_summaries,
        # so a suspension completed by this round's match still showed its
        # PRE-tick-down count - e.g. "1 game left" - in that same round's
        # summary, even though the round that just finished was the one
        # serving it).
        #
        # Deliberately does NOT delete a just-completed suspension here -
        # only decrements games_remaining to 0 and records its
        # suspension_id in completed_suspension_ids. Deletion happens AFTER
        # post_round_summaries below (mirroring the injury-recovery block
        # further down, which reads return_round <= next_round_num and
        # only deletes there too) so build_injury_suspension_list still
        # sees the row this one time and renders its "✅ Available" status
        # line - a player who was just served should show that once, not
        # silently vanish from the list the same round they're freed up.
        #
        # A bye round (team has no fixture row, or its fixture row is
        # still unsimulated for some reason) doesn't serve any of the
        # suspension, so those players are simply skipped this pass and
        # checked again next round.
        cursor = await db.execute(
            """SELECT s.suspension_id, p.team_id, s.games_remaining
               FROM suspensions s
               JOIN players p ON s.player_id = p.player_id
               WHERE s.status = 'suspended'""",
        )
        active_suspensions = await cursor.fetchall()

        completed_suspension_ids = []
        for suspension_id, team_id, games_remaining in active_suspensions:
            if team_id is None:
                continue
            if games_remaining is None:
                # Still TBC (a report from a round that hasn't fully ended
                # yet, e.g. mid-round with other matches still unsimulated -
                # _roll_pending_report_suspensions above only resolves
                # reports whose OWN round has already finished) - nothing
                # to tick down yet, checked again once it's revealed.
                continue
            if suspension_id in just_revealed_suspension_ids:
                # Just rolled from TBC above, THIS SAME call - its
                # suspension_round is the round the player was already
                # reported in (a round they already played), so that round
                # must not also count as served here - see
                # _roll_pending_report_suspensions' docstring for why this
                # doesn't apply to /addsuspension's own suspensions.
                continue

            cursor = await db.execute(
                """SELECT 1 FROM matches
                   WHERE season_id = ? AND round_number = ? AND simulated = 1
                     AND (home_team_id = ? OR away_team_id = ?)
                   LIMIT 1""",
                (season_id, current_round, team_id, team_id)
            )
            team_played = await cursor.fetchone() is not None
            if not team_played:
                continue

            new_games_remaining = max(0, games_remaining - 1)
            await db.execute(
                "UPDATE suspensions SET games_remaining = ? WHERE suspension_id = ?",
                (new_games_remaining, suspension_id)
            )
            if new_games_remaining <= 0:
                completed_suspension_ids.append(suspension_id)

        if active_suspensions:
            await db.commit()

        # The round just finished (guaranteed complete by the guard
        # above) - post each team's round summary now, for the round
        # that's ENDING, before it advances. Previously this fired the
        # moment the round's last match was simulated (match_commands.py);
        # moved here so it lines up with the admin's own "the round is
        # over" moment instead of firing mid-/matchsimulation. Fires for
        # EVERY round with a fixture, including finals - teams still want
        # their result/stats/injuries/lineup-button post during finals,
        # even though the competitive ladder itself is frozen by then (see
        # below). ranked_ladder is only computed/posted for a REGULAR-
        # season round ending - compute_and_store_ladder itself excludes
        # finals-round matches, so re-running it during finals would just
        # repost the same unchanged ladder every week for no reason.
        # post_round_summaries handles an empty ranked_ladder by simply
        # omitting its ladder-position line rather than showing anything
        # stale/misleading.
        cursor = await db.execute(
            "SELECT COUNT(*) FROM matches WHERE season_id = ? AND round_number = ?",
            (season_id, current_round)
        )
        has_fixture = (await cursor.fetchone())[0] > 0
        if has_fixture:
            ranked_ladder = []
            if current_round <= regular_rounds:
                ranked_ladder = await compute_and_store_ladder(db, season_id)
                await post_ladder(
                    self.bot, db, season_id, season_number, current_round,
                    regular_rounds, ranked_ladder,
                )
            await post_round_summaries(
                self.bot, db, season_id, season_number, current_round,
                regular_rounds, total_rounds, ranked_ladder, next_round_num,
            )

            # Self-heal: promote this season's own draft to 'current' if
            # it's somehow still 'future' (normally /startseason already
            # did this - see _ensure_draft_promoted_to_current's own
            # docstring for why this defensive check also lives here).
            await _ensure_draft_promoted_to_current(db, season_number)

            # Keep next season's National Draft's indicative pick order in
            # sync with this round's result - both regular season (uses
            # the ladder_positions just recomputed above) and finals
            # (_indicative_draft_order accounts for who's still alive vs
            # eliminated once finals are underway) - see that function and
            # update_indicative_draft_order's own docstrings.
            await update_indicative_draft_order(db, season_id, season_number, regular_rounds, current_round)

        # Advance to next round
        next_round_name = get_round_name(next_round_num, regular_rounds)

        await db.execute(
            """UPDATE seasons
               SET current_round = ?, round_name = ?, lineups_locked = 0
               WHERE season_id = ?""",
            (next_round_num, next_round_name, season_id)
        )
        await db.execute("UPDATE teams SET lineup_confirmed = 0 WHERE team_name != 'Draft Pool'")

        # Auto-generate the next finals round's fixture, if next_round_num
        # lands on one of the 5 finals weeks (Wildcard/QF-EF/Semis/
        # Prelims/GF) - a no-op for a regular round. Fixtures for every
        # OTHER round are set via /importdata's Excel import; this is the
        # one exception, since finals matchups depend on bracket results
        # the admin has no way to know in advance.
        await _generate_finals_round(db, season_id, next_round_num, regular_rounds)
        await db.commit()

        # Check for players who have recovered from injuries
        cursor = await db.execute(
            "SELECT injury_id FROM injuries WHERE status = 'injured' AND return_round <= ?",
            (next_round_num,)
        )
        recovered_players = await cursor.fetchall()

        # Recovered - remove the injury records. No longer notified per-team
        # here - injury/recovery status now shows up in the match report
        # messages instead (see build_injury_suspension_list). Runs AFTER
        # post_round_summaries above (not before) precisely so a player who
        # recovered THIS round still appears there one last time, rendered
        # by build_injury_suspension_list as "✅ Recovered", before being
        # removed for good.
        for (injury_id,) in recovered_players:
            await db.execute(
                "DELETE FROM injuries WHERE injury_id = ?",
                (injury_id,)
            )

        if recovered_players:
            await db.commit()

        # Suspensions ticked down to 0 games above are deleted here, for the
        # same reason and with the same timing as the injury recovery block
        # just above - post_round_summaries already ran with the row still
        # present (games_remaining=0, rendered as "✅ Available"), so it's
        # now safe to remove for good.
        for suspension_id in completed_suspension_ids:
            await db.execute(
                "DELETE FROM suspensions WHERE suspension_id = ?",
                (suspension_id,)
            )

        if completed_suspension_ids:
            await db.commit()

        # Post injury list to configured channel
        cursor = await db.execute(
            "SELECT setting_value FROM settings WHERE setting_key = 'injury_list_channel_id'"
        )
        result = await cursor.fetchone()
        if result and result[0]:
            injury_commands = self.bot.get_cog('InjuryCommands')
            if injury_commands:
                await injury_commands.post_injury_list_to_channel(db, int(result[0]))

        response = f"✅ Advanced to **{next_round_name}** of Season {season_number}"
        if recovered_players:
            response += f"\n\n🏥 {len(recovered_players)} player(s) recovered from injury"
        if completed_suspension_ids:
            response += f"\n🚫 {len(completed_suspension_ids)} suspension(s) completed"

        return response

    @app_commands.command(name="editseasonlength", description="[ADMIN] Edit a season's number of regular-season rounds")
    @app_commands.describe(
        season_number="Season number to edit",
        regular_rounds="New number of regular season rounds"
    )
    async def edit_season(
        self,
        interaction: discord.Interaction,
        season_number: int,
        regular_rounds: int
    ):
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Find the season
                cursor = await db.execute(
                    "SELECT season_id, status FROM seasons WHERE season_number = ?",
                    (season_number,)
                )
                season = await cursor.fetchone()

                if not season:
                    await interaction.response.send_message(
                        f"❌ Season {season_number} not found!",
                        ephemeral=True
                    )
                    return

                season_id, status = season

                # Calculate new total rounds
                total_rounds = regular_rounds + len(FINALS_ROUNDS)

                # Update the season
                await db.execute(
                    """UPDATE seasons
                       SET regular_rounds = ?, total_rounds = ?
                       WHERE season_id = ?""",
                    (regular_rounds, total_rounds, season_id)
                )
                await db.commit()

                await interaction.response.send_message(
                    f"✅ Updated **Season {season_number}** to {regular_rounds} rounds"
                )
        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)

    @app_commands.command(name="setround", description="[ADMIN] Skip to a specific round")
    @app_commands.describe(round_number="Round number to skip to")
    async def set_round(self, interaction: discord.Interaction, round_number: int):
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get active season
                cursor = await db.execute(
                    """SELECT season_id, season_number, regular_rounds, total_rounds
                       FROM seasons WHERE status = 'active' LIMIT 1"""
                )
                season = await cursor.fetchone()

                if not season:
                    await interaction.response.send_message(
                        "❌ No active season! Start a season first with `/startseason`.",
                        ephemeral=True
                    )
                    return

                season_id, season_number, regular_rounds, total_rounds = season

                # Validate round number
                if round_number < 1 or round_number > total_rounds:
                    await interaction.response.send_message(
                        f"❌ Round number must be between 1 and {total_rounds}!",
                        ephemeral=True
                    )
                    return

                # Set the round
                round_name = get_round_name(round_number, regular_rounds)

                await db.execute(
                    """UPDATE seasons
                       SET current_round = ?, round_name = ?
                       WHERE season_id = ?""",
                    (round_number, round_name, season_id)
                )
                await db.commit()

                await interaction.response.send_message(
                    f"✅ Skipped to **{round_name}** of Season {season_number}"
                )
        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)

    @app_commands.command(name="endseason", description="[ADMIN] End the current season and create offseason")
    async def end_season(self, interaction: discord.Interaction):
        # Deferred immediately - post_season_summaries below sends one
        # embed per team (a real channel.send per team, not batched), which
        # can easily run past Discord's 3-second initial-response window
        # once there are more than a handful of teams, causing the
        # interaction token to expire before any response is sent ("Unknown
        # interaction" / 404). Every response from here on uses
        # interaction.followup.send, not interaction.response.send_message.
        await interaction.response.defer(ephemeral=True)
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get active season
                cursor = await db.execute(
                    """SELECT season_id, season_number, regular_rounds, current_round, total_rounds FROM seasons
                       WHERE status = 'active' LIMIT 1"""
                )
                season = await cursor.fetchone()

                if not season:
                    await interaction.followup.send(
                        "❌ No active season to end!",
                        ephemeral=True
                    )
                    return

                season_id, season_number, current_regular_rounds, ending_current_round, ending_total_rounds = season

                # Placeholder only - the actual round count for the next
                # season is chosen at /startseason time (that's the ONE
                # place it's set now, overriding whatever's stored here
                # regardless), moved there since it makes more sense to
                # choose a season's length when it's actually starting
                # rather than when the previous one ends. Using the
                # ending season's own regular_rounds here just keeps this
                # row's numbers sane in the meantime, matching
                # ensure_future_seasons_exist's own placeholder convention.
                next_season_rounds = current_regular_rounds

                # Force-resolve any injury or report still TBC
                # (recovery_rounds / games_missed IS NULL - see
                # _roll_pending_injury_recoveries / _roll_pending_report_
                # suspensions) before this season ends. Normally these only
                # clear via advance_to_next_round moving past the round they
                # happened in, but /endseason can be run manually at any
                # point (no "season fully complete" guard) - without this,
                # an injury from a round the admin never advanced past
                # would stay permanently TBC and silently vanish from
                # /startseason's carryover query (which excludes NULL
                # return_round rows entirely), and a still-TBC report would
                # carry an unrolled sanction into the new season.
                await _roll_pending_injury_recoveries(db, next_round_num=10**9)
                await _roll_pending_report_suspensions(db, next_round_num=10**9)

                # End the current season (set to offseason, not completed)
                # Note: Player ages will be updated when the new season STARTS (in /startseason)
                await db.execute(
                    """UPDATE seasons
                       SET status = 'offseason', round_name = 'Offseason'
                       WHERE season_id = ?""",
                    (season_id,)
                )
                await db.commit()

                # Post each team's own end-of-season summary (final ladder
                # position, leading goalkicker, best & fairest winner + top
                # 10) to their own channel - see post_season_summaries.
                # Committed just above first so this read-only pass sees
                # the final, already-resolved injury/report data.
                await post_season_summaries(self.bot, db, season_id, season_number)

                # Post the league-wide injury/suspension list to the
                # configured channel, same as every advance_to_next_round
                # does - /endseason doesn't go through that function, so
                # without this the list would just go silent for the whole
                # offseason. current_round is left at its final in-season
                # value by the UPDATE above (not reset to 0), so the same
                # weeks/games-remaining math the last round summary already
                # showed still applies here unchanged.
                cursor = await db.execute(
                    "SELECT setting_value FROM settings WHERE setting_key = 'injury_list_channel_id'"
                )
                result = await cursor.fetchone()
                if result and result[0]:
                    from commands.injury_commands import build_injury_suspension_list, _chunk_lines_into_descriptions
                    combined_list = await build_injury_suspension_list(
                        self.bot, db, ending_current_round, ending_total_rounds,
                        season_id=season_id, regular_rounds=current_regular_rounds,
                    )
                    if combined_list:
                        descriptions = _chunk_lines_into_descriptions(combined_list, max_length=4000)
                        embeds = [
                            discord.Embed(
                                title=f"Injury & Suspension List - Off-season" if i == 0 else None,
                                description=description,
                                color=discord.Color.red(),
                            )
                            for i, description in enumerate(descriptions)
                        ]
                        channel = self.bot.get_channel(int(result[0]))
                        if channel:
                            await channel.send(embeds=embeds)

                # Check if next season already exists
                next_season_num = season_number + 1
                cursor = await db.execute(
                    "SELECT season_id, status FROM seasons WHERE season_number = ?",
                    (next_season_num,)
                )
                existing = await cursor.fetchone()

                message = f"✅ **Season {season_number}** has ended and is now in offseason!"

                if existing:
                    existing_id, existing_status = existing
                    # Next season already exists - should be 'future'
                    if existing_status != 'future':
                        message += f"\n⚠️ Season {next_season_num} already exists with status '{existing_status}' (expected 'future')"
                    else:
                        message += f"\n✅ **Season {next_season_num}** is ready as a future season"
                else:
                    # Create next season as 'future' status
                    total_rounds = next_season_rounds + len(FINALS_ROUNDS)
                    await db.execute(
                        """INSERT INTO seasons (season_number, current_round, regular_rounds, total_rounds, round_name, status)
                           VALUES (?, 0, ?, ?, 'Future', 'future')""",
                        (next_season_num, next_season_rounds, total_rounds)
                    )
                    message += f"\n✅ **Season {next_season_num}** created as future season ({next_season_rounds} rounds)"

                await db.commit()

                # Ensure next_season_num itself has its own draft+picks -
                # the block above only ever creates/confirms its `seasons`
                # row, never its draft (see _ensure_season_draft_and_picks's
                # own docstring for why this was split out: a season row
                # existing does NOT imply its draft/picks do).
                cursor = await db.execute("SELECT team_id, team_name FROM teams ORDER BY team_name")
                teams = await cursor.fetchall()
                if teams and await _ensure_season_draft_and_picks(db, next_season_num, teams):
                    await db.commit()
                    message += f"\n✅ **Season {next_season_num}**'s National Draft is ready for trading"

                # Ensure 2 more future seasons exist beyond the next season
                created_seasons = await ensure_future_seasons_exist(db, next_season_num, num_future=2)

                if created_seasons:
                    message += f"\n\n🔮 **Auto-created future seasons:**"
                    for future_season in created_seasons:
                        draft_name = f"Season {future_season - 1} National Draft"
                        message += f"\n• Season {future_season} with **{draft_name}**"

                message += f"\n\n**Current Status:** Offseason {season_number}"
                message += f"\n\nUse `/startseason` when ready to begin Season {next_season_num}."

                await interaction.followup.send(message, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @app_commands.command(name="seasonstatus", description="View the current season status")
    async def current_season(self, interaction: discord.Interaction):
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get active or most recent season
                cursor = await db.execute(
                    """SELECT season_number, current_round, total_rounds, round_name, status
                       FROM seasons
                       ORDER BY
                           CASE status
                               WHEN 'active' THEN 1
                               WHEN 'offseason' THEN 2
                               ELSE 3
                           END,
                           season_number DESC
                       LIMIT 1"""
                )
                season = await cursor.fetchone()

                if not season:
                    await interaction.response.send_message(
                        "No seasons created yet! An admin can start the league with `/startseason`.",
                        ephemeral=True
                    )
                    return

                season_number, current_round, total_rounds, round_name, status = season

                # Build embed
                if status == 'active':
                    color = discord.Color.green()
                    status_text = "🟢 Active"
                elif status == 'offseason':
                    color = discord.Color.blue()
                    status_text = "🔵 Offseason"
                else:
                    color = discord.Color.grey()
                    status_text = "⚫ Completed"

                embed = discord.Embed(
                    title=f"Season {season_number}",
                    color=color
                )
                embed.add_field(name="Status", value=status_text, inline=True)
                embed.add_field(name="Current", value=round_name, inline=True)

                await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)

class _AnnounceLineupsMissingView(discord.ui.View):
    """Offered by /announcelineups when one or more teams are blocking the
    round (never confirmed, or confirmed but no longer valid) - lets the
    admin force-submit them rather than waiting on every coach. For a team
    whose current live lineup is invalid, auto_fill_lineup (lineup_commands.py)
    is tried first - it backfills empty/injured/suspended starting-18 slots
    from the interchange/reserves per its own priority order - before
    re-validating; only a team still invalid after that auto-fix attempt
    (e.g. the whole roster is exhausted) actually blocks force-submit."""
    def __init__(self, cog, blocking_team_ids, panel_view=None):
        super().__init__(timeout=300)
        self.cog = cog
        self.blocking_team_ids = blocking_team_ids
        self.panel_view = panel_view

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await is_admin_user(interaction)

    @discord.ui.button(label="Force-submit remaining team lineups", style=discord.ButtonStyle.primary)
    async def force_confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        from commands.lineup_commands import validate_lineup, auto_fill_lineup

        await interaction.response.defer(ephemeral=True)
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT current_round FROM seasons WHERE status = 'active' LIMIT 1"
            )
            season_row = await cursor.fetchone()
            if not season_row:
                await interaction.followup.send("❌ No active season!", ephemeral=True)
                return
            current_round = season_row[0]

            still_invalid = []
            for team_id in self.blocking_team_ids:
                cursor = await db.execute("SELECT team_name FROM teams WHERE team_id = ?", (team_id,))
                team_name = (await cursor.fetchone())[0]

                errors, player_ids = await validate_lineup(db, team_id, current_round)
                if errors:
                    await auto_fill_lineup(db, team_id, current_round)
                    errors, player_ids = await validate_lineup(db, team_id, current_round)

                if errors:
                    still_invalid.append(f"**{team_name}**: {'; '.join(errors)}")
                else:
                    await db.execute(
                        "UPDATE teams SET lineup_confirmed = 1 WHERE team_id = ?",
                        (team_id,)
                    )
            await db.commit()

            result = await self.cog._try_announce_lineups(db)

        for item in self.children:
            item.disabled = True

        if self.panel_view is not None:
            await self.panel_view._refresh_panel()

        # Edits THIS message's own content into the final result, rather
        # than leaving the stale "teams have not submitted" text in place
        # and sending the result as a separate followup - the message
        # becomes the single source of truth for what happened, instead of
        # a growing thread of two messages for one action.
        if still_invalid:
            message = "⚠️ Force-submitted valid teams, but these are still blocking the round:\n" + "\n".join(still_invalid)
        elif isinstance(result, str):
            message = result
        else:
            blocking_team_ids, blocking_emojis = result
            message = "❌ Still blocking after force-submit: " + " ".join(blocking_emojis)

        await interaction.edit_original_response(content=message, view=self)


async def setup(bot):
    await bot.add_cog(SeasonCommands(bot))
