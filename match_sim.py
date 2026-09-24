"""AFL match simulation engine.

Pure simulation logic - no discord.py or database dependency. Callers fetch
the two lineups (list of (player_id, name, position, overall_rating, slot)
tuples) and the league's current average OVR, then call simulate_match().

See the "Match Simulation Engine" design doc for the full model rationale.
"""

import math
import random

# ---------------------------------------------------------------------------
# Lineup slot -> strength group
# ---------------------------------------------------------------------------

DEFENSE_SLOTS = {"LBP", "FB", "RBP", "LHB", "CHB", "RHB"}
MIDFIELD_SLOTS = {"LW", "C", "RW", "R", "RR", "RO"}
FORWARD_SLOTS = {"LHF", "CHF", "RHF", "LFP", "FF", "RFP"}

RUCK_SLOT = "R"
RUCK_ELIGIBLE_POSITIONS = {"RUCK", "RUCK-FWD", "RUCK-DEF"}

# Which of the 4 real LINES (defense/midfield/forward/ruck) each on-field
# slot belongs to, for effective_ovr's flat line-fit penalty (see below).
# Ruck is its OWN line here, distinct from midfield - a midfielder in the
# ruck and a ruckman on the wing are both full line mismatches, not a
# same-line reshuffle. (slot_group()/MIDFIELD_SLOTS above still treat R as
# midfield for everything else in this file - team strength, disposals,
# voting - since ruck contests are genuinely won at centre bounces and
# contribute to that line's output; only the POSITIONING penalty below
# treats ruck as its own line.)
SLOT_LINE = {}
for _s in DEFENSE_SLOTS:
    SLOT_LINE[_s] = "defense"
for _s in MIDFIELD_SLOTS:
    SLOT_LINE[_s] = "midfield"
for _s in FORWARD_SLOTS:
    SLOT_LINE[_s] = "forward"
SLOT_LINE[RUCK_SLOT] = "ruck"
del _s

# Which LINE(S) each natural position is genuinely at home in - a pure
# position has exactly one, a hybrid has two. RUCK's only home is the ruck
# line itself (not midfield generally). RUCK-DEF/RUCK-FWD are only ever at
# home in the ruck line or their own non-midfield line - never plain
# midfield - matching how a ruck-eligible hybrid not genuinely rucking was
# already a mismatch anywhere in midfield under the old model.
POSITION_ALLOWED_LINES = {
    "KEY DEF": {"defense"}, "GEN DEF": {"defense"},
    "MID": {"midfield"},
    "KEY FWD": {"forward"}, "GEN FWD": {"forward"},
    "RUCK": {"ruck"},
    "DEF-MID": {"defense", "midfield"},
    "MID-FWD": {"midfield", "forward"},
    "SWINGMAN": {"defense", "forward"},
    "UTILITY": {"defense", "midfield", "forward"},
    "RUCK-DEF": {"ruck", "defense"},
    "RUCK-FWD": {"ruck", "forward"},
}

# Slot typing for effective_ovr's flat key/general-fit penalty: the true
# spine (FB/CHB/FF/CHF) plus the ruck contest are KEY slots; the flanks and
# pure midfield slots are GENERAL slots; the 4 pockets are neutral ground -
# a key-position player and a generalist are equally at home there, so
# pockets never trigger this penalty either way.
KEY_SLOTS = {"FB", "CHB", "FF", "CHF", RUCK_SLOT}
GENERAL_SLOTS = {"LHB", "RHB", "LW", "C", "RW", "RR", "RO", "LHF", "RHF"}

# Which natural positions are KEY-position-type vs GENERAL-position-type,
# for that same penalty. A ruckman is explicitly key-position-type (real
# AFL rucks are genuine specialists, not generalists); MID is explicitly
# general-position-type. SWINGMAN/RUCK-DEF/RUCK-FWD stay key-position-type
# (a swingman or a rucking hybrid parked forward/back is still a tall, not
# a generalist); DEF-MID/MID-FWD/UTILITY stay general-position-type.
KEY_POSITION_TYPES = {"KEY DEF", "KEY FWD", "RUCK", "SWINGMAN", "RUCK-DEF", "RUCK-FWD"}
GENERAL_POSITION_TYPES = {"GEN DEF", "GEN FWD", "MID", "DEF-MID", "MID-FWD", "UTILITY"}

# Kept for backward compatibility with callers still keying off "which
# positions count as a tall in THIS group" (currently only the key-position
# line-overload count in _group_strength, a separate mechanic from the flat
# positioning penalties above - unaffected by this model). Derived from
# KEY_POSITION_TYPES filtered to positions actually eligible for that line;
# RUCK is deliberately excluded from both (the overload count is specific
# to defense/forward tall-stacking, not the ruck line).
KEY_DEF_EQUIVALENT_POSITIONS = {p for p in KEY_POSITION_TYPES if "defense" in POSITION_ALLOWED_LINES[p]}
KEY_FWD_EQUIVALENT_POSITIONS = {p for p in KEY_POSITION_TYPES if "forward" in POSITION_ALLOWED_LINES[p]}

# Slots that count as midfield for disposal purposes regardless of the
# player's natural position - the half-back flank functions as an auxiliary
# midfield role in real AFL.
HIGH_DISPOSAL_SLOTS = {"LHB", "RHB"}


def slot_group(slot):
    """Which strength group (defense/midfield/forward) an ON-FIELD lineup
    slot belongs to. Never called with an interchange slot (INT1-INT5) -
    those have no fixed group of their own; see player_group()/
    _resolve_bench_groups for how a bench player's per-match group is
    actually decided (from their natural position and the rest of their
    team's bench composition, not their slot number)."""
    if slot in DEFENSE_SLOTS:
        return "defense"
    if slot in MIDFIELD_SLOTS:
        return "midfield"
    if slot in FORWARD_SLOTS:
        return "forward"
    raise ValueError(f"Unrecognized on-field lineup slot: {slot!r}")


def best_fairest_group(slot):
    """Same as slot_group(), except the ruck slot (RUCK_SLOT, "R") is its
    own 4th group ("ruck") rather than folding into "midfield" - used only
    by MatchResult.best_and_fairest_votes(). A genuine ruck's stat profile
    (huge hitouts, modest disposals - see BEST_FAIREST_BASELINES) is
    different enough from an on-ball midfielder's (high disposals, low
    hitouts) that normalizing a ruck's game against midfield's own average
    would unfairly bury even a dominant ruck performance under a
    disposal-heavy comparison that was never realistic for that role."""
    if slot == RUCK_SLOT:
        return "ruck"
    return slot_group(slot)




# ---------------------------------------------------------------------------
# Natural position -> group / role weight / disposal tier
# ---------------------------------------------------------------------------

POSITION_ALLOWED_GROUPS = {
    # Pure positions - only ever one home group
    "KEY DEF": {"defense"},
    "GEN DEF": {"defense"},
    "MID": {"midfield"},
    "KEY FWD": {"forward"},
    "GEN FWD": {"forward"},
    "RUCK": {"midfield"},        # ruck contests are won at centre bounces

    # Hybrid positions - genuinely at home in either of their two groups,
    # no penalty for playing either one
    "DEF-MID": {"defense", "midfield"},
    "MID-FWD": {"midfield", "forward"},
    "RUCK-DEF": {"midfield", "defense"},
    "RUCK-FWD": {"midfield", "forward"},
    "SWINGMAN": {"forward", "defense"},           # forward or back, not midfield
    "UTILITY": {"defense", "midfield", "forward"},  # anywhere except ruck (ruck isn't a slot group)
}

# Pure (single-group) natural positions -> their one group. RUCK maps to
# its own "ruck" bucket here (not "midfield" the way POSITION_ALLOWED_GROUPS
# treats it for gameplay eligibility) - _resolve_bench_groups needs to
# distinguish "a true ruck is on this bench" from "a midfielder is on this
# bench" to implement the RUCK-DEF/RUCK-FWD ruck-availability rule below.
PURE_POSITION_GROUP = {
    "KEY DEF": "defense",
    "GEN DEF": "defense",
    "MID": "midfield",
    "KEY FWD": "forward",
    "GEN FWD": "forward",
    "RUCK": "ruck",
}

# Hybrid positions and the (non-ruck) groups they can resolve to via
# _resolve_bench_groups' "thinnest bench group" rule - RUCK-DEF/RUCK-FWD
# are handled as a special ruck-availability case first (see that
# function), only falling through to their listed group here if they
# don't end up playing ruck this match.
HYBRID_BENCH_GROUPS = {
    "DEF-MID": ("defense", "midfield"),
    "MID-FWD": ("midfield", "forward"),
    "RUCK-DEF": ("defense",),
    "RUCK-FWD": ("forward",),
    "SWINGMAN": ("defense", "forward"),
    "UTILITY": ("defense", "midfield", "forward"),
}


def _resolve_bench_groups(bench_players, rng):
    """Assigns each of a team's interchange Players (INT1-INT5) a
    .resolved_group ("defense"/"midfield"/"forward", or "ruck" for a
    player who ends up playing ruck this match) reflecting where they'll
    actually play THIS match - replacing the old approach of judging a
    bench player purely by their own natural position in isolation (or
    worse, by INTERCHANGE_GROUP's arbitrary fixed INT-slot-number mapping,
    which described team-strength balancing only and was never meant to
    stand in for an individual's role).

    The core idea: a hybrid bench player fills whichever of their eligible
    groups is currently THINNEST on the bench (comparing bench-to-bench
    only, not the fixed starting 18) - e.g. a bench of 2 MID + 1 GEN DEF +
    1 DEF-MID should see the DEF-MID play defense, since defense only has
    1 bench player covering it while midfield already has 2. Resolved in
    order from most-constrained to least (pure positions lock in first,
    then narrower hybrids, then UTILITY last, since it has no natural lean
    at all) so a more-specialized hybrid isn't crowded out by a
    less-constrained one claiming a thin group first. Ties (a hybrid's
    eligible groups are equally thin) broken randomly via rng.

    RUCK-DEF/RUCK-FWD are a special case, resolved BEFORE any other
    hybrid: they play ruck (.resolved_group = "ruck") only if the bench
    has no true natural RUCK on it AND no other RUCK-DEF/RUCK-FWD has
    already claimed the ruck spot this match (only one ruck takes the
    field at a time) - otherwise they fall back to their own listed
    non-ruck group (defense for RUCK-DEF, forward for RUCK-FWD), same as
    any other hybrid's fallback."""
    group_counts = {"defense": 0, "midfield": 0, "forward": 0}
    has_true_ruck = any(p.position == "RUCK" for p in bench_players)
    ruck_claimed = False

    def resolution_priority(p):
        # Pure positions (and RUCK, handled separately below) first, then
        # hybrids ordered by how few groups they're eligible for (most
        # constrained first) - RUCK-DEF/RUCK-FWD (1 non-ruck group) and
        # SWINGMAN/DEF-MID/MID-FWD (2 groups) before UTILITY (3 groups).
        if p.position in PURE_POSITION_GROUP:
            return 0
        return len(HYBRID_BENCH_GROUPS.get(p.position, ()))

    for p in sorted(bench_players, key=resolution_priority):
        if p.position in PURE_POSITION_GROUP:
            group = PURE_POSITION_GROUP[p.position]
            if group != "ruck":
                group_counts[group] += 1
            p.resolved_group = group
            continue

        if p.position in ("RUCK-DEF", "RUCK-FWD"):
            if not has_true_ruck and not ruck_claimed:
                p.resolved_group = "ruck"
                ruck_claimed = True
                continue
            group = HYBRID_BENCH_GROUPS[p.position][0]
            group_counts[group] += 1
            p.resolved_group = group
            continue

        eligible = HYBRID_BENCH_GROUPS.get(p.position)
        if eligible is None:
            # Unrecognized position - never expected in practice, but fall
            # back to whichever group is thinnest across the board rather
            # than crashing.
            eligible = ("defense", "midfield", "forward")
        min_count = min(group_counts[g] for g in eligible)
        thinnest = [g for g in eligible if group_counts[g] == min_count]
        group = rng.choice(thinnest) if len(thinnest) > 1 else thinnest[0]
        group_counts[group] += 1
        p.resolved_group = group


def player_group(p):
    """The single group (defense/midfield/forward, or "ruck") a player
    counts as for ALL group-dependent purposes THIS match - stat
    generation (disposal_tier, spoil_weight, mark/tackle tiers,
    shot_role_weight, hitouts), team strength (_group_strength), and
    voting (brownlow_votes/best_and_fairest_votes). The one shared
    resolution point every one of those now reads from, replacing each
    function's own previously-inconsistent interchange handling (disposals
    used to assume every bench player was a midfielder; spoils assumed
    defense; marks/tackles/goals ignored group entirely).

    An on-field player's group is simply their actual slot
    (best_fairest_group, which includes "ruck" for RUCK_SLOT - the 3-way
    slot_group() is just this with "ruck" collapsed to "midfield", used
    where a 3-way split is all that's needed). A bench (interchange)
    player's group is whatever _resolve_bench_groups assigned to
    .resolved_group for this match - set once, right after the Player
    objects for both teams are constructed, before any stat is rolled."""
    if p.slot not in INTERCHANGE_SLOTS:
        return best_fairest_group(p.slot)
    return p.resolved_group

# Two independent flat OVR penalties (not multipliers) that can stack -
# see Player._compute_effective_ovr. Replaced the old 3-multiplier system
# (OUT_OF_POSITION_PENALTY/KEY_POSITION_SLOT_PENALTY/GENERALIST_SPINE_PENALTY)
# with a simpler, additive model:
#   - LINE_MISMATCH_PENALTY fires when the slot's LINE (defense/midfield/
#     forward/ruck - see SLOT_LINE) isn't one of the player's
#     POSITION_ALLOWED_LINES.
#   - KEY_GENERAL_MISMATCH_PENALTY fires when the slot is typed KEY_SLOTS
#     or GENERAL_SLOTS and that doesn't match the player's own
#     KEY_POSITION_TYPES/GENERAL_POSITION_TYPES (pockets are neutral -
#     neither typed, so never trigger this one).
# A player can take neither, either, or both at once (e.g. a GEN FWD at
# CHB: wrong line AND a generalist in a key slot = both penalties).
LINE_MISMATCH_PENALTY = 8
KEY_GENERAL_MISMATCH_PENALTY = 8

# Shot ownership weighting within the forward group - key forwards generate
# far more looks than general forwards at the same OVR (per real Coleman
# Medal leaderboards; tune against actual /scratchmatch results over time).
#
# Every on-field player is technically shot-eligible (defenders and mids do
# occasionally kick a goal in real AFL - a rebound 50, a clearance snap) but
# non-forwards get deliberately low weights so forwards still take the
# overwhelming majority of shots. Defenders are the rarest scorers of all.
# Gaps between these were much wider originally (KEY FWD 1.6 down to MID
# 0.15), which produced a strictly stratified goalkicking ladder - every
# KEY FWD outscoring every GEN FWD outscoring every MID, with no overlap.
# Narrowed so a strong GEN FWD/MID/RUCK can genuinely out-score a weak KEY
# FWD, matching real-world goal-average spreads by position (targets: KEY
# FWD ~1-2.5 typical/3+ elite, GEN FWD ~0.6-2 typical/2.5 top, MID/RUCK
# ~0.1-1 typical/beyond for the elite). SHOT_OVR_EXPONENT (below) is what
# stretches the elite ceiling within each position - these weights set the
# baseline gap between positions, not the individual-to-individual spread.
SHOT_ROLE_WEIGHT = {
    "KEY FWD": 0.8,   # history: 0.75 -> 1.05 -> 0.7 -> briefly 1.02 (each retuned
                       # for a specific accuracy-model change - see git history) ->
                       # reverted to 0.7 after 1.02 was checked across 30 full
                       # seasons (not just a handful) and found to produce
                       # unrealistic 70+ goal Coleman-equivalent seasons (~10% of
                       # seasons) AND completely erase non-KEY-FWD players (e.g.
                       # Harley Reid-type MID-FWD/RUCK-FWD) from the top 10 -> 0.7
                       # alone undershot the "leader 3+" target -> 0.8, found by
                       # sweeping BOTH this weight and SHOT_OVR_EXPONENT together
                       # rather than either alone. Landed on 0.8 (this) +
                       # SHOT_OVR_EXPONENT=7.0 (unchanged) as the best joint fit:
                       # leader avg ~2.87/game (median 2.84, roughly half of
                       # seasons crest above 3.0), non-KEY-FWD present in ~40% of
                       # seasons' top 10 (genuinely "semi-regular"), zero 70+
                       # seasons across a 30-season validation sample. All three
                       # targets (leader magnitude, position diversity, realistic
                       # ceiling) hold simultaneously at this specific pair -
                       # retune BOTH together if either drifts again, not just
                       # this one in isolation.
    "RUCK-FWD": 0.68,
    "MID-FWD": 0.58,
    "GEN FWD": 0.55,
    "SWINGMAN": 0.58,
    "UTILITY": 0.58,
    "RUCK": 0.35,
    "MID": 0.30,
    "DEF-MID": 0.25,
    "RUCK-DEF": 0.12,
    "GEN DEF": 0.06,
    "KEY DEF": 0.04,
}
DEFAULT_SHOT_ROLE_WEIGHT = 0.1  # unrecognized position - treat as roughly midfield-rare

# Forward-leaning hybrids (MID-FWD, SWINGMAN, UTILITY, RUCK-FWD) get their
# elevated SHOT_ROLE_WEIGHT because they're genuinely a forward option when
# actually playing forward - but a MID-FWD parked in midfield or a UTILITY
# parked in defense isn't creating forward opportunities from there, so their
# weight should reflect a generic player of whatever group they're actually
# in, not their own forward-tier number. Same principle as spoil_weight and
# the effective_OVR out-of-position penalty.
GENERIC_SHOT_WEIGHT_BY_GROUP = {
    "midfield": SHOT_ROLE_WEIGHT["MID"],
    "defense": SHOT_ROLE_WEIGHT["GEN DEF"],
}

DISPOSAL_TIER_WEIGHT = {
    "high": 1.35,  # MID, DEF-MID, MID-FWD, or slotted at LHB/RHB
    "mid": 1.05,   # RUCK, RUCK-FWD, RUCK-DEF
    "low": 0.85,   # KEY DEF/FWD, GEN DEF/FWD elsewhere on the ground
}

# UTILITY is included: a utility genuinely playing midfield is treated as a
# midfielder there, same as DEF-MID/MID-FWD. Its flexibility is already
# expressed by taking no out-of-position penalty across three lines (see
# POSITION_ALLOWED_LINES) - it shouldn't ALSO be worse than a specialist at
# the job it's actually doing. Note disposal_tier() only reaches this set
# for a player whose resolved group IS midfield/ruck this match, so a
# UTILITY parked in defense or attack still gets the low tier.
DISPOSAL_HIGH_TIER_POSITIONS = {"MID", "DEF-MID", "MID-FWD", "UTILITY"}
DISPOSAL_MID_TIER_POSITIONS = {"RUCK", "RUCK-FWD", "RUCK-DEF"}


def disposal_tier(player):
    """Disposal tier for a player - driven by where they're actually playing
    this match (player_group(player) - their own slot if on-field, or
    _resolve_bench_groups' per-match assignment if on the interchange), not
    just their natural position label. A MID-FWD genuinely playing midfield
    this match gets high-tier disposals; the same MID-FWD resolved to
    forward this match doesn't - they're not getting the ball like a
    midfielder just because that's one of their listed roles. LHB/RHB are a
    special case (auxiliary midfield role) checked first. "ruck" (a player
    genuinely playing ruck, on-field or via bench resolution) counts as
    midfield here - ruck contests happen at centre bounces, midfield
    territory - so DISPOSAL_MID_TIER_POSITIONS can still apply its own,
    lower, tier within that."""
    if player.slot in HIGH_DISPOSAL_SLOTS:
        return "high"

    group = player_group(player)
    if group not in ("midfield", "ruck"):
        # Playing outside the midfield this match - natural position doesn't
        # grant a disposal bump they're not actually on the ground to earn
        return "low"

    position = player.position
    if position in DISPOSAL_HIGH_TIER_POSITIONS:
        return "high"
    if position in DISPOSAL_MID_TIER_POSITIONS:
        return "mid"
    return "low"


# ---------------------------------------------------------------------------
# Marks / tackles / spoils - same shape as disposals (tier weight * base *
# OVR sensitivity), but each stat has its own position skew. Tier is driven
# by the player's ACTUAL on-field group (slot_group), same reasoning as
# disposal_tier: a key forward parked in defense this week doesn't mark like
# a key forward, and vice versa. Interchange players are judged by natural
# position, same carve-out as disposals, for the same reason - a bench key
# forward is still a key forward regardless of which INT number they get.
# ---------------------------------------------------------------------------

# Real AFL marking is fairly evenly spread across positions - the gap
# between the best and worst marking positions is much smaller than shot
# volume or spoils, so this is a direct per-position weight (as a fraction
# of BASE_MARKS) rather than a 3-tier high/mid/low split. Targets (typical
# per-game range, from real stats): KEY DEF ~4-7, GEN DEF ~3-7, KEY FWD
# ~3-6, MID/GEN FWD/RUCK ~2-5.
MARK_WEIGHT = {
    "KEY DEF": 1.10,
    "GEN DEF": 1.00,
    "RUCK-DEF": 0.85,
    "SWINGMAN": 0.85,
    "KEY FWD": 0.90,
    "RUCK-FWD": 0.80,
    "DEF-MID": 0.75,
    "MID-FWD": 0.75,
    "UTILITY": 0.75,
    "GEN FWD": 0.70,
    "MID": 0.70,
    "RUCK": 0.70,
}
DEFAULT_MARK_WEIGHT = 0.70

TACKLE_TIER_POSITIONS = {
    "high": {"MID", "DEF-MID", "MID-FWD", "GEN FWD"},  # pressure midfielders and small forwards
    "mid": {"RUCK", "RUCK-FWD", "RUCK-DEF", "GEN DEF", "SWINGMAN", "UTILITY"},
    # KEY FWD/KEY DEF -> low: tall targets tackle the least
}

# A UTILITY genuinely playing MIDFIELD is treated as a midfielder for
# tackles, matching how DISPOSAL_HIGH_TIER_POSITIONS now treats it - when it
# IS the midfielder this match, it does a midfielder's job.
#
# Handled as a separate promotion rather than adding UTILITY to "high"
# above, because _position_group_tier only ever DEMOTES a player found
# outside POSITION_ALLOWED_GROUPS - and UTILITY allows all three lines, so
# it would never be demoted and would keep midfield-grade tackles in defense
# and attack too. This keeps the promotion where it belongs.
TACKLE_MIDFIELD_ONLY_PROMOTIONS = {"UTILITY"}
TACKLE_TIER_WEIGHT = {"high": 1.0, "mid": 0.7, "low": 0.4}

SPOIL_TIER_POSITIONS = {
    # SWINGMAN genuinely playing defense counts as a full defender here (see
    # spoil_weight) - they're doing the job, not just eligible to. UTILITY
    # stays in "mid" even when genuinely in defense: a broader def/mid/fwd
    # generalist is less specialized at any one skill than a def/fwd swingman.
    "high": {"KEY DEF"},
    "mid": {"GEN DEF", "RUCK-DEF", "UTILITY"},
    # everything outside the backline -> low: spoiling is an almost exclusively defensive act
}
SPOIL_TIER_WEIGHT = {"high": 1.0, "mid": 0.5, "low": 0.05}

# Non-defenders still spoil occasionally (a marking contest spoiled forward
# of centre, a desperate spoil at a stoppage) - real per-game ranges vary by
# position even outside the backline, so this is a direct per-position
# weight (as a fraction of BASE_SPOILS) rather than a single flat "low"
# bucket. Applies to a player's NATURAL position when they're not currently
# playing defense - see spoil_weight().
NON_DEFENDER_SPOIL_WEIGHT = {
    "KEY FWD": 0.333,    # targets ~1.0/game
    "RUCK-FWD": 0.30,    # ~0.9/game
    "MID-FWD": 0.20,     # ~0.6/game
    "DEF-MID": 0.20,     # ~0.6/game
    "MID": 0.15,         # ~0.45/game
    "RUCK": 0.15,        # ~0.45/game
    "GEN FWD": 0.10,     # ~0.3/game
    "SWINGMAN": 0.20,    # playing forward this week - between MID-FWD and KEY FWD
    "UTILITY": 0.15,     # generalist, treated like MID when not in defense
}
DEFAULT_NON_DEFENDER_SPOIL_WEIGHT = 0.15


def _position_group_tier(player, tier_positions):
    """Shared helper for marks/tackles: which tier a player falls into for a
    given stat, based on their natural position - but demoted one tier if
    they're out of position this match (their actual group this match,
    player_group(player) - their own slot if on-field, or
    _resolve_bench_groups' per-match assignment if on the interchange),
    since a key defender playing forward this week shouldn't mark/tackle
    like a key defender just because that's their listed role. A bench
    player is judged exactly the same way now that they have a real
    resolved group for this match, rather than being exempted from the
    demotion entirely.

    Not used for spoils - see spoil_tier() below, which needs a stricter
    rule than "not flagged out of position" can give."""
    position = player.position
    if position in tier_positions["high"]:
        tier = "high"
    elif position in tier_positions["mid"]:
        tier = "mid"
    else:
        tier = "low"

    group = player_group(player)
    group = "midfield" if group == "ruck" else group

    # Promoted to the midfield tier, but only while actually playing there
    # (see TACKLE_MIDFIELD_ONLY_PROMOTIONS).
    if (tier_positions is TACKLE_TIER_POSITIONS
            and position in TACKLE_MIDFIELD_ONLY_PROMOTIONS
            and group == "midfield"):
        return "high"

    if tier == "low":
        return tier

    allowed_groups = POSITION_ALLOWED_GROUPS.get(position)
    if allowed_groups is not None and group not in allowed_groups:
        return "mid" if tier == "high" else "low"
    return tier


def spoil_weight(player):
    """Spoil weight (fraction of BASE_SPOILS) for a player - which matters
    more, WHERE they're playing this match (their current on-field slot,
    or _resolve_bench_groups' per-match assignment if on the interchange)
    or WHO they fundamentally are (natural position)?

    Genuinely playing defense this week always wins: a key forward shifted
    back to CHB is doing defensive work now and spoils close to a defender's
    rate, not a forward's - same principle as the earlier Van Rooyen fix,
    just applied in the other direction too. Playing outside defense falls
    back to the player's NATURAL position's typical rate (NON_DEFENDER_SPOIL_WEIGHT),
    since "not in defense" doesn't further differentiate by which other slot
    they're in - only by who they are.

    A SWINGMAN genuinely playing (or resolved to) defense is treated as a
    full KEY DEF-equivalent - they're actually doing the job this week, not
    just generically capable of it. UTILITY stays at the reduced "mid" rate
    even then, per SPOIL_TIER_POSITIONS."""
    position = player.position
    group = player_group(player)
    if group == "defense":
        if position in SPOIL_TIER_POSITIONS["high"] or position == "SWINGMAN":
            return SPOIL_TIER_WEIGHT["high"]
        if position in SPOIL_TIER_POSITIONS["mid"]:
            return SPOIL_TIER_WEIGHT["mid"]
        return SPOIL_TIER_WEIGHT["low"]
    return NON_DEFENDER_SPOIL_WEIGHT.get(position, DEFAULT_NON_DEFENDER_SPOIL_WEIGHT)


# ---------------------------------------------------------------------------
# Hitouts - ruck-exclusive. Real AFL: hitouts are credited at boundary/ball-up
# contests that are overwhelmingly ruck-vs-ruck, and many non-ruck players go
# entire careers without registering one - so a non-ruck player simply never
# registers a hitout in this model, full stop, rather than a rare chance.
# ---------------------------------------------------------------------------

HITOUT_RUCK_POSITIONS = {"RUCK", "RUCK-FWD", "RUCK-DEF"}

BASE_HITOUTS = 25  # league-average ruck hitouts per match - real rucks range ~21-34
HITOUT_OVR_SENSITIVITY_ABOVE_AVG = 1.2
HITOUT_OVR_SENSITIVITY_BELOW_AVG = 0.65


# ---------------------------------------------------------------------------
# Simulation constants (tunable)
# ---------------------------------------------------------------------------

BASE_SHOTS_PER_TEAM = 27       # league-average total scoring shots per team per match -
                                # real league averages ~27 shots/team/game (~13 goals, ~11.5
                                # behinds, ~2.5 complete misses)
ON_TARGET_CHANCE = 0.907       # chance a shot scores at all (goal or behind), vs a complete miss -
                                # derived from the 27/13/11.5 real split: (13+11.5)/27 =~ 90.7% -
                                # real shots miss the target far less often than earlier assumed

# Goal accuracy varies by GROUP (forward vs everyone else), not a single
# flat league number - real forwards convert shots to goals more often than
# other positions. Both constants below are TEAM-LEVEL rates "of ON-TARGET
# shots" (matching _goal_chance's existing unit), derived so that, combined
# with ON_TARGET_CHANCE and the forward line's ~57% share of total shots,
# the WHOLE LEAGUE'S goal rate of all shots still lands on the real ~48.1%
# average (13/27) - the SCOREBOARD stat, i.e. team goals actually kicked.
#
# These are NOT the individual PLAYER accuracy numbers you'd see reported
# (goals / that player's own credited shots) - see RUSHED_BEHIND_CHANCE
# below for why individual accuracy reads meaningfully higher than these
# team-level rates. The specific 0.554/0.50 split (rather than the more
# extreme 0.634/0.363 an unconstrained derivation gives) was chosen so that,
# AFTER the rushed-behind adjustment, individual accuracy lands close to
# real reference points (~55% forward / ~50-55% other, individually) while
# the team-level weighted average is held exactly on the real 48.1% figure -
# a genuine tradeoff: pushing "other" up further would keep narrowing the
# forward/other gap, since the two move in lockstep against each other
# under the fixed weighted-average constraint (see the git history / prior
# session notes for the full derivation and the wider tradeoff table).
BASE_GOAL_ACCURACY_FORWARD = 0.554
BASE_GOAL_ACCURACY_OTHER = 0.50
ACCURACY_OVR_SENSITIVITY = 0.0015  # small per-OVR-point nudge to accuracy (kept narrow deliberately)

# Rushed behinds - a defender deliberately concedes a behind rather than
# allow a near-certain goal through. Real AFL averages ~2.2 rushed behinds
# per team per game out of ~11.5 total behinds (~19%). Doesn't touch team
# SCORING at all (see TeamMatchResult.rushed_behinds - the team's goals/
# behinds/score stay exactly as BASE_GOAL_ACCURACY_FORWARD/_OTHER already
# produce) - only whether the individual shooter personally gets credit for
# what would have been their behind, which is what pushes individual
# accuracy above the team-level 48.1% figure (see above).
RUSHED_BEHIND_CHANCE = 0.19

# Disposals now work like goals (see _disposal_count / _simulate_disposals):
# each team gets a single team-total disposal count driven by relative team
# strength (same attacking/suppression diff used for shots, so a team's
# disposal edge naturally correlates with how comfortably it's winning -
# close-strength teams land near the same total, a lopsided matchup skews
# harder), then that total is split across the 23 players by disposal_tier
# weight and individual effective_OVR, same pattern as SHOT_ROLE_WEIGHT.
#
# Real AFL: league-average team disposals ~360/game, highest-averaging teams
# ~380-390, lowest ~320-340.
BASE_DISPOSALS_PER_TEAM = 360
DISPOSAL_DIFF_SENSITIVITY = 7.0   # tuned so team-strength gaps produce the
                                    # real ~320-390 team range without needing
                                    # per-team variance to do all the work -
                                    # real teams' OVR gaps are much narrower
                                    # than the extreme best-vs-worst case, so
                                    # this needs to be considerably steeper
                                    # than SHOT_DIFF_SENSITIVITY to still
                                    # separate the league across that range

# Individual OVR still matters WITHIN a team's total - a tier's players don't
# all get an equal slice, an elite player in a tier out-touches a weak one in
# the same tier. This exponent stretches that spread the same way
# SHOT_OVR_EXPONENT does for shot ownership, but far gentler (disposals are
# nowhere near as concentrated as goals - even a weak player in a high-tier
# role gets a meaningful, non-trivial share).
#
# Tuned against SEASON-LONG averages, not a single match's expected value -
# a real AFL season's disposal leaderboard has the top 2-5 players around
# 30-33, and a season-topping average past 35 essentially never happens.
DISPOSAL_OVR_EXPONENT = 4.6

# Per-player noise on top of the proportional split (see _simulate_disposals)
# - without this, a player's SHARE of the team total is a fixed ratio every
# match, so only the team total itself would vary and every individual
# player's game-to-game range would look unrealistically narrow (e.g. a
# 99-OVR mid landing on 29-31 disposals in literally every match).
DISPOSAL_INDIVIDUAL_VARIANCE = 0.25  # produces a realistic per-player game-to-game
                                       # spread - an elite (99 OVR) mid should have a
                                       # genuine shot at a 40+ disposal game occasionally
                                       # (~1 in 16 games at this value) without turning
                                       # into a common occurrence, and without making
                                       # very low games (sub-15) too frequent either

# Marks/tackles/spoils all follow the same shape as disposals: a base rate
# for a league-average-OVR player, scaled by the stat's own per-position
# weight (MARK_WEIGHT / TACKLE_TIER_WEIGHT / SPOIL_TIER_WEIGHT+NON_DEFENDER_
# SPOIL_WEIGHT), then an OVR nudge (steeper above average than below, same
# reasoning as disposals). Marks are fairly even across positions in real
# AFL (targets ~2-5 typical, KEY DEF up to ~4-7, GEN DEF ~3-7, KEY FWD ~3-6).
# Tackles target elite ~7-8 / average ~4 / weak ~1-2 for the "high" tier.
# Spoils are concentrated in defense (KEY DEF elite ~6-7 / average ~3) but
# non-defenders still spoil occasionally - see NON_DEFENDER_SPOIL_WEIGHT.
BASE_MARKS = 5
MARK_OVR_SENSITIVITY_ABOVE_AVG = 0.44
MARK_OVR_SENSITIVITY_BELOW_AVG = 0.22

BASE_TACKLES = 4
TACKLE_OVR_SENSITIVITY_ABOVE_AVG = 0.44
TACKLE_OVR_SENSITIVITY_BELOW_AVG = 0.16

BASE_SPOILS = 3
SPOIL_OVR_SENSITIVITY_ABOVE_AVG = 0.44
SPOIL_OVR_SENSITIVITY_BELOW_AVG = 0.16

DEFAULT_VARIANCE = 0.6         # match_sim_variance setting default - dialed down
                                # from 1.0 to reduce match-level randomness, which
                                # was leaving mid-table teams' season-long ladder
                                # finishes too bunched together despite real OVR
                                # gaps between them. Also sharpens the best-vs-
                                # worst matchup beyond its original ~95% target
                                # (now closer to ~98-99%) - an accepted tradeoff,
                                # not a separate deliberate retune of that number.

# ---------------------------------------------------------------------------
# Live match feed - event timeline, quarter lengths, in-match injuries.
# Additive to the batch engine above: simulate_match() and its callers
# (sim_batch.py, sim_season.py, the non-live /scratchmatch) are untouched.
# See the "Live Match Feed" design doc for the full model rationale.
# ---------------------------------------------------------------------------

# ELAPSED quarter length, not playing time. An AFL quarter is nominally 20
# minutes of PLAYING time, but the clock stops at every stoppage and the
# time is added back on ("time on"), so a quarter actually runs ~25-35
# minutes wall-clock. These are those elapsed figures - the ~8 minutes over
# the nominal 20 IS the time on.
QUARTER_LENGTH_MIN = 25
QUARTER_LENGTH_MAX = 35
QUARTER_LENGTH_MODE = 28  # triangular distribution - most quarters land near 27-29


def quarter_length_minutes(rng):
    return rng.triangular(QUARTER_LENGTH_MIN, QUARTER_LENGTH_MAX, QUARTER_LENGTH_MODE)


# Two events don't get placed independently close enough to look like they
# happened in the same instant on the in-match clock (e.g. two goals 6
# simulated-seconds apart) - real AFL always has at least a stoppage/reset
# between scoring plays. 15 real-game-seconds, expressed in the same
# fractional-minutes unit everything else here uses.
MINIMUM_EVENT_GAP_MINUTES = 15 / 60


def _enforce_minimum_event_gap(events, quarter_lengths):
    """Walks the already-sorted, already-merged event list (both teams'
    goals/behinds plus injuries) and nudges events apart so every
    consecutive pair within the same quarter is at least
    MINIMUM_EVENT_GAP_MINUTES apart - each quarter is its own independent
    clock, so this never looks across a quarter boundary. The same minimum
    also applies between the START of the quarter (minute 0, i.e. the
    quarter-start message) and the first event in it - a goal shouldn't be
    able to land in the opening few seconds either.

    After-siren events (see AFTER_SIREN_SHOT_CHANCE) are deliberately placed
    PAST the quarter's own length and are excluded entirely - clamping them
    back to quarter_length like a normal event would destroy the whole point
    of them landing after the siren.

    Three passes: a forward pass pushes each event later if it's too close
    to the one before it - or to quarter-start, for the first event -
    (clamped so it can't spill past the quarter's own end); a backward pass
    then pulls events earlier if clamping left two or more stacked at the
    same instant (clamped so the first event can't go below
    MINIMUM_EVENT_GAP_MINUTES, honoring the same start-of-quarter floor) -
    without that second pass, a quarter with enough late-clustered events
    can have several pile up at the exact same clamped end-of-quarter
    moment."""
    by_quarter = {}
    for event in events:
        if event.after_siren:
            continue
        by_quarter.setdefault(event.quarter, []).append(event)

    for quarter, quarter_events in by_quarter.items():
        if not quarter_events:
            continue
        quarter_length = quarter_lengths[quarter - 1]

        if quarter_events[0].minute < MINIMUM_EVENT_GAP_MINUTES:
            quarter_events[0].minute = min(MINIMUM_EVENT_GAP_MINUTES, quarter_length)
        for i in range(1, len(quarter_events)):
            prev_minute = quarter_events[i - 1].minute
            if quarter_events[i].minute < prev_minute + MINIMUM_EVENT_GAP_MINUTES:
                quarter_events[i].minute = min(prev_minute + MINIMUM_EVENT_GAP_MINUTES, quarter_length)

        for i in range(len(quarter_events) - 2, -1, -1):
            next_minute = quarter_events[i + 1].minute
            if quarter_events[i].minute > next_minute - MINIMUM_EVENT_GAP_MINUTES:
                quarter_events[i].minute = max(next_minute - MINIMUM_EVENT_GAP_MINUTES, 0.0)
        if quarter_events[0].minute < MINIMUM_EVENT_GAP_MINUTES:
            quarter_events[0].minute = min(MINIMUM_EVENT_GAP_MINUTES, quarter_length)

        for event in quarter_events:
            event.match_minute = sum(quarter_lengths[:quarter - 1]) + event.minute


def _enforce_minimum_event_gap_flat(events, period_length):
    """Same two-pass forward/backward gap enforcement as
    _enforce_minimum_event_gap, but for a single flat period (extra-time
    half) rather than a list of quarters - there's no quarter_lengths
    offset to add, and after-siren events (already placed past
    period_length) are excluded the same way. A very short period
    (~4-5 minutes) combined with the same MINIMUM_EVENT_GAP_MINUTES floor
    caps how many events can realistically fit without clamping - fine in
    practice since a scaled-down shot count over that span is naturally low
    (around 4-5 shots per team, of which only some actually score)."""
    normal_events = [e for e in events if not e.after_siren]
    if not normal_events:
        return

    if normal_events[0].minute < MINIMUM_EVENT_GAP_MINUTES:
        normal_events[0].minute = min(MINIMUM_EVENT_GAP_MINUTES, period_length)
    for i in range(1, len(normal_events)):
        prev_minute = normal_events[i - 1].minute
        if normal_events[i].minute < prev_minute + MINIMUM_EVENT_GAP_MINUTES:
            normal_events[i].minute = min(prev_minute + MINIMUM_EVENT_GAP_MINUTES, period_length)

    for i in range(len(normal_events) - 2, -1, -1):
        next_minute = normal_events[i + 1].minute
        if normal_events[i].minute > next_minute - MINIMUM_EVENT_GAP_MINUTES:
            normal_events[i].minute = max(next_minute - MINIMUM_EVENT_GAP_MINUTES, 0.0)
    if normal_events[0].minute < MINIMUM_EVENT_GAP_MINUTES:
        normal_events[0].minute = min(MINIMUM_EVENT_GAP_MINUTES, period_length)

    for event in normal_events:
        event.match_minute = event.minute


# Category shown live in the feed -> plausible specific diagnoses (revealed
# later via the team injury update) and a rough recovery-weeks range per
# diagnosis. Flat/position-independent selection - same reasoning as
# INJURY_CHANCE_PER_PLAYER below.
INJURY_DIAGNOSES = {
    "Knee": [("PCL sprain", (2, 4)), ("MCL sprain", (2, 5)), ("ACL tear", (30, 52)), ("meniscus tear", (3, 6))],
    "Hamstring": [("hamstring tightness", (0, 2)), ("hamstring strain", (2, 5)), ("hamstring tear", (5, 9))],
    "Shoulder": [("AC joint sprain", (1, 3)), ("shoulder subluxation", (2, 4)), ("dislocated shoulder", (6, 10))],
    "Concussion": [("concussion", (1, 2))],
    "Ankle": [("ankle sprain", (1, 3)), ("high ankle sprain", (4, 7)), ("syndesmosis injury", (6, 10))],
    "Calf": [("calf tightness", (0, 2)), ("calf strain", (2, 4))],
    "Groin": [("groin strain", (2, 4)), ("osteitis pubis", (4, 8))],
    "Ribs": [("rib bruising", (0, 2)), ("fractured rib", (3, 5))],
    "Leg": [("fractured fibula", (6, 12)), ("broken leg", (12, 16))],
    "Quad": [("quad tightness", (0, 2)), ("quad strain", (3, 5)), ("quad tear", (8, 12))],
}
INJURY_CATEGORIES = list(INJURY_DIAGNOSES.keys())

# ~0.57 injuries per match on average (a second ~25% cut, ~44% down total
# from the original 1/match), spread across roughly 46 player-appearances
# (23 per team, on-ground and bench combined) - flat, position-independent,
# same reasoning as the eventual real injury system.
INJURY_CHANCE_PER_PLAYER = 1.0 / 81  # ~1.2%


def _generate_injury(player, rng):
    """Rolls a full injury (category + specific diagnosis + recovery weeks)
    for one player. The caller decides what to actually reveal - the live
    feed only ever shows MatchEvent.injury_category."""
    category = rng.choice(INJURY_CATEGORIES)
    diagnosis, (min_weeks, max_weeks) = rng.choice(INJURY_DIAGNOSES[category])
    recovery_weeks = rng.randint(min_weeks, max_weeks)
    return category, diagnosis, recovery_weeks


# Match Review Panel report reasons -> plausible specific charges (revealed
# later once the panel hands down its sanction, same withhold-the-detail
# pattern as INJURY_DIAGNOSES) and a rough suspension-games range per
# charge. Flat/position-independent selection.
REPORT_REASONS = {
    "Striking": [("striking - low impact", (0, 1)), ("striking - medium impact", (2, 3)), ("striking - high impact", (4, 6))],
    "Rough Conduct": [("rough conduct - low impact", (0, 2)), ("rough conduct - high impact", (3, 4))],
    "Dangerous Tackle": [("dangerous tackle - low impact", (0, 2)), ("dangerous tackle - high impact", (3, 5))],
    "High Contact": [("high contact - low impact", (0, 1)), ("high contact - medium impact", (2, 3))],
    "Unsportsmanlike Conduct": [("unsportsmanlike conduct", (1, 2))],
    "Umpire Contact": [("umpire contact", (1, 2))],
}
REPORT_CATEGORIES = list(REPORT_REASONS.keys())

# ~1-2 suspensions per week (league-wide) is the target - injuries land at
# roughly 0.57/match (INJURY_CHANCE_PER_PLAYER above, ~46 rolls/match), so
# reports need to be roughly 6x rarer per-player-roll to keep the same
# suspension:injury ratio, now ~0.85 league-wide per round at a typical
# 10-matches/round league (46 rolls x 10 matches x 1/545 =~ 0.85) - ~44%
# down total from the original 1/307, matching the injury reduction.
REPORT_CHANCE_PER_PLAYER = 1.0 / 545  # ~0.18%


def _generate_report(player, rng):
    """Rolls a full Match Review Panel report (category + specific charge +
    suspension games) for one player. The caller decides what to actually
    reveal - the live feed only ever shows MatchEvent.report_category."""
    category = rng.choice(REPORT_CATEGORIES)
    charge, (min_games, max_games) = rng.choice(REPORT_REASONS[category])
    suspension_games = rng.randint(min_games, max_games)
    return category, charge, suspension_games


class MatchEvent:
    """One timestamped moment in a live match feed - a goal, behind,
    injury, or report. Exists purely in memory for the duration of one
    command; never persisted (see the Live Match Feed design doc's data
    model section)."""

    __slots__ = (
        "kind", "quarter", "minute", "match_minute", "team_name", "player",
        "injury_category", "injury_diagnosis", "injury_recovery_weeks",
        "report_category", "report_charge", "report_suspension_games",
        "after_siren", "siren_beater_winner", "rushed",
    )

    def __init__(self, kind, quarter, minute, match_minute, team_name, player,
                 injury_category=None, injury_diagnosis=None, injury_recovery_weeks=None,
                 report_category=None, report_charge=None, report_suspension_games=None,
                 after_siren=False, siren_beater_winner=False, rushed=False):
        self.kind = kind  # "goal" / "behind" / "injury" / "report"
        self.quarter = quarter  # 1-4
        self.minute = minute  # position within that quarter, 0..quarter_length
        self.match_minute = match_minute  # absolute position across the whole match - what events sort/pace by
        self.team_name = team_name
        self.player = player  # still set even when rushed=True (the shooter who took the shot) - display layer decides whether to show it
        self.injury_category = injury_category
        self.injury_diagnosis = injury_diagnosis
        self.injury_recovery_weeks = injury_recovery_weeks
        self.report_category = report_category
        self.report_charge = report_charge
        self.report_suspension_games = report_suspension_games
        self.after_siren = after_siren  # this shot landed right after its quarter's siren
        self.siren_beater_winner = siren_beater_winner  # Q4 after-siren GOAL that won the match
        self.rushed = rushed  # a "behind" the defense conceded rather than the shooter's own accuracy - see RUSHED_BEHIND_CHANCE

INTERCHANGE_SLOTS = {"INT1", "INT2", "INT3", "INT4", "INT5"}
# Interchange players get reduced pull on their group's strength - coaches
# generally start their best 18 and use the bench for rotation/injury cover,
# so a bench player shouldn't count as heavily as a starter of the same OVR.
INTERCHANGE_STRENGTH_WEIGHT = 0.6


class Player:
    """A single player as seen by the simulator."""

    __slots__ = ("player_id", "name", "position", "overall_rating", "slot", "effective_ovr", "strength_weight", "resolved_group")

    def __init__(self, player_id, name, position, overall_rating, slot):
        self.player_id = player_id
        self.name = name
        self.position = position
        self.overall_rating = overall_rating
        self.slot = slot
        self.effective_ovr = self._compute_effective_ovr()
        self.strength_weight = INTERCHANGE_STRENGTH_WEIGHT if slot in INTERCHANGE_SLOTS else 1.0
        # Set by _resolve_bench_groups (called once per team, right after
        # that team's 5 interchange Players are all constructed) for a
        # bench player only - see player_group(). Stays None for an
        # on-field player, whose group is always just their own slot.
        self.resolved_group = None

    def _compute_effective_ovr(self):
        if self.slot in INTERCHANGE_SLOTS:
            # Called from __init__, before _resolve_bench_groups has run
            # (that needs every one of a team's 5 interchange Players
            # already constructed, to compare bench composition) - so
            # .resolved_group isn't set yet at this point, and never can
            # be for this calculation. Not a real gap though: a bench
            # player is never actually out of position under this system
            # (see POSITION_ALLOWED_LINES/HYBRID_BENCH_GROUPS - every group
            # _resolve_bench_groups can assign a hybrid to is already one
            # of their genuinely allowed lines), so no penalty is ever the
            # correct answer here regardless.
            return self.overall_rating

        allowed_lines = POSITION_ALLOWED_LINES.get(self.position)
        if allowed_lines is None:
            # unrecognized position - never penalized
            return self.overall_rating

        penalty = 0
        # Flat penalty #1: the slot's LINE isn't one of the player's
        # allowed lines (ruck is its own line here - see SLOT_LINE).
        if SLOT_LINE[self.slot] not in allowed_lines:
            penalty += LINE_MISMATCH_PENALTY
        # Flat penalty #2: the slot is KEY- or GENERAL-typed and that
        # doesn't match the player's own type (pockets are neutral -
        # in neither KEY_SLOTS nor GENERAL_SLOTS, so never trigger this).
        # Independent of the line check above - both can fire together
        # (e.g. a GEN FWD at CHB: wrong line AND a generalist in a key slot).
        if self.slot in KEY_SLOTS and self.position in GENERAL_POSITION_TYPES:
            penalty += KEY_GENERAL_MISMATCH_PENALTY
        elif self.slot in GENERAL_SLOTS and self.position in KEY_POSITION_TYPES:
            penalty += KEY_GENERAL_MISMATCH_PENALTY

        return self.overall_rating - penalty


# How much random noise MatchResult.brownlow_votes() applies to each
# player's proxy score before ranking, as a multiplier's std deviation.
# Calibrated empirically against realistic full-season simulations (18
# teams, 23 rounds, real OVR-driven match_sim.simulate_match, not a toy
# model) rather than guessed: even a modest, consistent per-match talent
# edge compounds hugely over 23 rounds (law of large numbers), so 0.25-0.45
# barely dented season-medallist tallies in that testing (still landing
# 40-56 votes). 0.8 was the point where repeated full-season runs actually
# landed in/near the real medal's usual high-20s/low-30s range on average
# (tested across 10 seeded seasons: 27-52 range, ~37 average) without
# flattening the leaderboard entirely the way higher values (1.0+) started
# to - a genuinely dominant performance should still often win a given
# week's votes, just not compound into a runaway season total. See
# brownlow_votes()'s docstring for how this combines with
# BROWNLOW_POSITION_VOTE_WEIGHT/BROWNLOW_WINNING_TEAM_VOTE_WEIGHT below.
BROWNLOW_VOTE_NOISE_STDDEV = 0.8

# Extra per-slot-group multiplier applied on top of brownlow_votes_proxy,
# BEFORE noise. Empirically calibrated against realistic full-season sims
# (18 teams, 23 rounds) rather than guessed, measuring how many of the
# season's top 10 vote-getters end up midfielders - the real medal's
# history is heavily midfield-dominated (most years' top 10 skews strongly
# midfield, not just the medallist). With BROWNLOW_VOTE_NOISE_STDDEV=0.8's
# heavy noise, NO boost at all (1.0 across the board) actually under-
# represents midfielders (~5/10 in testing - noise alone washes out too
# much of the proxy formula's own disposal-heavy bias). A boost above 1.0
# for defense/forward (tried up to 1.8) makes this WORSE, not better -
# wipes midfielders out almost entirely (~0.6/10 at 1.8). A mild PENALTY
# below 1.0 for defense/forward is what actually restores realistic
# midfield dominance - 0.85 landed at ~7.6/10 midfielders in the top 10
# across repeated season sims, without fully shutting out a standout
# defender/forward performance the way a much lower value (0.5-0.7,
# ~9.5-10/10) would.
BROWNLOW_POSITION_VOTE_WEIGHT = {
    "midfield": 1.0,
    "defense": 0.85,
    "forward": 0.85,
}

# Extra multiplier for every player on the match's WINNING team (a draw
# applies no boost to either side) - real Brownlow voting does skew toward
# best-on-ground performances in winning sides, and this also helps break
# up season-long concentration further: the same players' teams don't win
# every single week, so this rotates who's even in the running for votes
# more than a purely individual-stats model would.
#
# Lowered from 1.5 to 1.2. Unlike the noise, this is a DETERMINISTIC
# handicap applied before any randomness, so at 1.5 it could erase a large
# stat-sheet gap on its own: a 63-proxy game on the losing side ranked
# behind a 43-proxy game on the winning side before a die was rolled, which
# is how a 45-disposal/2-goal/9-tackle best-on-ground could routinely poll
# nothing after a narrow loss.
#
# Measured over 60 simulated seasons, dropping to 1.2 leaves season
# balance essentially untouched (medallist ~24.8 -> ~25.6 votes, top-10
# spread 9.1 -> 9.4, similar number of players polling) while lifting how
# often a dominant game - best on ground by 1.5x or more - takes the 3
# votes, from ~39% to ~44%. Reshaping the noise instead was tried first
# and was strictly worse: at matched season balance it barely moved
# standout games at all.
#
# Note this boost is a step, not a slope - a 1-point win applies the same
# multiplier as a 100-point win.
BROWNLOW_WINNING_TEAM_VOTE_WEIGHT = 1.2


# Club best & fairest is a DIFFERENT award from the Brownlow - voted
# per-TEAM (each club hands out its own 5-4-3-2-1 among its own 23 players
# every match, not one combined ranking across both teams), and
# deliberately POSITION-NEUTRAL rather than biased toward midfielders - a
# genuinely dominant defensive or forward game should compete on equal
# footing with a genuine midfield haul, not be structurally disadvantaged
# the way raw disposal-heavy stats would make it.
#
# Each stat is compared against its own position group's typical output
# (mean, std deviation) as a z-score - (value - mean) / std - rather than
# a single flat weighted formula, since "a good game" looks completely
# different by role: a defense's average game already carries far more
# disposals than a forward's (14.7 vs 12.7 empirically), while a forward's
# average game carries far more goals (1.08 vs ~0 for defense). Using
# ANY one flat formula across positions (the way brownlow_votes_proxy
# deliberately does, to match the real medal's own historical bias)
# would make best & fairest inherit that same bias - the opposite of what
# a position-neutral club award should do.
#
# Values below were measured empirically from real match_sim.py output
# (simulate_match across many realistic matches, grouped by
# best_fairest_group) - not guessed. Only stats that are actually
# meaningful signal for that group are included (e.g. a defender's goal
# count is near-zero with high relative noise - included it would just
# add spurious swings, not real signal; same reasoning excludes spoils
# from ruck, whose spoil rate barely differs from a modest baseline).
BEST_FAIREST_BASELINES = {
    "defense": {"disposals": (14.7, 6.9), "marks": (5.9, 2.8), "tackles": (3.1, 1.7), "spoils": (2.5, 1.7)},
    "midfield": {"disposals": (20.1, 9.9), "tackles": (4.9, 2.9), "goals": (0.5, 0.8), "marks": (4.0, 2.2)},
    "forward": {"goals": (1.1, 1.2), "disposals": (12.7, 5.6), "marks": (4.2, 2.0), "tackles": (4.2, 2.7)},
    "ruck": {"hitouts": (25.6, 7.7), "disposals": (14.5, 6.3), "marks": (3.8, 1.9), "tackles": (3.1, 1.6)},
}

# Noise stddev for best_and_fairest_votes(), same multiplicative-Gaussian
# mechanism as BROWNLOW_VOTE_NOISE_STDDEV. The z-score normalization
# against BEST_FAIREST_BASELINES already does most of the position-fairness
# work on its own, so a much smaller value than Brownlow's would still keep
# an even defense/midfield/forward/ruck spread - but season-long vote
# TALLIES were still heavily concentrated on the same standout player at
# a small value (e.g. 0.3 averaged ~98 votes for a club's own B&F winner
# out of 115 possible - the same player winning nearly every single week).
# Note that unlike Brownlow, club best & fairest genuinely doesn't have a
# comparable real-world "should land in the 20s/30s" target to calibrate
# against - a real club B&F winner routinely racks up a much higher count
# than the Brownlow medallist, since they're not competing against the
# whole league for votes, just their own 23 - so this is tuned to simply
# match Brownlow's own value (0.8) for a real week-to-week spread, not to
# hit any specific season-total number. Confirmed this still keeps a
# reasonable position-group spread among season B&F winners in testing
# (no group shut out, though naturally still skews toward whichever
# groups the underlying proxy/baselines already favour a little).
BEST_FAIREST_VOTE_NOISE_STDDEV = 0.8


class PlayerStatLine:
    """Accumulated per-player stats for one simulated match."""

    def __init__(self, player):
        self.player = player
        self.disposals = 0
        self.goals = 0
        self.behinds = 0
        self.marks = 0
        self.tackles = 0
        self.spoils = 0
        self.hitouts = 0

    @property
    def score_contribution(self):
        return self.goals * 6 + self.behinds

    @property
    def brownlow_votes_proxy(self):
        """Lightweight best-on-ground ranking metric, tuned to match the
        real Brownlow Medal's historical bias toward high-disposal
        midfielders over pure goalkickers - the medal is awarded by field
        umpires judging overall influence on the game, not a scoreboard
        formula, so goals/behinds are deliberately NOT weighted anywhere
        near their actual 6-to-1 scoreboard value here (that's
        score_contribution, a different property, for ladder points).
        Disposals dominate (a genuinely huge ~30+ disposal game can
        outscore a bag of 3-4 goals); tackles are bumped up too since a
        heavy tackling game draws real votes in real life; spoils/hitouts
        stay minor, role-specific value-adds rather than vote-getters."""
        return (
            self.goals * 3 + self.behinds * 0.5
            + self.disposals * 1.0
            + self.marks * 0.5
            + self.tackles * 1.0
            + self.spoils * 0.2
            + self.hitouts * 0.2
        )

    @property
    def best_fairest_proxy(self):
        """Position-neutral best-on-ground ranking metric for club best &
        fairest (see MatchResult.best_and_fairest_votes) - deliberately a
        DIFFERENT formula from brownlow_votes_proxy above, which is
        intentionally biased toward midfielders to match the real medal's
        history. This instead sums z-scores ((value - mean) / std) for
        each stat that's meaningful for this player's role THIS match
        (via player_group(self.player) - the SAME per-match role
        resolution every other stat generator, team strength calc, and
        Brownlow voting all share - see _resolve_bench_groups for how a
        bench player's group is decided), comparing their game against
        that role's own typical output (BEST_FAIREST_BASELINES) rather
        than one flat cross-position scale - so a big defensive game and
        a big midfield game are judged on equal footing relative to what's
        normal for each role, not squeezed onto the same
        disposal-dominated yardstick."""
        group = player_group(self.player)
        total = 0.0
        for stat_name, (mean, std) in BEST_FAIREST_BASELINES[group].items():
            total += (getattr(self, stat_name) - mean) / std
        return total


class TeamMatchResult:
    def __init__(self, team_name, players):
        self.team_name = team_name
        self.stat_lines = {p.player_id: PlayerStatLine(p) for p in players}
        # A rushed behind (defender deliberately concedes rather than allow
        # a goal) still counts on the scoreboard exactly like any other
        # behind - it just isn't credited to the attacking shooter's own
        # tally, since it wasn't their doing. Kept as a separate team-level
        # counter rather than a player stat so team scoring accuracy is
        # completely unaffected; only individual shooters' accuracy stats
        # improve slightly, since some of what would have been their
        # behinds are instead uncredited. See RUSHED_BEHIND_CHANCE.
        self.rushed_behinds = 0

    @property
    def goals(self):
        return sum(s.goals for s in self.stat_lines.values())

    @property
    def behinds(self):
        return sum(s.behinds for s in self.stat_lines.values()) + self.rushed_behinds

    @property
    def score(self):
        return self.goals * 6 + self.behinds

    def best_on_ground(self):
        return max(self.stat_lines.values(), key=lambda s: s.brownlow_votes_proxy)

    def best_and_fairest_votes(self, rng=None):
        """5-4-3-2-1 club best & fairest votes among THIS team's own 23
        players only - unlike MatchResult.brownlow_votes (whole match,
        both teams combined, deliberately midfield-biased to match real
        Brownlow history), this is a separate, per-team, position-neutral
        award. Ranked by PlayerStatLine.best_fairest_proxy (a per-role
        z-score against BEST_FAIREST_BASELINES, not one flat cross-position
        formula), with the same kind of per-player random multiplier
        Brownlow uses (BEST_FAIREST_VOTE_NOISE_STDDEV) applied before
        ranking - without it the same standout players within a team would
        win almost every week; empirically this needs much less noise than
        Brownlow's to spread votes evenly across positions, since the
        z-score normalization already does most of that work on its own.

        Returns {player_id: votes} for exactly the top 5 (ties broken by
        stable sort order); everyone else is simply absent from the dict,
        so callers should use .get(player_id, 0)."""
        rng = rng or random.Random()
        ranked = sorted(
            self.stat_lines.values(),
            key=lambda s: s.best_fairest_proxy * rng.gauss(1.0, BEST_FAIREST_VOTE_NOISE_STDDEV),
            reverse=True,
        )
        votes = {}
        for stat_line, vote_count in zip(ranked[:5], (5, 4, 3, 2, 1)):
            votes[stat_line.player.player_id] = vote_count
        return votes


class MatchResult:
    def __init__(self, home, away):
        self.home = home
        self.away = away

    def brownlow_votes(self, rng=None):
        """3-2-1 votes to the best three players in the WHOLE match (both
        teams combined, ranked together) - matches how the real Brownlow
        Medal is awarded (one field umpire panel judging the whole game,
        not separately per team), so a dominant team can plausibly take
        all 3 votes in a blowout, same as real footy. Ranked by
        PlayerStatLine.brownlow_votes_proxy - the same formula
        TeamMatchResult.best_on_ground already uses, weighted toward
        disposals/goals so it naturally favours midfielders, matching the
        real medal's historical winner profile.

        Before ranking, each player's brownlow_votes_proxy is adjusted by
        three multipliers (in this order - order doesn't actually matter
        since they're all multiplicative, but conceptually: role, team
        result, then randomness):
          1. BROWNLOW_POSITION_VOTE_WEIGHT, keyed by their SLOT's group
             (defense/midfield/forward via slot_group - on-field role for
             THIS match, not their roster position) - boosts non-midfield
             slots, since the proxy's own disposal-heavy weighting still
             let the same elite midfielders dominate almost every week
             even with noise alone applied.
          2. BROWNLOW_WINNING_TEAM_VOTE_WEIGHT for every player on the
             match's winning team (no boost on a draw) - real Brownlow
             voting does skew toward winning sides, and this also helps
             rotate the winner pool across a season (the same team doesn't
             win every week).
          3. A per-player random multiplier (BROWNLOW_VOTE_NOISE_STDDEV) -
             without it, the same handful of elite players would top the
             proxy almost every single week. Real field umpire judgment
             isn't a pure stat-sheet readout, and votes get spread around
             a competitive field far more than a deterministic formula
             alone would produce.
        Together these are purely a vote-count-realism tool, tuned so a
        season of simulated rounds produces medallist tallies closer to
        the real medal's usual high-20s/low-30s range - none of them
        affect any other stat, ladder result, or match outcome.

        Returns {player_id: votes} for exactly the top 3 (ties broken by
        stable sort order - whichever appeared first in the combined
        list); every other player is simply absent from the dict rather
        than mapped to 0, so callers should use .get(player_id, 0)."""
        rng = rng or random.Random()

        if self.home.score > self.away.score:
            winning_team_result = self.home
        elif self.away.score > self.home.score:
            winning_team_result = self.away
        else:
            winning_team_result = None  # draw - no boost to either side

        def weighted_score(stat_line):
            # "ruck" comes back for anyone actually playing ruck this
            # match (on-field at RUCK_SLOT, or a bench player
            # _resolve_bench_groups assigned to ruck) - brownlow_votes
            # only has a 3-way split, so that collapses to "midfield"
            # here, same as slot_group() already treats the ruck slot.
            group = player_group(stat_line.player)
            position_weight = BROWNLOW_POSITION_VOTE_WEIGHT[group if group != "ruck" else "midfield"]
            team_weight = (
                BROWNLOW_WINNING_TEAM_VOTE_WEIGHT
                if winning_team_result is not None and stat_line.player.player_id in winning_team_result.stat_lines
                else 1.0
            )
            noise = rng.gauss(1.0, BROWNLOW_VOTE_NOISE_STDDEV)
            return stat_line.brownlow_votes_proxy * position_weight * team_weight * noise

        all_lines = list(self.home.stat_lines.values()) + list(self.away.stat_lines.values())
        ranked = sorted(all_lines, key=weighted_score, reverse=True)
        votes = {}
        for stat_line, vote_count in zip(ranked[:3], (3, 2, 1)):
            votes[stat_line.player.player_id] = vote_count
        return votes


# Real AFL teams typically run 2-3 key position players per line (defense
# or forward) - the 3rd is often in a pocket rather than the spine. A 4th+
# is an unbalanced, tall-heavy line lacking the mobility a normal mix has.
# This only counts players genuinely on-field in that line right now (not
# bench key position players sitting on the interchange).
KEY_POSITION_COUNT_THRESHOLD = 3
# Tuned via controlled A/B testing (same lineup, only the count of KEY DEF/
# KEY FWD on-field varied) rather than picked from the raw % alone - 0.05
# measured as a ~15-point win-rate swing for one excess player, which felt
# too severe for a real but minor lineup inefficiency; 0.02 lands closer to
# a ~7-8 point swing.
KEY_POSITION_OVERLOAD_PENALTY_PER_EXCESS = 0.02  # ~2% line strength per player past the threshold

# Lines this overload rule applies to - defense and forward only, never
# midfield (a stack of midfielders isn't a "tall-heavy" problem the way a
# stack of key position players is).
KEY_POSITION_OVERLOAD_GROUPS = {"defense", "forward"}


def _group_strength(players, group):
    """Weighted mean effective_OVR of a group's members - interchange players
    pull less weight than starters (see INTERCHANGE_STRENGTH_WEIGHT).

    A bench player's group membership is player_group(p) - the same
    per-match resolution (see _resolve_bench_groups) every stat generator,
    voting, and this function all now share, rather than each guessing
    independently. "ruck" collapses to "midfield" here since that's a
    ruck's genuine strength-contribution group - ruck contests are won at
    centre bounces, same reasoning as POSITION_ALLOWED_LINES["RUCK"]. An
    on-field player's group is still exactly their own slot.

    Also applies a small penalty when the line (defense or forward - see
    KEY_POSITION_OVERLOAD_GROUPS) is overloaded with key position players
    (more than KEY_POSITION_COUNT_THRESHOLD genuinely on-field in this
    group) - see KEY_POSITION_OVERLOAD_PENALTY_PER_EXCESS. Counts ANY
    KEY_POSITION_TYPES player currently contributing to this line - not
    just that line's own "natural" key-equivalent positions - since RUCK is
    now a key-position type in its own right (see match_sim.py's
    positioning-penalty model) and a mismatched KEY FWD/KEY DEF stuck in
    the wrong line is still a tall crowding that line, not a non-event.
    UTILITY never counts (it's a GENERAL_POSITION_TYPES member)."""
    def member_group(p):
        g = player_group(p)
        return "midfield" if g == "ruck" else g

    members = [p for p in players if member_group(p) == group]
    if not members:
        return 0.0
    total_weight = sum(p.strength_weight for p in members)
    if total_weight == 0:
        return 0.0
    strength = sum(p.effective_ovr * p.strength_weight for p in members) / total_weight

    if group in KEY_POSITION_OVERLOAD_GROUPS:
        # On-field only, per the "genuinely on-field" rule above - a bench
        # key-position player in this group doesn't crowd the line the way
        # an extra starter would, so they're excluded from the count here
        # even though they still count as members[] for the strength mean
        # itself just above.
        key_count = sum(1 for p in members if p.position in KEY_POSITION_TYPES and p.slot not in INTERCHANGE_SLOTS)
        excess = max(0, key_count - KEY_POSITION_COUNT_THRESHOLD)
        if excess:
            strength *= max(0.0, 1 - excess * KEY_POSITION_OVERLOAD_PENALTY_PER_EXCESS)

    return strength


def _team_strengths(players):
    return {
        "defense": _group_strength(players, "defense"),
        "midfield": _group_strength(players, "midfield"),
        "forward": _group_strength(players, "forward"),
    }


# Flat OVR-equivalent bonus applied to every one of the home team's three
# group strengths - flows through every downstream mechanic that already
# keys off team strength (shots, disposals, and therefore scoring) exactly
# like any other strength differential, rather than a separate one-off
# win-probability nudge bolted onto just one stat. A fixed effect regardless
# of matchup - real AFL's ~60% home win rate is a league-wide average across
# both close and lopsided games, and a small flat bonus already naturally
# matters less against a big existing gap and more in an even matchup.
HOME_GROUND_ADVANTAGE = 0.8  # tuned against the REAL league's full fixture
                              # list (mismatched and even matchups alike,
                              # home team randomized) to a ~55% home win
                              # rate - dialed down from 1.45 (~58%). This is
                              # the comparable number to real AFL's actual
                              # home win rate stat, which is also a
                              # whole-league average, not an even-matchup-
                              # only stat - an isolated identical-lineup
                              # test reads noticeably higher at the same
                              # value, since real OVR spread structurally
                              # pulls the league-wide rate down (a small
                              # fixed home bonus can't rescue a big
                              # underlying mismatch as often as it tips an
                              # even game).


def _apply_home_ground_advantage(home_strengths):
    return {group: strength + HOME_GROUND_ADVANTAGE for group, strength in home_strengths.items()}


SHOT_DIFF_SENSITIVITY = 0.8  # tuned against team OVR gaps alone (best-vs-worst
                              # real league gap, ~8pts) - history: 0.7 -> 1.1
                              # -> 1.3 as BASE_SHOTS_PER_TEAM rose 24->36 and
                              # the best-vs-worst target crept toward ~95-98%;
                              # then retuned DOWN to 0.8 after BASE_SHOTS_PER_TEAM
                              # dropped 36->27 (see the forward/other goal-
                              # accuracy split), since a smaller shot-count base
                              # gives the same absolute OVR-diff term much more
                              # RELATIVE leverage on the shot split - left
                              # unretuned it would have pushed the best-vs-worst
                              # matchup to ~99.9%. 0.8 restores it to the same
                              # ~97-98% zone that was previously, deliberately,
                              # accepted (see the mid-table-separation-vs-
                              # extreme-matchup tradeoff this constant keeps
                              # getting caught in). Real lineup composition
                              # flaws (tall-overload, spine/pocket penalties)
                              # are meant to move a specific team's actual win
                              # rate away from what pure OVR would predict, not
                              # be re-absorbed back into this general constant.

SHOT_OVR_EXPONENT = 7.0  # how much effective_OVR is exponentiated when
                          # picking who takes a shot - see _pick_shooter

# Each side of the ball (offense / defense) is 100% owned between two lines:
# the specialist line carries the majority share, midfield carries the rest -
# giving midfield equal-sized (symmetric) responsibility for both sides,
# while forward/back stay single-purpose specialists.
#   Team offense = forward * FORWARD_OFFENSE_SHARE + midfield * MIDFIELD_OFFENSE_SHARE
#   Team defense = backline * BACKLINE_DEFENSE_SHARE + midfield * MIDFIELD_DEFENSE_SHARE
# Midfield's share is nudged a bit above the "clean" 0.33 split - an isolated
# equal-OVR boost to each line in turn showed midfield trailing defense/forward
# by ~2-3 win-rate points at 0.33; 0.36 closes that gap (measured ~68-69% for
# all three lines at N=3000, see the design doc / sim_batch.py for the method).
FORWARD_OFFENSE_SHARE = 0.66
MIDFIELD_OFFENSE_SHARE = 0.36
BACKLINE_DEFENSE_SHARE = 0.66
MIDFIELD_DEFENSE_SHARE = 0.36

# league_avg_ovr's own coefficient is derived so two perfectly average teams
# (every group at league_avg_ovr) net to a neutral diff of exactly 0 - neither
# boosted nor suppressed relative to BASE_SHOTS_PER_TEAM. At league average,
# attacking = avg*(offense shares) and suppression = avg*(defense shares), so
# the baseline is just the difference between the two share totals (0 here,
# since both sides sum to the same 1.02 - kept as a formula, not hardcoded,
# so this stays correct if the shares are ever tuned to something asymmetric).
_LEAGUE_AVG_BASELINE_WEIGHT = (
    (FORWARD_OFFENSE_SHARE + MIDFIELD_OFFENSE_SHARE)
    - (BACKLINE_DEFENSE_SHARE + MIDFIELD_DEFENSE_SHARE)
)


def _shot_count(own_strengths, opp_strengths, league_avg_ovr, variance, rng):
    """Expected scoring shots for one team, drawn from a Poisson-ish distribution
    whose mean is nudged by this team's offense (forward + a midfield share)
    minus the opponent's defense (backline + a midfield share)."""
    attacking = (
        own_strengths["forward"] * FORWARD_OFFENSE_SHARE
        + own_strengths["midfield"] * MIDFIELD_OFFENSE_SHARE
    )
    suppression = (
        opp_strengths["defense"] * BACKLINE_DEFENSE_SHARE
        + opp_strengths["midfield"] * MIDFIELD_DEFENSE_SHARE
    )
    diff = attacking - suppression - league_avg_ovr * _LEAGUE_AVG_BASELINE_WEIGHT
    expected = BASE_SHOTS_PER_TEAM + diff * SHOT_DIFF_SENSITIVITY
    expected = max(8.0, expected)  # floor - even a weak team gets some looks

    spread = max(0.05, variance)
    shots = rng.gauss(expected, expected * 0.18 * spread)
    return max(4, round(shots))


def _disposal_count(own_strengths, opp_strengths, variance, rng):
    """Expected TEAM total disposals, driven by overall team strength vs. the
    opponent's - not the forward/defense split shots use, since ball-winning
    is a whole-team contest (led by midfield, but backs and forwards both
    contribute) rather than an attack-vs-defense matchup. A team's edge here
    tracks the SAME underlying strength gap that decides shots/scoring, so a
    team that's comfortably better tends to both win more of the ball and win
    by more - without disposals needing the actual final margin as an input."""
    own_avg = (own_strengths["defense"] + own_strengths["midfield"] + own_strengths["forward"]) / 3
    opp_avg = (opp_strengths["defense"] + opp_strengths["midfield"] + opp_strengths["forward"]) / 3
    diff = own_avg - opp_avg
    expected = BASE_DISPOSALS_PER_TEAM + diff * DISPOSAL_DIFF_SENSITIVITY
    expected = max(250.0, expected)  # floor - even a weak team touches the ball plenty in AFL

    # Low variance coefficient relative to shots/other stats - since two
    # teams each draw an INDEPENDENT Gaussian total, the gap between them has
    # its own (larger) variance than either draw alone. A coefficient in the
    # same ballpark as shots' 0.18 left even identical lineups averaging a
    # ~20-disposal gap purely from noise, which reads as a real disparity
    # rather than a close, evenly-matched game - tuned down so two
    # same-strength teams land close together most of the time, matching
    # the "no meaningful disparity in close games" target.
    spread = max(0.05, variance)
    total = rng.gauss(expected, expected * 0.015 * spread)
    return max(200, round(total))


def shot_role_weight(player):
    """SHOT_ROLE_WEIGHT for a player, demoted when a forward-leaning hybrid
    (MID-FWD, SWINGMAN, UTILITY, RUCK-FWD) isn't actually playing forward
    this match (player_group(player) - their own slot if on-field, or
    _resolve_bench_groups' per-match assignment if on the interchange) -
    their elevated shot weight reflects genuine forward-line involvement,
    not a generic trait they carry everywhere.

    MID-FWD specifically gets no weight of its own AT ALL - a MID-FWD
    genuinely playing forward is treated exactly like a GEN FWD (not its own
    slightly-different weight), same as it's already treated exactly like a
    MID when genuinely playing midfield."""
    position = player.position
    group = player_group(player)
    group = "midfield" if group == "ruck" else group

    if position == "MID-FWD":
        if group == "forward":
            return SHOT_ROLE_WEIGHT["GEN FWD"]
        if group == "midfield":
            return SHOT_ROLE_WEIGHT["MID"]

    base_weight = SHOT_ROLE_WEIGHT.get(position, DEFAULT_SHOT_ROLE_WEIGHT)

    if group == "forward":
        return base_weight

    generic_weight = GENERIC_SHOT_WEIGHT_BY_GROUP.get(group)
    if generic_weight is not None and base_weight > generic_weight:
        return generic_weight
    return base_weight


def _pick_shooter(on_field_players, rng):
    """Weighted random pick of who takes a given shot, using effective_OVR^
    SHOT_OVR_EXPONENT times the player's shot role weight (key forwards get
    proportionally more looks than general forwards at the same OVR;
    non-forwards are eligible too, just heavily deweighted - see
    SHOT_ROLE_WEIGHT). The high exponent is what stretches a truly elite
    player's ceiling within their position - OVR only spans a fairly narrow
    band per position (e.g. most KEY FWDs sit 85-99), so a low exponent
    barely separates a great player from a merely-good one; this pushes the
    very best noticeably clear without meaningfully touching the
    typical/floor range for that position. This same exponent already gives
    elite (95+ OVR) MID/MID-FWD players a real, noticeable goalkicking
    ceiling on its own - season-long the best land ~1.2-1.9 goals/game
    without needing any position-specific bonus on top."""
    weights = []
    for p in on_field_players:
        role_weight = shot_role_weight(p)
        weights.append((p.effective_ovr ** SHOT_OVR_EXPONENT) * role_weight)
    return rng.choices(on_field_players, weights=weights, k=1)[0]


def _goal_chance(shooter, league_avg_ovr):
    """Fraction of ON-TARGET shots (goal or behind - see ON_TARGET_CHANCE)
    that go through as a goal rather than a behind. Driven primarily by
    whether the shooter is GENUINELY PLAYING FORWARD this match (their
    on-field slot, or _resolve_bench_groups' per-match assignment if on
    the interchange - a MID parked at CHF gets forward accuracy, a KEY FWD
    pushed into midfield doesn't), same player_group() check already used
    by shot_role_weight/disposal_tier/etc. OVR still gives a small nudge
    within whichever band applies, same shape as before this was split by
    group."""
    is_forward = player_group(shooter) == "forward"
    base = BASE_GOAL_ACCURACY_FORWARD if is_forward else BASE_GOAL_ACCURACY_OTHER
    nudge = ACCURACY_OVR_SENSITIVITY * (shooter.effective_ovr - league_avg_ovr)
    chance = base + nudge
    # Clamp bounds are relative to their own BASE_GOAL_ACCURACY_* (+/- ~0.06)
    # so the OVR nudge still has real room to move within, without letting a
    # stale hardcoded range silently override a retuned base value.
    if is_forward:
        return min(BASE_GOAL_ACCURACY_FORWARD + 0.06, max(BASE_GOAL_ACCURACY_FORWARD - 0.06, chance))
    return min(BASE_GOAL_ACCURACY_OTHER + 0.06, max(BASE_GOAL_ACCURACY_OTHER - 0.06, chance))


def _simulate_disposals(team_players, own_strengths, opp_strengths, variance, rng):
    """Team-total-first, same shape as goals: _disposal_count() rolls one
    number for the whole team (driven by relative team strength), then that
    total is split across the 23 players proportionally by disposal_tier
    weight and individual effective_OVR - not each player rolling an
    independent expected value.

    The split itself also needs its own per-player randomness (a quiet game
    vs. a breakout game) - without it, a player's SHARE of the team total is
    a fixed ratio every match (only the team total varies), which produces
    an unrealistically narrow game-to-game range for every individual
    player even though the team total itself has plausible variance."""
    team_total = _disposal_count(own_strengths, opp_strengths, variance, rng)

    weights = []
    for p in team_players:
        tier_weight = DISPOSAL_TIER_WEIGHT[disposal_tier(p)]
        base_weight = tier_weight * (p.effective_ovr ** DISPOSAL_OVR_EXPONENT)
        noise = max(0.05, rng.gauss(1.0, DISPOSAL_INDIVIDUAL_VARIANCE * max(0.05, variance)))
        weights.append(base_weight * noise)
    weight_total = sum(weights)

    # Proportional split, then round with a residual correction so the
    # per-player total always sums to EXACTLY the team total rolled above -
    # naive independent rounding would drift by a few disposals either way.
    raw_shares = [team_total * w / weight_total for w in weights]
    disposals = [int(share) for share in raw_shares]
    remainder = team_total - sum(disposals)
    # Hand out the leftover disposals to whichever players lost the most to
    # rounding, largest-remainder-first - keeps the split fair rather than
    # always favoring the first few players in the list.
    fractional_order = sorted(range(len(team_players)), key=lambda i: -(raw_shares[i] - disposals[i]))
    for i in fractional_order[:remainder]:
        disposals[i] += 1

    return {p.player_id: d for p, d in zip(team_players, disposals)}


def _simulate_tiered_stat(all_players, league_avg_ovr, variance, rng,
                           weight_fn, base_rate,
                           sensitivity_above, sensitivity_below, floor):
    """Shared generator for marks/tackles/spoils - same weight * base *
    OVR-sensitivity shape as disposals, parameterized per stat. weight_fn is
    called as weight_fn(player) -> numeric weight (0.0-1.0-ish)."""
    stat_lines = {}
    for p in all_players:
        tier_weight = weight_fn(p)
        base = base_rate * tier_weight
        ovr_diff = p.effective_ovr - league_avg_ovr
        sensitivity = sensitivity_above if ovr_diff >= 0 else sensitivity_below
        # Same fix as disposals: OVR bonus scales WITH the tier weight so a
        # low-tier player's raw OVR can't override their positional ceiling
        ovr_nudge = sensitivity * ovr_diff * tier_weight
        expected = max(floor, base + ovr_nudge)
        value = max(0, round(rng.gauss(expected, expected * 0.35 * max(0.05, variance))))
        stat_lines[p.player_id] = value
    return stat_lines


def _simulate_hitouts(all_players, league_avg_ovr, variance, rng):
    """Ruck players get a continuous OVR-scaled hitout count (same shape as
    the other tiered stats). Everyone else is always exactly 0.

    Only a player ACTUALLY PLAYING RUCK this match gets hitouts -
    on-field, that's exactly RUCK_SLOT; on the bench, that's whoever
    _resolve_bench_groups assigned .resolved_group = "ruck" to (at most
    one player per team, and only when no true natural RUCK already
    covers it - see that function's ruck-availability rule). A
    RUCK-DEF/RUCK-FWD who didn't win the bench's ruck spot this match
    (because a real ruck was already on the bench, or another
    RUCK-DEF/RUCK-FWD claimed it first) correctly gets 0 hitouts, same as
    any other non-ruck player - they're not genuinely rucking this game."""
    stat_lines = {}
    for p in all_players:
        if p.position in HITOUT_RUCK_POSITIONS and player_group(p) == "ruck":
            ovr_diff = p.effective_ovr - league_avg_ovr
            sensitivity = HITOUT_OVR_SENSITIVITY_ABOVE_AVG if ovr_diff >= 0 else HITOUT_OVR_SENSITIVITY_BELOW_AVG
            expected = max(10.0, BASE_HITOUTS + sensitivity * ovr_diff)
            value = max(0, round(rng.gauss(expected, expected * 0.25 * max(0.05, variance))))
        else:
            value = 0
        stat_lines[p.player_id] = value
    return stat_lines


def _simulate_team_shots(team_players, opp_strengths, own_strengths, league_avg_ovr, variance, rng, result,
                          team_name=None, quarter_lengths=None, events=None):
    """Resolves one team's shots. When quarter_lengths/events are supplied
    (the live-feed path), each scoring shot also becomes a MatchEvent placed
    at a random quarter/minute and appended to the shared events list -
    same resolved shots, just also captured before the loop discards their
    order, per the Live Match Feed design doc."""
    # Every on-field player is shot-eligible - SHOT_ROLE_WEIGHT is what keeps
    # forwards dominant and defenders rare, not pool membership.
    if not team_players:
        return

    total_shots = _shot_count(own_strengths, opp_strengths, league_avg_ovr, variance, rng)

    for _ in range(total_shots):
        shooter = _pick_shooter(team_players, rng)
        line = result.stat_lines[shooter.player_id]
        kind = None
        rushed = False
        if rng.random() < ON_TARGET_CHANCE:
            # On target - now split goal vs. behind (real AFL accuracy is
            # ~48% goals of shots that score at all, not of every shot)
            goal_chance = _goal_chance(shooter, league_avg_ovr)
            if rng.random() < goal_chance:
                line.goals += 1
                kind = "goal"
            elif rng.random() < RUSHED_BEHIND_CHANCE:
                # Rushed - still a behind on the scoreboard, just not this
                # shooter's personal credit (see TeamMatchResult.rushed_behinds)
                result.rushed_behinds += 1
                kind = "behind"
                rushed = True
            else:
                line.behinds += 1
                kind = "behind"
        # else: shot missed entirely, no stat recorded

        if kind is not None and events is not None:
            quarter = rng.choice((1, 2, 3, 4))
            minute = rng.uniform(0, quarter_lengths[quarter - 1])
            match_minute = sum(quarter_lengths[:quarter - 1]) + minute
            events.append(MatchEvent(kind, quarter, minute, match_minute, team_name, shooter, rushed=rushed))


# Extra time (finals-only draw-breaker) - two short halves, repeated in
# further pairs of halves for as long as the scores stay level. Genuinely
# INCREMENTAL, unlike the rest of the match: the normal 4-quarter simulation
# is always fully pre-computed up front (see simulate_match_with_events),
# but extra time can only be known to be needed once Q4 actually ends in a
# draw, and can't be known to have ended until a half is actually simulated
# and checked - so each half is simulated fresh, one at a time, by the
# live-match command loop calling simulate_extra_time_half() as it goes,
# rather than being part of the single big up-front simulate call.
#
# Like QUARTER_LENGTH_* above, these are ELAPSED minutes, not playing time.
# The league's rule is "2 x 3 minute halves + time on" (as stated in the
# rules embed the feed posts - see _extra_time_info_embed in
# match_commands.py); the extra elapsed minutes ARE that time on.
#
# Derived from the quarter's own figures rather than hardcoded, so extra
# time gets stoppages at exactly the same rate as the rest of the match: a
# quarter is 20 minutes of playing time that elapses ~28, so a 3-minute half
# elapses 3 * 28/20 = 4.2. Changing a quarter's length now carries through
# here automatically instead of quietly leaving the two inconsistent.
EXTRA_TIME_HALF_PLAYING_MINUTES = 3  # the league's stated "3 minute halves"
QUARTER_PLAYING_MINUTES = 20  # an AFL quarter's nominal playing time

_TIME_ON_SCALE = EXTRA_TIME_HALF_PLAYING_MINUTES / QUARTER_PLAYING_MINUTES

EXTRA_TIME_HALF_LENGTH_MIN = QUARTER_LENGTH_MIN * _TIME_ON_SCALE    # 3.75
EXTRA_TIME_HALF_LENGTH_MAX = QUARTER_LENGTH_MAX * _TIME_ON_SCALE    # 5.25
EXTRA_TIME_HALF_LENGTH_MODE = QUARTER_LENGTH_MODE * _TIME_ON_SCALE  # 4.20


def extra_time_half_length_minutes(rng):
    return rng.triangular(
        EXTRA_TIME_HALF_LENGTH_MIN, EXTRA_TIME_HALF_LENGTH_MAX, EXTRA_TIME_HALF_LENGTH_MODE
    )


# Nominal length used for anything that needs a single representative value
# rather than one half's actual roll (the shot-count scale below).
EXTRA_TIME_HALF_LENGTH_MINUTES = EXTRA_TIME_HALF_LENGTH_MODE

# Same team-strength-driven formula as _shot_count, just scaled down to a
# much shorter period - reuses the tuned team-strength inputs/accuracy
# model rather than a separately-tuned extra-time-only formula. _shot_count
# itself isn't reused directly since its floor (8.0 expected, 4 minimum)
# is calibrated for a ~28-minute quarter and would swamp a short half.
# Scaled off the MODE rather than each half's own roll so the shot rate
# stays consistent; a longer half then naturally has its shots spread over
# more minutes rather than getting proportionally more of them.
# Extra time scores at the SAME per-minute rate as normal play. Note this
# is deliberately NOT BASE_SHOTS_PER_TEAM scaled by period length: that
# constant feeds _shot_count, whose output is a raw contest/possession count
# that later gets filtered down before anything reaches the scoreboard
# (~27 per team per quarter becomes ~6 scoring shots). _extra_time_shot_count
# has no such filtering - almost every shot it returns becomes a score via
# ON_TARGET_CHANCE - so scaling off BASE_SHOTS_PER_TEAM over-scored extra
# time roughly fourfold.
#
# Calibrated instead against the MEASURED full-match rate: 4000 simulated
# matches between even sides produce ~49 scoring shots across 4x28 minutes,
# i.e. ~0.44 per minute for both teams, ~0.22 per team.
_MATCH_SCORING_SHOTS_PER_TEAM_PER_MINUTE = 0.219

# Divided back out by ON_TARGET_CHANCE because that is applied per shot by
# the caller - this figure is raw shots, of which ~91% become a score.
_EXTRA_TIME_SHOTS_PER_TEAM_PER_MINUTE = (
    _MATCH_SCORING_SHOTS_PER_TEAM_PER_MINUTE / ON_TARGET_CHANCE
)

# How much a strength mismatch tilts the rate, as a fraction of the even-teams
# baseline. SHOT_DIFF_SENSITIVITY is calibrated in raw _shot_count units and
# would swamp a rate this small, so the same `diff` is applied proportionally
# instead: a clearly stronger side gets meaningfully more of the ball without
# the weaker side ever dropping to a guaranteed zero.
_EXTRA_TIME_DIFF_SENSITIVITY = 0.02
_EXTRA_TIME_MIN_RATE_MULTIPLIER = 0.4
_EXTRA_TIME_MAX_RATE_MULTIPLIER = 1.8


def _extra_time_shot_count(own_strengths, opp_strengths, league_avg_ovr, variance, rng,
                           half_length_minutes=None):
    """Scoring shots for ONE team in one extra-time half.

    Poisson-distributed rather than a rounded gaussian: at roughly one shot
    per team per half, the count distribution near zero is the whole point
    (a goalless extra-time period is a real outcome), and a gaussian
    rounds that badly.
    """
    if half_length_minutes is None:
        half_length_minutes = EXTRA_TIME_HALF_LENGTH_MODE

    attacking = (
        own_strengths["forward"] * FORWARD_OFFENSE_SHARE
        + own_strengths["midfield"] * MIDFIELD_OFFENSE_SHARE
    )
    suppression = (
        opp_strengths["defense"] * BACKLINE_DEFENSE_SHARE
        + opp_strengths["midfield"] * MIDFIELD_DEFENSE_SHARE
    )
    diff = attacking - suppression - league_avg_ovr * _LEAGUE_AVG_BASELINE_WEIGHT

    multiplier = 1.0 + diff * _EXTRA_TIME_DIFF_SENSITIVITY
    multiplier = min(_EXTRA_TIME_MAX_RATE_MULTIPLIER,
                     max(_EXTRA_TIME_MIN_RATE_MULTIPLIER, multiplier))

    expected = _EXTRA_TIME_SHOTS_PER_TEAM_PER_MINUTE * half_length_minutes * multiplier

    # `variance` widens the spread around that mean without changing it -
    # the same knob the rest of the engine exposes.
    spread = max(0.05, variance)
    if spread != 1.0:
        expected *= max(0.1, rng.gauss(1.0, 0.18 * spread))

    # Poisson sampling by Knuth's method - no numpy dependency anywhere in
    # this module, and the rate here is small enough for it to be cheap.
    limit = math.exp(-expected)
    product = 1.0
    shots = 0
    while True:
        product *= rng.random()
        if product <= limit:
            return shots
        shots += 1


def simulate_extra_time_half(home_players, away_players, home_result, away_result,
                              home_team_name, away_team_name, league_avg_ovr, variance, rng,
                              home_ground_advantage=True):
    """Simulates one extra-time half and adds its goals/behinds DIRECTLY
    into the same ongoing home_result/away_result (season/match stat lines
    keep accumulating - this isn't a fresh separate match). Reuses the same
    Player objects from the original simulate_match_with_events call
    (effective_OVR, positions, slots all already resolved) - just more shots
    added on top of the same two lineups.

    The half's length is rolled here, per half, the same way
    quarter_length_minutes rolls a quarter's - so extra time gets its own
    "time on" rather than always ending on the same whole minute.

    Returns (events, half_length_minutes):
      - events: a fresh list[MatchEvent] for JUST this half (not merged into
        any other event list - the caller decides how to post/track them),
        each with event.minute positioned 0..half_length_minutes.
      - half_length_minutes: this half's actual length, which the caller
        needs for the siren clock and final pacing delay. Reading the module
        constant instead would drift from where the events actually sit.
    """
    half_length = extra_time_half_length_minutes(rng)

    home_strengths = _team_strengths(home_players)
    if home_ground_advantage:
        home_strengths = _apply_home_ground_advantage(home_strengths)
    away_strengths = _team_strengths(away_players)

    events = []

    def resolve_team_shots(team_players, opp_strengths, own_strengths, result, team_name):
        total_shots = _extra_time_shot_count(
            own_strengths, opp_strengths, league_avg_ovr, variance, rng,
            half_length_minutes=half_length,
        )
        for _ in range(total_shots):
            shooter = _pick_shooter(team_players, rng)
            line = result.stat_lines[shooter.player_id]
            kind = None
            rushed = False
            if rng.random() < ON_TARGET_CHANCE:
                goal_chance = _goal_chance(shooter, league_avg_ovr)
                if rng.random() < goal_chance:
                    line.goals += 1
                    kind = "goal"
                elif rng.random() < RUSHED_BEHIND_CHANCE:
                    result.rushed_behinds += 1
                    kind = "behind"
                    rushed = True
                else:
                    line.behinds += 1
                    kind = "behind"

            if kind is not None:
                minute = rng.uniform(0, half_length)
                events.append(MatchEvent(kind, 0, minute, minute, team_name, shooter, rushed=rushed))

    resolve_team_shots(home_players, away_strengths, home_strengths, home_result, home_team_name)
    resolve_team_shots(away_players, home_strengths, away_strengths, away_result, away_team_name)

    events.sort(key=lambda e: e.match_minute)
    _enforce_minimum_event_gap_flat(events, half_length)

    return events, half_length


def simulate_extra_time_after_siren_shot(home_players, away_players, home_result, away_result,
                                          home_team_name, away_team_name, league_avg_ovr, rng,
                                          half_length_minutes=EXTRA_TIME_HALF_LENGTH_MINUTES):
    """After-siren shot for the end of an extra-time half - same mechanic
    and odds as the normal Q4 version (AFTER_SIREN_SHOT_CHANCE_TRAILING/
    _TIED, AFTER_SIREN_GOAL_CHANCE, AFTER_SIREN_MAX_DEFICIT eligibility),
    just driven by the CURRENT ongoing score (which already includes every
    prior quarter AND every prior extra-time half) rather than only Q4's
    own total. Returns the MatchEvent if a shot happened, else None -
    caller is responsible for checking event.siren_beater_winner and
    positioning/posting it.

    half_length_minutes is that half's own rolled length (see
    simulate_extra_time_half, which returns it) - the shot is placed just
    after that siren."""
    home_score = home_result.score
    away_score = away_result.score
    is_tied = home_score == away_score
    shot_chance = AFTER_SIREN_SHOT_CHANCE_TIED if is_tied else AFTER_SIREN_SHOT_CHANCE_TRAILING

    eligible = []
    if home_score >= away_score - AFTER_SIREN_MAX_DEFICIT and home_score <= away_score:
        eligible.append((home_players, home_result, home_team_name))
    if away_score >= home_score - AFTER_SIREN_MAX_DEFICIT and away_score <= home_score:
        eligible.append((away_players, away_result, away_team_name))
    rng.shuffle(eligible)

    for ordinal, (players, result, team_name) in enumerate(eligible):
        if not players or rng.random() >= shot_chance:
            continue
        shooter = _pick_shooter(players, rng)
        line = result.stat_lines[shooter.player_id]
        kind = None
        if rng.random() < ON_TARGET_CHANCE:
            if rng.random() < AFTER_SIREN_GOAL_CHANCE:
                line.goals += 1
                kind = "goal"
            else:
                line.behinds += 1
                kind = "behind"
        if kind is None:
            continue
        # Positioned just PAST this half's siren, so it has to use the
        # half's actual rolled length rather than the nominal constant.
        minute = half_length_minutes + AFTER_SIREN_OFFSET_MINUTES * (ordinal + 1)
        event = MatchEvent(kind, 0, minute, minute, team_name, shooter, after_siren=True)
        was_already_ahead = (home_score > away_score) if team_name == home_team_name else (away_score > home_score)
        new_home_score, new_away_score = home_result.score, away_result.score
        is_winner = not was_already_ahead and new_home_score != new_away_score
        event.siren_beater_winner = kind == "goal" and is_winner
        return event
    return None


# Real AFL occasionally sees a shot land after the siren - a free kick or
# bounce of the ball that plays on despite time expiring. Live-feed easter
# egg: ONCE PER MATCH (Q4 only - a real "after the siren" moment is
# culturally a full-time thing, not something that meaningfully happens at
# quarter/half/three-quarter time), only a team tied or within one goal at
# the siren gets a roll at all (see AFTER_SIREN_MAX_DEFICIT below). Purely
# additive - doesn't touch total_shots or any of the already-tuned
# shot-count/accuracy constants for every OTHER shot, since this is flavor
# on top of the real simulation, not a change to it.
#
# A TRAILING team (only one side can be in this state) gets a flat 10%
# chance. When scores are TIED, both teams are eligible at once, so each
# instead gets the smaller 1-sqrt(1-0.10)=~5.13% - the two independent
# rolls combine back to the same overall 10% "either team gets a shot"
# chance as the trailing case, rather than tied games getting roughly
# double the trailing case's odds of SOME after-siren shot happening.
AFTER_SIREN_SHOT_CHANCE_TRAILING = 0.10
AFTER_SIREN_SHOT_CHANCE_TIED = 1 - (1 - 0.10) ** 0.5
AFTER_SIREN_OFFSET_MINUTES = 0.05  # tiny nudge past the quarter's own length

# Real after-the-siren shots that make the highlight reel are disproportionately
# GOALS, not nervy scrambled behinds - deliberately separate from the normal
# ON_TARGET_CHANCE/BASE_GOAL_ACCURACY (~35.5% combined goal chance for any
# regular shot) rather than nudging those tuned constants.
AFTER_SIREN_GOAL_CHANCE = 0.70

# Only a team that's tied or trailing by this many points or less at the
# final siren is eligible for an after-siren shot - a comfortably-ahead
# team doesn't get a bonus attempt, since a real after-the-siren shot
# happens because a team is still genuinely chasing the result. 6 points =
# one goal, i.e. eligible if a single major would tie or win it outright.
AFTER_SIREN_MAX_DEFICIT = 6


def _simulate_after_siren_shot(team_players, quarter, quarter_lengths, rng, result, team_name, events, shot_chance, ordinal=0):
    """One team's shot at `shot_chance` odds (AFTER_SIREN_SHOT_CHANCE_TRAILING
    or _TIED, picked by the caller based on the score at the siren) - only
    ever called for Q4 (see the call site). Returns the MatchEvent if a shot
    actually happened (a miss - the (1 - AFTER_SIREN_GOAL_CHANCE) remainder
    still includes a chance of missing entirely, not just a behind - see
    below - produces no event), else None. `ordinal` gives each after-siren
    event in the same quarter (rare - both teams hitting the roll in the
    same match) a distinct, increasing offset so they don't land at the
    exact same instant and sort deterministically in the order they were
    resolved."""
    if not team_players or rng.random() >= shot_chance:
        return None

    shooter = _pick_shooter(team_players, rng)
    line = result.stat_lines[shooter.player_id]
    kind = None
    if rng.random() < ON_TARGET_CHANCE:
        if rng.random() < AFTER_SIREN_GOAL_CHANCE:
            line.goals += 1
            kind = "goal"
        else:
            line.behinds += 1
            kind = "behind"

    if kind is None:
        return None

    quarter_length = quarter_lengths[quarter - 1]
    minute = quarter_length + AFTER_SIREN_OFFSET_MINUTES * (ordinal + 1)
    match_minute = sum(quarter_lengths[:quarter - 1]) + minute
    event = MatchEvent(kind, quarter, minute, match_minute, team_name, shooter, after_siren=True)
    events.append(event)
    return event


def _mark_siren_beater_winners(events, home_team_name, away_team_name):
    """Flags any Q4 after-siren GOAL that actually won the match on the spot
    - the team was not already ahead before it, and is ahead after it (a
    tie counts as "not already ahead", so a siren-beater that merely draws
    the scores level does NOT count - has to be the actual winner). Walks
    the full, already-finalized event timeline in order so the running
    score at the moment of each after-siren event is exact."""
    home_goals = home_behinds = away_goals = away_behinds = 0
    for event in events:
        if event.kind not in ("goal", "behind"):
            continue
        is_home = event.team_name == home_team_name

        if event.after_siren and event.quarter == 4 and event.kind == "goal":
            home_score = home_goals * 6 + home_behinds
            away_score = away_goals * 6 + away_behinds
            was_already_ahead = home_score > away_score if is_home else away_score > home_score
            scoring_team_score_before = home_score if is_home else away_score
            other_team_score_before = away_score if is_home else home_score
            if not was_already_ahead and (scoring_team_score_before + 6) > other_team_score_before:
                event.siren_beater_winner = True

        if event.kind == "goal":
            if is_home:
                home_goals += 1
            else:
                away_goals += 1
        else:
            if is_home:
                home_behinds += 1
            else:
                away_behinds += 1


def _simulate_match_core(home_team_name, home_lineup, away_team_name, away_lineup,
                          league_avg_ovr, variance, rng, build_events, home_ground_advantage=True):
    """Shared implementation behind simulate_match() and
    simulate_match_with_events() - identical stat generation either way;
    build_events=True additionally rolls quarter lengths, captures each shot
    as a MatchEvent, and rolls in-match injuries.

    Returns (MatchResult, list[MatchEvent] or None).
    """
    home_players = [Player(*row) for row in home_lineup]
    away_players = [Player(*row) for row in away_lineup]

    # Resolve each team's own bench composition into per-match roles
    # BEFORE any stat generation runs - every downstream stat function,
    # team-strength calc, and voting reads player_group(p), which relies
    # on this having already set .resolved_group for interchange players.
    # Each team's bench is resolved independently (a thin defense on the
    # home bench says nothing about the away bench).
    _resolve_bench_groups([p for p in home_players if p.slot in INTERCHANGE_SLOTS], rng)
    _resolve_bench_groups([p for p in away_players if p.slot in INTERCHANGE_SLOTS], rng)

    home_result = TeamMatchResult(home_team_name, home_players)
    away_result = TeamMatchResult(away_team_name, away_players)

    home_strengths = _team_strengths(home_players)
    if home_ground_advantage:
        home_strengths = _apply_home_ground_advantage(home_strengths)
    away_strengths = _team_strengths(away_players)

    events = None
    quarter_lengths = None
    if build_events:
        events = []
        quarter_lengths = [quarter_length_minutes(rng) for _ in range(4)]

    _simulate_team_shots(home_players, away_strengths, home_strengths, league_avg_ovr, variance, rng, home_result,
                          team_name=home_team_name, quarter_lengths=quarter_lengths, events=events)
    _simulate_team_shots(away_players, home_strengths, away_strengths, league_avg_ovr, variance, rng, away_result,
                          team_name=away_team_name, quarter_lengths=quarter_lengths, events=events)

    after_siren_events = []
    if build_events:
        # Q4 only - a real "after the siren" moment is a full-time thing,
        # not something that meaningfully happens at quarter/half/three-
        # quarter time (see AFTER_SIREN_SHOT_CHANCE's comment). At this
        # point every normal shot for the whole match is already resolved
        # into home_result/away_result (before/behinds), so their scores
        # here ARE the score at the final siren, before any bonus shot.
        quarter = 4
        home_score_at_siren = home_result.score
        away_score_at_siren = away_result.score

        # Only a team that's tied or trailing by AFTER_SIREN_MAX_DEFICIT or
        # less gets a shot at it - a real after-the-siren attempt happens
        # because a team is chasing the game, not as a bonus for a team
        # already comfortably ahead. When tied, BOTH teams are eligible at
        # once and each uses the smaller _TIED chance instead of _TRAILING
        # (see the constants' comment).
        is_tied = home_score_at_siren == away_score_at_siren
        shot_chance = AFTER_SIREN_SHOT_CHANCE_TIED if is_tied else AFTER_SIREN_SHOT_CHANCE_TRAILING

        eligible_teams = []
        if home_score_at_siren >= away_score_at_siren - AFTER_SIREN_MAX_DEFICIT and home_score_at_siren <= away_score_at_siren:
            eligible_teams.append((home_players, home_result, home_team_name))
        if away_score_at_siren >= home_score_at_siren - AFTER_SIREN_MAX_DEFICIT and away_score_at_siren <= home_score_at_siren:
            eligible_teams.append((away_players, away_result, away_team_name))
        # Both scores equal - both entries above already include the tied
        # team (>= and <= both hold), so eligible_teams naturally ends up
        # with both sides in that case without any extra handling.

        # Rolled in a random order, rather than always home-then-away, so
        # which team's after-siren shot "counts as happening first"
        # (relevant if both are eligible AND both hit - only one team can
        # actually have the real final say) isn't systematically biased
        # toward either side.
        rng.shuffle(eligible_teams)
        ordinal = 0
        for players, result, team_name in eligible_teams:
            event = _simulate_after_siren_shot(players, quarter, quarter_lengths, rng, result, team_name, events, shot_chance, ordinal=ordinal)
            if event is not None:
                after_siren_events.append(event)
                ordinal += 1

    def apply_stat(stat_name, generator, *generator_args):
        for players, result in ((home_players, home_result), (away_players, away_result)):
            values = generator(players, *generator_args)
            for player_id, value in values.items():
                setattr(result.stat_lines[player_id], stat_name, value)

    def mark_weight(p):
        # Natural position only - marking contests happen all over the
        # ground, unlike spoils, so this doesn't need a slot-based demotion
        return MARK_WEIGHT.get(p.position, DEFAULT_MARK_WEIGHT)

    def tackle_weight(p):
        return TACKLE_TIER_WEIGHT[_position_group_tier(p, TACKLE_TIER_POSITIONS)]

    # Disposals need each team's OWN/OPPONENT strengths (like shots), not
    # just a flat league_avg_ovr comparison like the other tiered stats -
    # doesn't fit apply_stat's generic per-team-independent signature.
    for players, result, own_str, opp_str in (
        (home_players, home_result, home_strengths, away_strengths),
        (away_players, away_result, away_strengths, home_strengths),
    ):
        values = _simulate_disposals(players, own_str, opp_str, variance, rng)
        for player_id, value in values.items():
            result.stat_lines[player_id].disposals = value

    apply_stat("marks", _simulate_tiered_stat, league_avg_ovr, variance, rng,
               mark_weight, BASE_MARKS,
               MARK_OVR_SENSITIVITY_ABOVE_AVG, MARK_OVR_SENSITIVITY_BELOW_AVG, 0.5)

    # Marks and goals are both independently-rolled from disposals, but in
    # real AFL every mark and every goal IS also a disposal - a player can
    # never have more marks+goals than total disposals. Goals are protected
    # first (rarer, more meaningful than marks), marks get clamped down to
    # whatever's left.
    for result in (home_result, away_result):
        for line in result.stat_lines.values():
            marks_ceiling = max(0, line.disposals - line.goals)
            if line.marks > marks_ceiling:
                line.marks = marks_ceiling

    apply_stat("tackles", _simulate_tiered_stat, league_avg_ovr, variance, rng,
               tackle_weight, BASE_TACKLES,
               TACKLE_OVR_SENSITIVITY_ABOVE_AVG, TACKLE_OVR_SENSITIVITY_BELOW_AVG, 0.5)
    apply_stat("spoils", _simulate_tiered_stat, league_avg_ovr, variance, rng,
               spoil_weight, BASE_SPOILS,
               SPOIL_OVR_SENSITIVITY_ABOVE_AVG, SPOIL_OVR_SENSITIVITY_BELOW_AVG, 0.2)
    apply_stat("hitouts", _simulate_hitouts, league_avg_ovr, variance, rng)

    if build_events:
        for team_name, players in ((home_team_name, home_players), (away_team_name, away_players)):
            for p in players:
                if rng.random() < INJURY_CHANCE_PER_PLAYER:
                    category, diagnosis, recovery_weeks = _generate_injury(p, rng)
                    quarter = rng.choice((1, 2, 3, 4))
                    minute = rng.uniform(0, quarter_lengths[quarter - 1])
                    match_minute = sum(quarter_lengths[:quarter - 1]) + minute
                    events.append(MatchEvent(
                        "injury", quarter, minute, match_minute, team_name, p,
                        injury_category=category, injury_diagnosis=diagnosis, injury_recovery_weeks=recovery_weeks,
                    ))
                if rng.random() < REPORT_CHANCE_PER_PLAYER:
                    category, charge, suspension_games = _generate_report(p, rng)
                    quarter = rng.choice((1, 2, 3, 4))
                    minute = rng.uniform(0, quarter_lengths[quarter - 1])
                    match_minute = sum(quarter_lengths[:quarter - 1]) + minute
                    events.append(MatchEvent(
                        "report", quarter, minute, match_minute, team_name, p,
                        report_category=category, report_charge=charge, report_suspension_games=suspension_games,
                    ))
        events.sort(key=lambda e: e.match_minute)
        _enforce_minimum_event_gap(events, quarter_lengths)
        events.sort(key=lambda e: e.match_minute)

        _mark_siren_beater_winners(events, home_team_name, away_team_name)

    return MatchResult(home_result, away_result), events, quarter_lengths


def simulate_match(home_team_name, home_lineup, away_team_name, away_lineup,
                    league_avg_ovr, variance=DEFAULT_VARIANCE, rng=None, home_ground_advantage=True):
    """Simulate one AFL match between two complete (23-player) lineups.

    Args:
        home_team_name / away_team_name: display names
        home_lineup / away_lineup: list of (player_id, name, position, overall_rating, slot)
        league_avg_ovr: current league-average OVR among rostered players,
            computed live by the caller - never hardcoded
        variance: match_sim_variance setting value (higher = more upset-prone)
        rng: optional random.Random instance (for deterministic testing)
        home_ground_advantage: apply HOME_GROUND_ADVANTAGE to the home team's
            strength (default True) - set False to simulate a neutral-venue
            match with no home team benefit

    Returns:
        MatchResult
    """
    rng = rng or random.Random()
    result, _, _ = _simulate_match_core(home_team_name, home_lineup, away_team_name, away_lineup,
                                         league_avg_ovr, variance, rng, build_events=False,
                                         home_ground_advantage=home_ground_advantage)
    return result


def simulate_match_with_events(home_team_name, home_lineup, away_team_name, away_lineup,
                                league_avg_ovr, variance=DEFAULT_VARIANCE, rng=None, home_ground_advantage=True):
    """Same simulation as simulate_match(), but also returns an ordered
    event timeline (goals, behinds, injuries) for a live match feed to post
    incrementally - see the Live Match Feed design doc, sections 02-03.

    Returns:
        (MatchResult, list[MatchEvent], list[float]) - events sorted by
        match_minute; quarter_lengths is each quarter's rolled length in
        minutes (index 0 = Q1), needed to pace the gap from a quarter's last
        event to its actual end.
    """
    rng = rng or random.Random()
    return _simulate_match_core(home_team_name, home_lineup, away_team_name, away_lineup,
                                 league_avg_ovr, variance, rng, build_events=True,
                                 home_ground_advantage=home_ground_advantage)


def format_score(goals, behinds):
    """AFL score display format, e.g. 14.9 (93)."""
    return f"{goals}.{behinds} ({goals * 6 + behinds})"
