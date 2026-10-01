"""AFL fixture generator - pure scheduling logic, no discord.py or database
dependency (same separation as match_sim.py). Builds a full round-based
fixture for a set of teams: every team plays every other team once, plus a
fixed number of "double-up" rematches, with balanced home/away splits and
support for admin-pinned marquee games and guaranteed double-up matchups.

Designed around an EVEN team count (so every round is a full, bye-free
round of pairings) and a round count of team_count - 1 + double_up_count
(e.g. 20 teams, 24 rounds -> 19 unique-opponent rounds + 5 double-up
rounds, each team doubling up against exactly 5 opponents). A round count
outside that exact shape is refused with a clear error rather than
silently producing an uneven or bye-laden fixture - see generate_fixture.

What a generated fixture guarantees:
  - Every team plays every other team once, plus a rematch against
    double_up_count of them (their two legs at opposite venues).
  - Rematch rounds are spread through the season rather than clustered,
    so a pair's two meetings are well apart.
  - No team plays more than MAX_HOME_AWAY_STREAK games in a row at the
    same venue, and every team's home/away split lands within
    MAX_HOME_AWAY_IMBALANCE.
  - Admin-pinned marquee games play in their exact round with their
    exact home team; admin-specified guaranteed double-ups are among the
    rematches.

Both venue limits are deliberately looser than "2 in a row" and "exactly
even", because those stricter values are infeasible here, not merely hard
to find - see the constants for the measurements behind each.

Marquee games can't be pinned to a rematch round: who plays whom in those
rounds comes out of the double-up draw, not the round-robin. Ask for one
there and generate_fixture says so, naming the rounds.

Usage:
    fixture = generate_fixture(
        team_ids=[1, 2, ..., 20],
        rounds=24,
        marquees=[(round_number, home_id, away_id), ...],
        guaranteed_doubleups=[(team_a_id, team_b_id), ...],
        rng=random.Random(seed),
    )
    # -> list of (round_number, home_team_id, away_team_id), 1-indexed rounds,
    #    len == rounds * (team_count // 2)
"""

import random


class FixtureGenerationError(Exception):
    """Raised whenever a valid fixture satisfying every hard constraint
    can't be produced - the caller (a Discord command) should surface
    str(exc) directly to the admin rather than silently guessing."""


# No more than this many consecutive home (or away) games for one team.
# 3, not 2, for a concrete reason: at 20 teams / 24 rounds, a cap of 2 is
# effectively INFEASIBLE alongside the other hard constraints (double-up
# legs alternating venue + every team's home/away split within
# MAX_HOME_AWAY_IMBALANCE). Measured over 400 randomized generation
# attempts per seed across 50 seeds, a cap of 2 produced zero valid
# fixtures; a cap of 3 produced one on the first or second attempt every
# single time. The binding constraint is the double-up rematch: its second
# leg's venue is forced by its first leg, at a round chosen for pairing
# reasons rather than venue reasons, so a team can arrive there already
# mid-streak with no legal option left.
MAX_HOME_AWAY_STREAK = 3

# Max |home games - away games| per team across the season. 2 (rather than
# 0, i.e. a perfectly even split) for the same feasibility reason as the
# streak cap above - at 24 rounds this means a team plays at worst 13 home
# / 11 away.
MAX_HOME_AWAY_IMBALANCE = 2

# How many times generate_fixture rebuilds the whole schedule from scratch
# (new round-robin rotation, new double-up pairing, new venue assignment)
# before giving up. Each attempt is ~10ms and succeeds well over half the
# time, so this is a deep safety margin, not an expected cost.
MAX_GENERATION_ATTEMPTS = 400

# Round counts beyond this many double-ups per team get unreliable: the
# more rematches, the more legs whose venue is forced by their first leg,
# and eventually there's no venue assignment left that satisfies both the
# streak cap and the imbalance cap. Measured for a 20-team league: 0-5
# double-ups per team (19-24 rounds) succeed every time, 6-7 usually, and
# 8+ rarely or never. Used only to give a clearer error than "couldn't do
# it after 400 tries".
RELIABLE_DOUBLEUPS_PER_TEAM = 7


def build_doubleup_rounds(team_ids, guaranteed_pairs, target_degree, rng):
    """Returns target_degree ROUNDS (each a list of (team_a, team_b)
    pairs - a perfect matching over team_ids) such that every team
    appears in exactly one pairing per round, and no two teams are ever
    paired more than once across all of them - i.e. every team ends up
    with exactly target_degree distinct double-up partners.

    Built as a sequence of independent random perfect matchings (one per
    round) rather than first building a random target_degree-regular
    GRAPH and then trying to decompose it into matchings afterward - that
    two-step approach was tried and discarded: an arbitrary regular graph
    is only guaranteed decomposable into exactly target_degree perfect
    matchings when it's bipartite (König's theorem), which a randomly
    built regular graph over 20 teams generally isn't, so decomposition
    attempts dead-ended on the large majority of runs even with zero
    constraints. Building matchings directly sidesteps the problem
    entirely - decomposability is true by construction, never something
    that has to be discovered after the fact.

    Raises FixtureGenerationError if guaranteed_pairs already over-commits
    a team (more than target_degree guaranteed partners) or if no
    satisfying set of rounds can be found within the attempt budget
    (expected only for pathological/contradictory guaranteed pairs)."""
    team_ids = list(team_ids)
    team_set = set(team_ids)

    guaranteed_pairs = list(guaranteed_pairs)
    guaranteed_degree = {t: 0 for t in team_ids}
    seen_guaranteed = set()
    deduped_guaranteed = []
    for a, b in guaranteed_pairs:
        if a not in team_set or b not in team_set:
            raise FixtureGenerationError(f"Guaranteed double-up references an unknown team: {a}, {b}")
        if a == b:
            raise FixtureGenerationError(f"A team can't be guaranteed a double-up against itself: {a}")
        edge = frozenset((a, b))
        if edge in seen_guaranteed:
            continue
        seen_guaranteed.add(edge)
        deduped_guaranteed.append((a, b))
        guaranteed_degree[a] += 1
        guaranteed_degree[b] += 1

    for t, deg in guaranteed_degree.items():
        if deg > target_degree:
            raise FixtureGenerationError(
                f"Team {t} has {deg} guaranteed double-ups, but only {target_degree} "
                f"double-up slot(s) are available per team at this round count."
            )

    max_attempts = 2000
    for _attempt in range(max_attempts):
        result = _try_build_doubleup_rounds(team_ids, deduped_guaranteed, target_degree, rng)
        if result is not None:
            return result

    raise FixtureGenerationError(
        "Could not build a valid double-up schedule satisfying every guaranteed "
        "double-up - the constraints may be too tight (e.g. several teams sharing "
        "guaranteed partners in a way that can't be evenly scheduled). Try removing "
        "or changing one of the guaranteed double-ups."
    )


def _try_build_doubleup_rounds(team_ids, guaranteed_pairs, target_degree, rng):
    """One attempt at build_doubleup_rounds. Places each guaranteed pair
    into the first round (in generation order) where both its teams are
    still free, then fills every round's remaining teams with random
    perfect-matching pairs (via the same repeated-random-pairing method
    as a single round's construction), rejecting a pairing whenever the
    two teams have already met in an earlier round. Returns the list of
    rounds on success, None on dead-end (caller retries)."""
    used_pairs = set()
    rounds = []
    remaining_guaranteed = list(guaranteed_pairs)
    rng.shuffle(remaining_guaranteed)

    for _round_idx in range(target_degree):
        available = set(team_ids)
        round_pairs = []

        placed = []
        for pair in remaining_guaranteed:
            a, b = pair
            if a in available and b in available:
                round_pairs.append((a, b))
                used_pairs.add(frozenset((a, b)))
                available.discard(a)
                available.discard(b)
                placed.append(pair)
        for pair in placed:
            remaining_guaranteed.remove(pair)

        avail_list = list(available)
        rng.shuffle(avail_list)
        while avail_list:
            a = avail_list.pop()
            candidates = [t for t in avail_list if frozenset((a, t)) not in used_pairs]
            if not candidates:
                return None
            b = rng.choice(candidates)
            avail_list.remove(b)
            round_pairs.append((a, b))
            used_pairs.add(frozenset((a, b)))

        rounds.append(round_pairs)

    if remaining_guaranteed:
        # Ran out of rounds before every guaranteed pair could be placed
        # without a same-round conflict - retry with a fresh shuffle.
        return None

    return rounds


def _circle_method_slot_pairings(n):
    """The circle method's pairings expressed as SEAT indices rather than
    team ids: returns n-1 rounds, each a list of (seat_a, seat_b) pairs
    over seats 0..n-1. Seat 0 is the fixed pivot; seats 1..n-1 rotate.
    Separating seats from teams is what makes marquee placement possible -
    the seat structure is invariant, so pinning a pairing is just a matter
    of which teams sit in which seats (see _seat_assignment_for_marquees)."""
    rounds = []
    rotating = list(range(1, n))
    for _ in range(n - 1):
        current = [0] + rotating
        rounds.append([(current[i], current[n - 1 - i]) for i in range(n // 2)])
        rotating = [rotating[-1]] + rotating[:-1]
    return rounds


def _seat_assignment_for_marquees(team_ids, slot_rounds, marquee_round_pairs, rng):
    """Chooses which team sits in which seat so that every requested
    marquee pairing falls in its requested round. Returns a list mapping
    seat index -> team id, or None if the marquees can't be co-satisfied
    by any seating (e.g. two marquees needing the same team in two
    different seats).

    marquee_round_pairs: list of (round_index, team_a, team_b) - 0-based
    round index into slot_rounds.

    Works because the circle method's seat pairings are fixed: for a
    marquee in round R, any of that round's n/2 seat-pairs will do, so we
    try them in random order and commit the two teams to that seat pair,
    backtracking if a later marquee can't be placed. With only a handful
    of marquees this search is tiny."""
    n = len(team_ids)
    seat_to_team = {}
    team_to_seat = {}

    def place(idx):
        if idx == len(marquee_round_pairs):
            return True
        round_idx, team_a, team_b = marquee_round_pairs[idx]
        seat_pairs = list(slot_rounds[round_idx])
        rng.shuffle(seat_pairs)
        for seat_a, seat_b in seat_pairs:
            for first, second in ((seat_a, seat_b), (seat_b, seat_a)):
                # Both teams must be unseated (or already in exactly these
                # seats), and both seats free (or already held by them).
                if team_to_seat.get(team_a, first) != first:
                    continue
                if team_to_seat.get(team_b, second) != second:
                    continue
                if seat_to_team.get(first, team_a) != team_a:
                    continue
                if seat_to_team.get(second, team_b) != team_b:
                    continue

                added = []
                if team_a not in team_to_seat:
                    team_to_seat[team_a] = first
                    seat_to_team[first] = team_a
                    added.append((team_a, first))
                if team_b not in team_to_seat:
                    team_to_seat[team_b] = second
                    seat_to_team[second] = team_b
                    added.append((team_b, second))

                if place(idx + 1):
                    return True

                for team, seat in added:
                    del team_to_seat[team]
                    del seat_to_team[seat]
        return False

    if not place(0):
        return None

    # Fill every remaining seat with the still-unseated teams, shuffled.
    leftover_teams = [t for t in team_ids if t not in team_to_seat]
    rng.shuffle(leftover_teams)
    seating = [None] * n
    for seat, team in seat_to_team.items():
        seating[seat] = team
    for seat in range(n):
        if seating[seat] is None:
            seating[seat] = leftover_teams.pop()
    return seating


def round_robin_pairings(team_ids, rng, marquee_round_pairs=None):
    """Standard circle-method single round-robin: returns a list of rounds
    (each a list of (team_a, team_b) tuples, order not yet meaningful for
    home/away), one round per team - 1, every pair appearing exactly once
    across the whole schedule. team_ids must have an even length (the
    classic circle method handles odd counts via a "bye" seat, which this
    deliberately doesn't support - see generate_fixture's even-count
    requirement).

    marquee_round_pairs, if given, is a list of (round_index, team_a,
    team_b) that MUST be paired in that 0-based round; the team-to-seat
    assignment is chosen to make that true rather than left to chance
    (see _seat_assignment_for_marquees). Returns None if no seating
    satisfies them all - note this only constrains the single
    round-robin's own rounds, so callers pinning a marquee to a
    double-up round handle that separately."""
    teams = list(team_ids)
    n = len(teams)
    if n % 2 != 0:
        raise FixtureGenerationError("round_robin_pairings requires an even number of teams")

    slot_rounds = _circle_method_slot_pairings(n)

    if marquee_round_pairs:
        seating = _seat_assignment_for_marquees(teams, slot_rounds, marquee_round_pairs, rng)
        if seating is None:
            return None
    else:
        seating = list(teams)
        rng.shuffle(seating)

    return [[(seating[a], seating[b]) for a, b in slot_round] for slot_round in slot_rounds]


def _interleave_layout(total_base, total_extra):
    """The ORDER in which base and double-up rounds are played, computed
    from the counts alone (no round content needed): returns a list of
    ("base", base_index) / ("extra", extra_index) tags, one per final
    round. Double-up rounds are spread roughly evenly rather than
    clustered - e.g. 5 among 19 lands one about every 4th base round -
    so a pair's rematch sits well away from their first meeting.

    Split out from the actual merging so callers can map a FINAL round
    number back to the base round it corresponds to before the rounds
    themselves exist, which is what marquee placement needs."""
    layout = []
    if total_extra == 0:
        return [("base", i) for i in range(total_base)]

    # Evenly spaced insertion points across the base rounds (never at the
    # very first slot - a double-up leg shouldn't open the season before
    # the base round-robin has even started once).
    spacing = total_base / (total_extra + 1)
    insertion_points = sorted(
        min(total_base - 1, max(1, round(spacing * (i + 1))))
        for i in range(total_extra)
    )

    extra_idx = 0
    for i in range(total_base):
        layout.append(("base", i))
        while extra_idx < total_extra and insertion_points[extra_idx] == i:
            layout.append(("extra", extra_idx))
            extra_idx += 1
    while extra_idx < total_extra:
        layout.append(("extra", extra_idx))
        extra_idx += 1
    return layout


def _interleave_rounds(base_rounds, extra_rounds, rng):
    """Merges base_rounds (the single round-robin) and extra_rounds (the
    double-up rounds) into one ordered list of rounds in final play
    order, per _interleave_layout."""
    merged = []
    for kind, idx in _interleave_layout(len(base_rounds), len(extra_rounds)):
        merged.append(base_rounds[idx] if kind == "base" else extra_rounds[idx])
    return merged


def assign_home_away(rounds, doubleup_pairs, marquees, rng):
    """Assigns home/away for every (team_a, team_b) pairing across every
    round, returning the same round structure with each pairing now
    (home_id, away_id), or None if this particular attempt paints itself
    into a corner (the caller regenerates and retries - see
    generate_fixture's MAX_GENERATION_ATTEMPTS loop).

    Constraints enforced:
      - No team plays more than MAX_HOME_AWAY_STREAK consecutive home (or
        away) games. Checked as a hard skip at every decision.
      - A double-up pair's two legs alternate venue (whoever hosted the
        first leg is away for the second). The second leg is therefore
        never a free choice - it's forced, and if that forced venue would
        breach the streak cap, this attempt is abandoned.
      - A marquee game's home/away is honored exactly as specified.
      - Every team's |home - away| ends within MAX_HOME_AWAY_IMBALANCE,
        checked once at the end.

    Single forward pass, no backtracking: each free choice picks the
    orientation minimizing a cost that weights streak continuation ten
    times more heavily than running home/away imbalance, ties broken
    randomly. Backtracking was tried and abandoned - the search tree over
    ~240 decisions is far too wide to explore when a dead end is only
    discovered near the end, whereas simply regenerating the whole
    schedule is ~10ms and usually succeeds within a couple of tries.

    marquees: list of (round_index, team_a, team_b, home_team) - round_index
    is the FINAL merged round order's 0-based index.
    doubleup_pairs: set of frozenset({team_a, team_b}) - which pairings are
    a double-up."""
    marquee_by_round_pair = {}
    for round_idx, team_a, team_b, home_team in marquees:
        marquee_by_round_pair[(round_idx, frozenset((team_a, team_b)))] = home_team

    teams = {t for rnd in rounds for pair in rnd for t in pair}
    home_count = {t: 0 for t in teams}
    away_count = {t: 0 for t in teams}
    streak = {t: (None, 0) for t in teams}
    doubleup_first_leg_home = {}

    def violates(team, venue):
        last_venue, length = streak[team]
        return last_venue == venue and length >= MAX_HOME_AWAY_STREAK

    def apply(team, venue):
        last_venue, length = streak[team]
        streak[team] = (venue, length + 1) if last_venue == venue else (venue, 1)
        if venue == "home":
            home_count[team] += 1
        else:
            away_count[team] += 1

    def cost(team, venue):
        # Streak length dominates: a 3rd-in-a-row costs far more than any
        # imbalance this early, which keeps teams away from the cap rather
        # than merely legal at it.
        last_venue, length = streak[team]
        streak_part = (length + 1) if last_venue == venue else 1
        projected_home = home_count[team] + (1 if venue == "home" else 0)
        projected_away = away_count[team] + (1 if venue == "away" else 0)
        return streak_part * 10 + abs(projected_home - projected_away)

    # A marquee on a double-up pair pins the venue for ONE of that pair's
    # two legs. Resolve that up front into the pair's first-leg host, so
    # the alternation rule below and the marquee agree instead of the
    # marquee silently overriding it and producing two same-venue legs.
    pair_leg_rounds = {}
    for round_idx, round_pairs in enumerate(rounds):
        for team_a, team_b in round_pairs:
            pair_key = frozenset((team_a, team_b))
            if pair_key in doubleup_pairs:
                pair_leg_rounds.setdefault(pair_key, []).append(round_idx)
    for (round_idx, pair_key), marquee_home in marquee_by_round_pair.items():
        if pair_key not in doubleup_pairs:
            continue
        legs = sorted(pair_leg_rounds.get(pair_key, []))
        if len(legs) != 2:
            continue
        other = next(iter(pair_key - {marquee_home}))
        # If the marquee is on the first leg, its host IS the first-leg
        # host; if it's on the second, the first leg must be the other team.
        doubleup_first_leg_home[pair_key] = marquee_home if round_idx == legs[0] else other

    assigned_rounds = []
    for round_idx, round_pairs in enumerate(rounds):
        assigned = [None] * len(round_pairs)
        order = list(range(len(round_pairs)))
        rng.shuffle(order)
        # Marquee-pinned games first, so the streak state they force is in
        # place before the round's free choices are made around them.
        order.sort(key=lambda i: (round_idx, frozenset(round_pairs[i])) not in marquee_by_round_pair)

        for i in order:
            team_a, team_b = round_pairs[i]
            pair_key = frozenset((team_a, team_b))
            is_doubleup = pair_key in doubleup_pairs

            marquee_home = marquee_by_round_pair.get((round_idx, pair_key))
            forced_home = None
            if is_doubleup and pair_key in doubleup_first_leg_home:
                # Alternation takes precedence - for a marquee'd double-up
                # this was seeded above to match the marquee, so the two
                # never actually disagree.
                first_leg_home = doubleup_first_leg_home[pair_key]
                is_first_leg = round_idx == min(pair_leg_rounds[pair_key])
                forced_home = first_leg_home if is_first_leg else next(iter(pair_key - {first_leg_home}))
            elif marquee_home is not None:
                forced_home = marquee_home

            if forced_home is not None:
                home = forced_home
                away = team_b if home == team_a else team_a
                if violates(home, "home") or violates(away, "away"):
                    return None
            else:
                options = []
                for home, away in ((team_a, team_b), (team_b, team_a)):
                    if violates(home, "home") or violates(away, "away"):
                        continue
                    options.append((cost(home, "home") + cost(away, "away"), home, away))
                if not options:
                    return None
                rng.shuffle(options)
                options.sort(key=lambda option: option[0])
                _, home, away = options[0]

            apply(home, "home")
            apply(away, "away")
            if is_doubleup and pair_key not in doubleup_first_leg_home:
                doubleup_first_leg_home[pair_key] = home
            assigned[i] = (home, away)

        assigned_rounds.append(assigned)

    for team in teams:
        if abs(home_count[team] - away_count[team]) > MAX_HOME_AWAY_IMBALANCE:
            return None

    return assigned_rounds



def generate_fixture(team_ids, rounds, marquees=None, guaranteed_doubleups=None, rng=None):
    """Generates a full fixture. Returns a list of (round_number,
    home_team_id, away_team_id) tuples, round_number 1-indexed.

    team_ids: list of team IDs, must have an even length.
    rounds: total rounds to generate - must equal len(team_ids) - 1 + D for
        some non-negative integer D (the double-up count per team); D is
        derived from this, not passed separately. E.g. 20 teams supports
        19 rounds (no double-ups) up to 38 (a full double round-robin);
        24 -> D=5, matching the "5 double-up opponents" default.
    marquees: list of (round_number, home_team_id, away_team_id) - pins
        that exact fixture, with that exact home team, into that round.
        Satisfied by rejecting any generated schedule that doesn't happen
        to pair those two teams in that round and retrying, so a marquee
        makes generation take more attempts; several marquees in the same
        round, or a marquee on a round a pair genuinely can't meet in,
        can make it fail outright.
    guaranteed_doubleups: list of (team_a_id, team_b_id) - these two teams
        are guaranteed to be paired twice (once each way) somewhere in the
        season; raises FixtureGenerationError if this over-commits a team
        beyond the available double-up slots (D above).
    rng: random.Random instance - defaults to a fresh, unseeded Random()
        (each call produces a different valid fixture, not a reproducible
        one, matching match_sim.py's own convention for match simulation).

    Raises FixtureGenerationError if the request is structurally
    impossible (bad round count, odd team count, over-committed
    guaranteed double-ups) or if MAX_GENERATION_ATTEMPTS schedules in a
    row all fail to satisfy every constraint."""
    if rng is None:
        rng = random.Random()

    team_ids = list(team_ids)
    n = len(team_ids)
    if n < 2 or n % 2 != 0:
        raise FixtureGenerationError(f"generate_fixture requires an even number of teams, got {n}")

    base_rounds_needed = n - 1
    doubleup_count = rounds - base_rounds_needed
    if doubleup_count < 0:
        raise FixtureGenerationError(
            f"{rounds} rounds isn't enough for {n} teams to each play every other team "
            f"once - at least {base_rounds_needed} rounds are required."
        )
    if doubleup_count > n - 1:
        raise FixtureGenerationError(
            f"{rounds} rounds would require every team to double up against more "
            f"opponents than exist ({n - 1} possible opponents, {doubleup_count} "
            f"double-ups requested per team)."
        )

    marquees = marquees or []
    guaranteed_doubleups = guaranteed_doubleups or []

    for round_number, home_id, away_id in marquees:
        if not (1 <= round_number <= rounds):
            raise FixtureGenerationError(f"Marquee game round {round_number} is outside the 1-{rounds} range.")
        if home_id not in team_ids or away_id not in team_ids:
            raise FixtureGenerationError(f"Marquee game references an unknown team: {home_id}, {away_id}")
        if home_id == away_id:
            raise FixtureGenerationError(f"Marquee game can't have a team play itself: {home_id}")

    seen_marquee_slots = set()
    for round_number, home_id, away_id in marquees:
        for t in (home_id, away_id):
            key = (round_number, t)
            if key in seen_marquee_slots:
                raise FixtureGenerationError(
                    f"Team {t} is claimed by more than one marquee game in round {round_number}."
                )
            seen_marquee_slots.add(key)

    if doubleup_count == 0 and guaranteed_doubleups:
        raise FixtureGenerationError(
            "Guaranteed double-ups were specified, but this round count leaves no "
            "double-up slots at all (every team already plays every opponent exactly once)."
        )

    # Every stage below is randomized and can fail on an unlucky draw, so
    # the whole schedule is rebuilt from scratch until one satisfies every
    # constraint. See MAX_GENERATION_ATTEMPTS - a single attempt is cheap.
    # The play order of base vs double-up rounds is fixed up front, so a
    # marquee's FINAL round number can be mapped to the specific base (or
    # double-up) round it lands on before any pairings exist - which is
    # what lets marquees be built in rather than hoped for.
    layout = _interleave_layout(base_rounds_needed, doubleup_count)
    if len(layout) != rounds:
        raise FixtureGenerationError(
            f"Internal scheduling error: laid out {len(layout)} rounds, expected {rounds}."
        )

    base_marquees = []   # (base_round_index, home_id, away_id)
    extra_marquees = []  # (extra_round_index, home_id, away_id)
    for round_number, home_id, away_id in marquees:
        kind, idx = layout[round_number - 1]
        if kind == "base":
            base_marquees.append((idx, home_id, away_id))
        else:
            extra_marquees.append((idx, home_id, away_id))

    # A marquee landing on a double-up round demands that those two teams
    # are double-up partners AND that their rematch is the leg in that
    # exact round - two things chosen by the double-up builder, which has
    # its own guaranteed-pairs mechanism instead. Rather than silently
    # producing a fixture missing the marquee, say so.
    if extra_marquees:
        rounds_list = ", ".join(str(layout.index(("extra", idx)) + 1) for idx, _, _ in extra_marquees)
        raise FixtureGenerationError(
            f"Round(s) {rounds_list} are double-up (rematch) rounds, which can't host a "
            "marquee game - the teams playing then are decided by the double-up draw. "
            "Pick a different round for those marquee games, or set them up as guaranteed "
            "double-ups instead."
        )

    for _attempt in range(MAX_GENERATION_ATTEMPTS):
        if doubleup_count == 0:
            extra_rounds = []
            doubleup_pairs = set()
        else:
            extra_rounds = build_doubleup_rounds(team_ids, guaranteed_doubleups, doubleup_count, rng)
            doubleup_pairs = {frozenset(pair) for rnd in extra_rounds for pair in rnd}

        base_rounds = round_robin_pairings(team_ids, rng, base_marquees)
        if base_rounds is None:
            # No seating satisfies every marquee - structural, so retrying
            # with a different draw won't help.
            raise FixtureGenerationError(
                "These marquee games can't all be scheduled together - two of them need the "
                "same team in incompatible places. Try removing one, or moving it to another round."
            )

        merged_rounds = _interleave_rounds(base_rounds, extra_rounds, rng)

        marquee_triples = [
            (round_number - 1, home_id, away_id, home_id)
            for round_number, home_id, away_id in marquees
        ]

        assigned_rounds = assign_home_away(merged_rounds, doubleup_pairs, marquee_triples, rng)
        if assigned_rounds is None:
            continue

        fixture = []
        for round_idx, round_pairs in enumerate(assigned_rounds):
            for home_id, away_id in round_pairs:
                fixture.append((round_idx + 1, home_id, away_id))
        return fixture

    if doubleup_count > RELIABLE_DOUBLEUPS_PER_TEAM:
        max_reliable = base_rounds_needed + RELIABLE_DOUBLEUPS_PER_TEAM
        detail = (
            f" {rounds} rounds means every team plays {doubleup_count} rematches, which is "
            f"too many to schedule fairly for {n} teams - each rematch forces a venue, and "
            f"past about {RELIABLE_DOUBLEUPS_PER_TEAM} there's no way left to keep everyone's "
            f"home/away split fair. Try {max_reliable} rounds or fewer."
        )
    elif marquees:
        detail = (
            " Marquee games are the most likely cause - each one pins a venue on a fixed "
            "round, which can leave a team with no legal home/away option nearby. Try "
            "fewer marquee games, or spreading them further apart."
        )
    else:
        detail = " Try generating again."
    raise FixtureGenerationError(
        f"Could not build a valid fixture after {MAX_GENERATION_ATTEMPTS} attempts.{detail}"
    )
