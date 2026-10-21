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
    double_up_count of them (their two meetings at opposite venues).
  - All fresh matchups come first: rounds 1 to team_count - 1 are a full
    single round-robin, and every rematch is in the rounds after that.
  - No team plays more than MAX_HOME_AWAY_STREAK games in a row at the
    same venue, and every team's home/away split lands within
    MAX_HOME_AWAY_IMBALANCE - then _even_out_home_away evens it out
    further, which in testing gets every team an exact split (or one apart
    with an odd number of games) at every round count from 19 to 28.
  - Admin-pinned marquee games play in their exact round with their
    exact home team; admin-specified guaranteed double-ups are among the
    rematches, each in a random rematch round other than the final one
    (unless a team has more of them than that leaves room for).
  - A pair's rematch is at least MIN_REMATCH_GAP rounds after their first
    meeting (relaxed only if a setup makes that impossible).

And, as a best effort rather than a guarantee: when last season's ladder
is supplied, rematch opponents lean toward teams that finished in the same
third of the ladder (see rematch_group_targets).

Both venue limits are deliberately looser than "2 in a row" and "exactly
even", because those stricter values are infeasible here, not merely hard
to find - see the constants for the measurements behind each.

Marquee games can't be pinned to a rematch round: who plays whom in those
rounds comes out of the double-up draw, not the round-robin. Ask for one
there and generate_fixture says so, naming the rounds.

Timeslots are a separate step afterwards (assign_timeslots): every game
gets one of TIMESLOTS, with the prime-time slots tending to go to games
between teams that finished high on last season's ladder. Grounds come
after that (assign_grounds), run as generate_fixture's `accept` check so a
fixture with too many games at one ground is swapped for another.

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


class FixtureRejected(FixtureGenerationError):
    """Raised by a generate_fixture `accept` check to reject one generated
    fixture without giving up - e.g. assign_grounds when a round has more
    games at one ground than fit. generate_fixture just tries another."""


# No more than this many consecutive home (or away) games for one team.
# 3, not 2, for a concrete reason: at 20 teams / 24 rounds, a cap of 2 is
# effectively INFEASIBLE alongside the other hard constraints (rematches
# alternating venue + every team's home/away split within
# MAX_HOME_AWAY_IMBALANCE). Measured with the current algorithm over 30
# seeds: a cap of 2 failed every time at 24 rounds and 29 of 30 at 22,
# while a cap of 3 succeeded every time from 19 to 32 rounds. The binding
# constraint is the rematch: its venue is forced by the first meeting's,
# so each team carries several games whose venue isn't a free choice.
MAX_HOME_AWAY_STREAK = 3

# Max |home games - away games| per team across the season. 2 (rather than
# 0, i.e. a perfectly even split) for the same feasibility reason as the
# streak cap above - at 24 rounds this means a team plays at worst 13 home
# / 11 away.
MAX_HOME_AWAY_IMBALANCE = 2

# How many times generate_fixture rebuilds the whole schedule from scratch
# (new round-robin rotation, new double-up pairing, new venue assignment)
# before giving up. Each attempt takes a few milliseconds and, at 24
# rounds, succeeds about half the time, so this is a deep safety margin,
# not an expected cost.
MAX_GENERATION_ATTEMPTS = 400

# generate_fixture reuses one set of rounds (round-robin + rematch draw)
# for this many attempts at the venue step before building new ones.
ATTEMPTS_PER_REMATCH_DRAW = 10

# Minimum distance, in rounds, between a pair's first meeting and its
# rematch - 8 means e.g. round 12 then round 20 at the closest. The rematch
# draw only picks pairs that met early enough (see _draw_rematches). If a
# setup makes that impossible - say a forced double-up whose first meeting
# is pinned late by a marquee game - it's relaxed a round at a time rather
# than failing.
MIN_REMATCH_GAP = 8

# Round counts beyond this many double-ups per team get unreliable: the
# rematch draw (a fresh random pairing per rematch round, each team needing
# an opponent it hasn't been drawn against yet) starts running out of
# unused opponents. Measured for a 20-team league: 0-13 double-ups per team
# (19-32 rounds) succeed every time, 14 about half the time, 15+ never.
# Used only to give a clearer error than "couldn't do it".
RELIABLE_DOUBLEUPS_PER_TEAM = 13


# With a ladder supplied, how many complete rematch draws build_doubleup_rounds
# makes (each then improved by _improve_group_balance) before keeping the
# one closest to every team's group targets.
GROUP_WEIGHTING_CANDIDATES = 8

# Swap attempts _improve_group_balance makes per draw.
GROUP_BALANCE_STEPS = 3000

# With a ladder supplied, how many orderings of rounds 1-19 generate_fixture
# tries (each with its own rematch draw) before keeping the one closest to
# the group targets - see _order_and_draw. One hits the best possible
# about 80% of the time; three, essentially always (30/30 in testing).
ORDERINGS_PER_DRAW = 3


def ladder_groups(ladder_order):
    """Splits teams into top / middle / bottom groups by last season's
    finish (ladder_order[0] finished 1st). The top and bottom groups are a
    third of the league each, rounded down, and the middle takes the rest:
    6/8/6 for 20 teams, 6/6/6 for 18 (the real AFL split). Returns
    {team_id: 0 (top) | 1 (middle) | 2 (bottom)}."""
    n = len(ladder_order)
    outer = n // 3
    groups = {}
    for position, team in enumerate(ladder_order):
        if position < outer:
            groups[team] = 0
        elif position >= n - outer:
            groups[team] = 2
        else:
            groups[team] = 1
    return groups


def rematch_group_targets(groups, target_degree):
    """{team: {group: rematches wanted against that group}} - one against
    each OTHER group and the rest within its own (3 own + 1 + 1 at five
    rematches), so teams mostly rematch teams that finished near them, like
    the real AFL fixture. With too few rematches for that, all of them go
    to the team's own group.

    These are aims, not guarantees: with 6/8/6 groups the middle can't
    all hit them (the top six only have six rematches to give the eight
    middle teams), so four middle teams end up with an extra own-group
    rematch in place of one of the cross-group ones."""
    group_ids = sorted(set(groups.values()))
    targets = {}
    for team, own in groups.items():
        others = [g for g in group_ids if g != own]
        if target_degree >= len(others) + 1:
            wanted = {g: 1 for g in others}
            wanted[own] = target_degree - len(others)
        else:
            wanted = {own: target_degree}
        targets[team] = wanted
    return targets


# Weighting for misses against rematch_group_targets: falling short on
# own-group rematches, or having an extra one against another group, costs
# this much more than the reverse. When targets can't all be met (the
# 6/8/6 middle group), that pushes the shortfall into "an extra own-group
# rematch" rather than "one fewer" - the direction the rule wants.
WRONG_WAY_MISS_WEIGHT = 3


def _team_group_cost(own_group, actual, wanted):
    """One team's miss against its group targets. Misses are squared, so
    one team missing by two costs more than two teams missing by one -
    which spreads an unavoidable shortfall around instead of leaving one
    middle team with no cross-group rematches at all."""
    cost = 0
    for group in set(wanted) | set(actual):
        diff = actual.get(group, 0) - wanted.get(group, 0)
        if group == own_group:
            cost += WRONG_WAY_MISS_WEIGHT * max(0, -diff) ** 2 + max(0, diff) ** 2
        else:
            cost += WRONG_WAY_MISS_WEIGHT * max(0, diff) ** 2 + max(0, -diff) ** 2
    return cost


def group_deviation(rounds, groups, targets):
    """How far a set of rematch rounds is from every team's group targets,
    summed over teams - see WRONG_WAY_MISS_WEIGHT for how misses are
    weighed. 0 means every team hit its targets exactly."""
    actual = {team: {} for team in groups}
    for round_pairs in rounds:
        for a, b in round_pairs:
            actual[a][groups[b]] = actual[a].get(groups[b], 0) + 1
            actual[b][groups[a]] = actual[b].get(groups[a], 0) + 1
    return sum(_team_group_cost(groups[team], actual[team], targets[team]) for team in groups)


def _improve_group_balance(rounds, guaranteed_pairs, groups, targets, rng, allowed=None):
    """Hill-climbs a rematch draw toward every team's group targets, in
    place. Each step takes two rematches in the same round - A v B and
    C v D - and re-pairs them as A v C + B v D (or A v D + B v C), keeping
    the change if it brings those four teams no further from their
    targets. Re-pairing within a round keeps every round a full set of
    pairings; guaranteed pairs are never touched, and a re-pairing that
    would repeat a rematch drawn elsewhere is skipped. Accepting equal
    moves too lets the search drift across flat patches instead of stalling
    on them. allowed(pair, round_index), if given, must accept both new
    pairings. Returns the final group_deviation."""
    fixed = {frozenset(pair) for pair in guaranteed_pairs}
    used = {frozenset(pair) for round_pairs in rounds for pair in round_pairs}
    actual = {team: {} for team in groups}

    def add(a, b, amount):
        actual[a][groups[b]] = actual[a].get(groups[b], 0) + amount
        actual[b][groups[a]] = actual[b].get(groups[a], 0) + amount

    def team_deviation(team):
        return _team_group_cost(groups[team], actual[team], targets[team])

    for round_pairs in rounds:
        for a, b in round_pairs:
            add(a, b, 1)
    total = sum(team_deviation(team) for team in groups)

    for _step in range(GROUP_BALANCE_STEPS):
        if total == 0:
            break
        round_index = rng.randrange(len(rounds))
        round_pairs = rounds[round_index]
        if len(round_pairs) < 2:
            continue
        i, j = rng.sample(range(len(round_pairs)), 2)
        (a, b), (c, d) = round_pairs[i], round_pairs[j]
        if frozenset((a, b)) in fixed or frozenset((c, d)) in fixed:
            continue
        new_i, new_j = rng.choice((((a, c), (b, d)), ((a, d), (b, c))))
        if frozenset(new_i) in used or frozenset(new_j) in used:
            continue
        if allowed and not (allowed(frozenset(new_i), round_index) and allowed(frozenset(new_j), round_index)):
            continue

        four = (a, b, c, d)
        before = sum(team_deviation(t) for t in four)
        add(a, b, -1)
        add(c, d, -1)
        add(*new_i, 1)
        add(*new_j, 1)
        after = sum(team_deviation(t) for t in four)
        if after <= before:
            used -= {frozenset((a, b)), frozenset((c, d))}
            used |= {frozenset(new_i), frozenset(new_j)}
            round_pairs[i], round_pairs[j] = new_i, new_j
            total += after - before
        else:
            add(*new_i, -1)
            add(*new_j, -1)
            add(a, b, 1)
            add(c, d, 1)
    return total


def build_doubleup_rounds(team_ids, guaranteed_pairs, target_degree, rng, groups=None, allowed=None):
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

    groups, if given (see ladder_groups), steers the draw toward each
    team's rematch_group_targets: GROUP_WEIGHTING_CANDIDATES weighted draws
    are made, each improved by _improve_group_balance, and the one with the
    lowest group_deviation is kept.

    allowed(pair, round_index), if given, restricts which pairs may be
    drawn into which rematch round (see _draw_rematches).

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

    targets = rematch_group_targets(groups, target_degree) if groups else None
    best, best_deviation, found = None, None, 0
    max_attempts = 2000
    for _attempt in range(max_attempts):
        result = _try_build_doubleup_rounds(team_ids, deduped_guaranteed, target_degree, rng, groups, targets,
                                            allowed)
        if result is None:
            continue
        if targets is None:
            return result
        deviation = _improve_group_balance(result, deduped_guaranteed, groups, targets, rng, allowed)
        if best is None or deviation < best_deviation:
            best, best_deviation = result, deviation
        found += 1
        if found >= GROUP_WEIGHTING_CANDIDATES or deviation == 0:
            break
    if best is not None:
        return best

    if deduped_guaranteed:
        raise FixtureGenerationError(
            "Could not build a valid double-up schedule satisfying every guaranteed "
            "double-up - the constraints may be too tight (e.g. several teams sharing "
            "guaranteed partners in a way that can't be evenly scheduled). Try removing "
            "or changing one of the guaranteed double-ups."
        )
    raise FixtureGenerationError(
        f"Could not draw {target_degree} rematch opponents for every team - with that many "
        "rematches, teams run out of opponents they haven't already been drawn against. "
        "Try fewer rounds."
    )


def _try_build_doubleup_rounds(team_ids, guaranteed_pairs, target_degree, rng, groups=None, targets=None,
                               allowed=None):
    """One attempt at build_doubleup_rounds. First places each guaranteed
    pair into a random rematch round where both its teams are still free,
    keeping them out of the final round whenever every team's guaranteed
    rematches fit without it, so the season doesn't close on a block of
    derbies. Then fills every round's remaining teams with random pairs,
    rejecting a pairing whenever the two teams have already been drawn
    together - weighted toward opponents both teams still want (by ladder
    group) when targets are given. Returns the list of rounds on success,
    None on dead-end (caller retries)."""
    guaranteed_degree = {t: 0 for t in team_ids}
    for a, b in guaranteed_pairs:
        guaranteed_degree[a] += 1
        guaranteed_degree[b] += 1
    if target_degree > 1 and max(guaranteed_degree.values(), default=0) <= target_degree - 1:
        eligible_rounds = list(range(target_degree - 1))
    else:
        eligible_rounds = list(range(target_degree))

    planned = [[] for _ in range(target_degree)]
    busy = [set() for _ in range(target_degree)]
    shuffled_guaranteed = list(guaranteed_pairs)
    rng.shuffle(shuffled_guaranteed)
    for a, b in shuffled_guaranteed:
        options = [r for r in eligible_rounds if a not in busy[r] and b not in busy[r]
                   and (allowed is None or allowed(frozenset((a, b)), r))]
        if not options:
            return None
        chosen = rng.choice(options)
        planned[chosen].append((a, b))
        busy[chosen].update((a, b))

    # Every guaranteed pair counts as already drawn, so the random fill
    # can't pair them in some earlier round as well.
    used_pairs = {frozenset(pair) for pair in guaranteed_pairs}
    rounds = []

    # Rematches each team still wants per ladder group, after its guaranteed ones.
    need = None
    if targets is not None:
        need = {team: dict(wanted) for team, wanted in targets.items()}
        for a, b in guaranteed_pairs:
            need[a][groups[b]] = need[a].get(groups[b], 0) - 1
            need[b][groups[a]] = need[b].get(groups[a], 0) - 1

    def weight(a, b):
        wanted_by_a = need[a].get(groups[b], 0) > 0
        wanted_by_b = need[b].get(groups[a], 0) > 0
        if wanted_by_a and wanted_by_b:
            return 20
        return 3 if wanted_by_a or wanted_by_b else 1

    for round_idx in range(target_degree):
        round_pairs = list(planned[round_idx])
        available = set(team_ids) - busy[round_idx]

        avail_list = list(available)
        rng.shuffle(avail_list)
        while avail_list:
            a = avail_list.pop()
            candidates = [t for t in avail_list if frozenset((a, t)) not in used_pairs
                          and (allowed is None or allowed(frozenset((a, t)), round_idx))]
            if not candidates:
                return None
            if need is None:
                b = rng.choice(candidates)
            else:
                b = rng.choices(candidates, weights=[weight(a, c) for c in candidates])[0]
                need[a][groups[b]] = need[a].get(groups[b], 0) - 1
                need[b][groups[a]] = need[b].get(groups[a], 0) - 1
            avail_list.remove(b)
            round_pairs.append((a, b))
            used_pairs.add(frozenset((a, b)))

        rounds.append(round_pairs)

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


# Upper bound on placement steps in _seat_assignment_for_marquees' search -
# a safety valve for marquee sets with no solution, which would otherwise
# make the backtracking search try every combination. Real marquee lists
# solve in well under a thousand steps.
MAX_MARQUEE_SEARCH_STEPS = 200_000

# Budget for _round_robin_by_search: search steps per attempt, and fresh
# attempts (in a different random order) before deciding the marquee games
# can't be fitted together. Real marquee sets - even several whole rounds -
# solve in a few hundred steps.
SEARCH_STEPS_PER_ATTEMPT = 50_000
SEARCH_ATTEMPTS = 5


def _round_robin_by_search(team_ids, marquee_round_pairs, rng):
    """Builds a single round-robin with every marquee pairing in its round,
    for marquee sets the seating search can't fit (see
    _seat_assignment_for_marquees) - e.g. several whole rounds picked by
    hand. Returns rounds like round_robin_pairings, or None if no
    arrangement was found.

    Treats it as filling in a table: every pair of teams needs a round,
    and every team must play once in every round. Marquee pairings are
    placed first; then, one at a time, the unplaced pair with the fewest
    rounds still open to both teams gets one of them (tried in random
    order). After each placement, every affected team is checked: each
    round it still needs must still be open to at least one of its
    remaining opponents, otherwise that choice is undone straight away.
    Picking the tightest pair first and checking ahead like this means it
    rarely has to back up at all."""
    n = len(team_ids)
    teams = list(team_ids)

    for _attempt in range(SEARCH_ATTEMPTS):
        open_rounds = {team: set(range(n - 1)) for team in teams}
        round_of = {}
        for round_idx, a, b in marquee_round_pairs:
            if round_idx not in open_rounds[a] or round_idx not in open_rounds[b]:
                return None  # a team pinned twice in one round
            open_rounds[a].discard(round_idx)
            open_rounds[b].discard(round_idx)
            round_of[frozenset((a, b))] = round_idx
        opponents_left = {
            team: {other for other in teams if other != team and frozenset((team, other)) not in round_of}
            for team in teams
        }
        steps = [0]

        def still_possible(team):
            return all(
                any(r in open_rounds[other] for other in opponents_left[team])
                for r in open_rounds[team]
            )

        def tightest_pair():
            best, best_rounds = None, None
            for a in teams:
                for b in opponents_left[a]:
                    if a < b:
                        shared = open_rounds[a] & open_rounds[b]
                        if best is None or len(shared) < len(best_rounds):
                            best, best_rounds = (a, b), shared
                            if len(shared) <= 1:
                                return best, best_rounds
            return best, best_rounds

        def search():
            steps[0] += 1
            if steps[0] > SEARCH_STEPS_PER_ATTEMPT:
                return False
            pair, shared = tightest_pair()
            if pair is None:
                return True
            a, b = pair
            choices = list(shared)
            rng.shuffle(choices)
            opponents_left[a].discard(b)
            opponents_left[b].discard(a)
            for round_idx in choices:
                open_rounds[a].discard(round_idx)
                open_rounds[b].discard(round_idx)
                affected = {a, b} | opponents_left[a] | opponents_left[b]
                if all(still_possible(team) for team in affected):
                    round_of[frozenset(pair)] = round_idx
                    if search():
                        return True
                    del round_of[frozenset(pair)]
                open_rounds[a].add(round_idx)
                open_rounds[b].add(round_idx)
            opponents_left[a].add(b)
            opponents_left[b].add(a)
            return False

        rng.shuffle(teams)
        if search():
            rounds = [[] for _ in range(n - 1)]
            for pair, round_idx in round_of.items():
                rounds[round_idx].append(tuple(pair))
            return rounds
    return None


def _marquee_search_order(marquee_round_pairs):
    """Orders marquees so each one shares a team with an earlier one where
    possible (a breadth-first walk over teams). Linked marquees then get
    placed back-to-back, so a seat choice that can't work is caught by the
    very next marquee instead of many steps later."""
    remaining = list(marquee_round_pairs)
    ordered = []
    seen_teams = set()
    while remaining:
        connected = next((m for m in remaining if m[1] in seen_teams or m[2] in seen_teams), None)
        marquee = connected or remaining[0]
        remaining.remove(marquee)
        ordered.append(marquee)
        seen_teams.update((marquee[1], marquee[2]))
    return ordered


def _seat_assignment_for_marquees(team_ids, slot_rounds, marquee_round_pairs, rng):
    """Chooses which team sits in which seat, and which rotation round
    becomes each marquee round, so every marquee pairing falls in its
    round. Returns (seating, target_to_slot) - seating maps seat index ->
    team id, target_to_slot maps a marquee's round index -> the rotation
    round played there - or None if no choice satisfies them all.

    marquee_round_pairs: list of (round_index, team_a, team_b) - 0-based
    index into the season's regular rounds.

    The rotation's seat pairings are fixed, so two seated teams always meet
    in one specific rotation round. Pinning marquee round R to rotation
    round R (as this once did) leaves too little freedom: a few marquees
    linked in a loop - e.g. A v B, B v C, C v D and D v A across three
    rounds - then usually have no seating that satisfies every link. But
    the regular rounds can be played in any order, so each marquee round
    can be any rotation round, as long as all of one round's marquees share
    the same one and different marquee rounds get different ones. Searching
    over that choice as well as the seating solves those loops.

    Backtracking search: each marquee either reuses its round's rotation
    round or picks an unused one, then seats its teams. Once a team is
    seated, its partner in any given rotation round is fixed, so most
    choices are forced and the search stays small."""
    n = len(team_ids)
    partner = []
    for slot_round in slot_rounds:
        seats = {}
        for seat_a, seat_b in slot_round:
            seats[seat_a] = seat_b
            seats[seat_b] = seat_a
        partner.append(seats)

    seat_to_team = {}
    team_to_seat = {}
    target_to_slot = {}
    used_slots = set()
    ordered = _marquee_search_order(marquee_round_pairs)
    steps = [0]

    def seat_options(slot, team_a, team_b):
        seat_a, seat_b = team_to_seat.get(team_a), team_to_seat.get(team_b)
        if seat_a is not None and seat_b is not None:
            return [(seat_a, seat_b)] if partner[slot][seat_a] == seat_b else []
        if seat_a is not None:
            return [(seat_a, partner[slot][seat_a])]
        if seat_b is not None:
            return [(partner[slot][seat_b], seat_b)]
        options = [pair for seat_pair in slot_rounds[slot] for pair in (seat_pair, seat_pair[::-1])]
        rng.shuffle(options)
        return options

    def place(idx):
        if idx == len(ordered):
            return True
        steps[0] += 1
        if steps[0] > MAX_MARQUEE_SEARCH_STEPS:
            return False
        target, team_a, team_b = ordered[idx]
        if target in target_to_slot:
            slots = [target_to_slot[target]]
        else:
            slots = [slot for slot in range(len(slot_rounds)) if slot not in used_slots]
            rng.shuffle(slots)

        for slot in slots:
            new_slot = target not in target_to_slot
            for seat_a, seat_b in seat_options(slot, team_a, team_b):
                # Each seat must be free or already held by this exact team.
                if seat_to_team.get(seat_a, team_a) != team_a or seat_to_team.get(seat_b, team_b) != team_b:
                    continue

                added = []
                for team, seat in ((team_a, seat_a), (team_b, seat_b)):
                    if team not in team_to_seat:
                        team_to_seat[team] = seat
                        seat_to_team[seat] = team
                        added.append((team, seat))
                if new_slot:
                    target_to_slot[target] = slot
                    used_slots.add(slot)

                if place(idx + 1):
                    return True

                if new_slot:
                    del target_to_slot[target]
                    used_slots.discard(slot)
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
    return seating, dict(target_to_slot)


def round_robin_pairings(team_ids, rng, marquee_round_pairs=None):
    """Standard circle-method single round-robin: returns a list of rounds
    (each a list of (team_a, team_b) tuples, order not yet meaningful for
    home/away), one round per team - 1, every pair appearing exactly once
    across the whole schedule. team_ids must have an even length (the
    classic circle method handles odd counts via a "bye" seat, which this
    deliberately doesn't support - see generate_fixture's even-count
    requirement).

    marquee_round_pairs, if given, is a list of (round_index, team_a,
    team_b) that MUST be paired in that 0-based round; the seating and
    round order are chosen to make that true rather than left to chance
    (see _seat_assignment_for_marquees). Returns None if nothing satisfies
    them all. Rounds without a marquee come back in random order."""
    teams = list(team_ids)
    n = len(teams)
    if n % 2 != 0:
        raise FixtureGenerationError("round_robin_pairings requires an even number of teams")

    slot_rounds = _circle_method_slot_pairings(n)

    if marquee_round_pairs:
        result = _seat_assignment_for_marquees(teams, slot_rounds, marquee_round_pairs, rng)
        if result is None:
            # The seating search only explores one rigid family of
            # round-robins, which many marquee sets (e.g. several whole
            # rounds) don't fit - search all round-robins instead.
            return _round_robin_by_search(teams, marquee_round_pairs, rng)
        seating, target_to_slot = result
    else:
        seating = list(teams)
        rng.shuffle(seating)
        target_to_slot = {}

    play_order = [None] * len(slot_rounds)
    for target, slot in target_to_slot.items():
        play_order[target] = slot
    unpinned_slots = [slot for slot in range(len(slot_rounds)) if slot not in target_to_slot.values()]
    rng.shuffle(unpinned_slots)
    for position in range(len(play_order)):
        if play_order[position] is None:
            play_order[position] = unpinned_slots.pop()

    return [[(seating[a], seating[b]) for a, b in slot_rounds[slot]] for slot in play_order]


def _round_layout(total_base, total_extra):
    """The order rounds are played in: every round-robin ("base") round
    first, then every rematch ("extra") round, so all fresh matchups come
    before any rematch. Returns ("base", i)/("extra", i) tags, one per final
    round - kept as its own step so a marquee's final round number can be
    mapped to the base round it lands on before any pairings exist."""
    return [("base", i) for i in range(total_base)] + [("extra", i) for i in range(total_extra)]


def _order_base_rounds(base_rounds, guaranteed_pairs, pinned, rng, groups=None):
    """Reorders the round-robin rounds - they can be played in any order -
    so the rounds where forced double-up pairs first meet come early,
    leaving room for their rematch to be MIN_REMATCH_GAP rounds later.

    With ladder groups, the rounds with the most same-group games come
    next: most rematches are meant to be within a team's own group (see
    rematch_group_targets), and those pairs can only be rematched if they
    first met early enough. Left in random order, the draw often runs
    short of eligible same-group pairs and misses the targets. Ties (and
    everything, without groups) are in random order.

    pinned: base round indices holding a marquee game - they keep their
    exact position."""
    guaranteed = {frozenset(pair) for pair in guaranteed_pairs}
    free = [rnd for idx, rnd in enumerate(base_rounds) if idx not in pinned]
    rng.shuffle(free)

    def priority(rnd):
        same_group = sum(1 for a, b in rnd if groups[a] == groups[b]) if groups else 0
        return (-sum(1 for pair in rnd if frozenset(pair) in guaranteed), -same_group)

    free.sort(key=priority)
    ordered = []
    for idx in range(len(base_rounds)):
        ordered.append(base_rounds[idx] if idx in pinned else free.pop(0))
    return ordered


def _order_and_draw(team_ids, base_rounds, guaranteed_pairs, pinned, rematch_count, rng, groups):
    """Orders the round-robin rounds and draws the rematches around them
    -> (ordered base rounds, rematch rounds). With ladder groups, tries
    ORDERINGS_PER_DRAW orderings (each with a fresh draw) and keeps the
    one closest to the group targets - which rounds come early decides
    which pairs MIN_REMATCH_GAP leaves eligible, so it matters."""
    if rematch_count == 0:
        return _order_base_rounds(base_rounds, guaranteed_pairs, pinned, rng, groups), []
    targets = rematch_group_targets(groups, rematch_count) if groups else None
    best = None
    for _ in range(ORDERINGS_PER_DRAW if groups else 1):
        ordered = _order_base_rounds(base_rounds, guaranteed_pairs, pinned, rng, groups)
        extra = _draw_rematches(team_ids, guaranteed_pairs, rematch_count, rng, groups, ordered)
        deviation = group_deviation(extra, groups, targets) if groups else 0
        if best is None or deviation < best[0]:
            best = (deviation, ordered, extra)
        if deviation == 0:
            break
    return best[1], best[2]


def _draw_rematches(team_ids, guaranteed_pairs, rematch_count, rng, groups, base_rounds):
    """The rematch draw, only pairing teams whose first meeting (in
    base_rounds) is at least MIN_REMATCH_GAP rounds before that rematch
    round. If no draw manages that, the gap is relaxed a round at a time,
    so an unusual setup gets the most spacing possible rather than an
    error."""
    first_meeting = {frozenset(pair): idx for idx, rnd in enumerate(base_rounds) for pair in rnd}
    base_count = len(base_rounds)
    error = None
    # A forced double-up whose first meeting is pinned late only asks for
    # the widest gap it can get short of the final round (where forced
    # double-ups aren't put), without lowering everyone else's. Other pairs
    # that met late simply aren't picked for a rematch.
    reachable = {}
    for pair in guaranteed_pairs:
        pair = frozenset(pair)
        reachable[pair] = max(1, base_count + max(rematch_count - 2, 0) - first_meeting[pair])
    for gap in range(MIN_REMATCH_GAP, 0, -1):
        def allowed(pair, rematch_index, gap=gap):
            # Rounds count from 1: first meeting in round idx + 1, rematch in
            # round base_count + rematch_index + 1.
            return base_count + rematch_index - first_meeting[pair] >= min(gap, reachable.get(pair, gap))
        try:
            return build_doubleup_rounds(team_ids, guaranteed_pairs, rematch_count, rng, groups, allowed=allowed)
        except FixtureGenerationError as e:
            error = e
    raise error


def assign_home_away(rounds, doubleup_pairs, marquees, rng, rematch_start):
    """Assigns home/away for every (team_a, team_b) pairing across every
    round, returning the same round structure with each pairing now
    (home_id, away_id), or None if this particular attempt paints itself
    into a corner (the caller regenerates and retries - see
    generate_fixture's MAX_GENERATION_ATTEMPTS loop).

    Constraints enforced:
      - No team plays more than MAX_HOME_AWAY_STREAK consecutive home (or
        away) games. Checked as a hard skip at every decision.
      - A double-up pair's two legs alternate venue (whoever hosts one leg
        is away for the other).
      - A marquee game's home/away is honored exactly as specified.
      - Every team's |home - away| ends within MAX_HOME_AWAY_IMBALANCE,
        checked once at the end.

    Two passes. Every game from rematch_start on is a rematch, and each
    rematch's venue fixes its first leg's, so the rematch block is decided
    FIRST, with home and away spread evenly within it. Deciding the season
    in plain round order instead would leave every team's closing games
    with venues already forced by first legs chosen weeks earlier, which
    regularly ends in an unbreakable 4-in-a-row. Doing the block first
    turns that around: its venues are chosen freely, and the forced games
    become the first legs, which are scattered through the regular season
    where they're easy to work around. Each team's rematches then also come
    out exactly balanced - every one is paired with a first leg at the
    opposite venue.

    The second pass then walks the season in round order, playing out the
    forced games and choosing the rest. Both passes look ahead (and in the
    first pass, around each first leg) at a team's already-forced venues,
    so a choice that would complete an over-long streak with games that are
    already fixed is never made. Among legal choices, the one minimizing a
    cost that weights streak length ten times more heavily than home/away
    imbalance wins, ties broken randomly. No backtracking - a dead end just
    returns None and the caller retries.

    marquees: list of (round_index, team_a, team_b, home_team) - round_index
    is the final round order's 0-based index.
    doubleup_pairs: set of frozenset({team_a, team_b}) - which pairings are
    a double-up."""
    teams = {t for rnd in rounds for pair in rnd for t in pair}

    pair_leg_rounds = {}
    for round_idx, round_pairs in enumerate(rounds):
        for team_a, team_b in round_pairs:
            pair_key = frozenset((team_a, team_b))
            if pair_key in doubleup_pairs:
                pair_leg_rounds.setdefault(pair_key, []).append(round_idx)

    def other_leg(pair_key, round_idx):
        return next((r for r in pair_leg_rounds.get(pair_key, []) if r != round_idx), None)

    # forced_home[(round_idx, pair_key)] = the team that must host that game;
    # team_forced[team][round_idx] = "home"/"away", the same thing per team.
    forced_home = {}
    team_forced = {t: {} for t in teams}

    def force(round_idx, pair_key, home):
        away = next(iter(pair_key - {home}))
        forced_home[(round_idx, pair_key)] = home
        team_forced[home][round_idx] = "home"
        team_forced[away][round_idx] = "away"

    def forced_run(team, round_idx, venue):
        """Length of the same-venue run through round_idx if the team plays
        `venue` there, counting only neighbouring games already forced."""
        run = 1
        for step in (-1, 1):
            k = round_idx + step
            while team_forced[team].get(k) == venue:
                run += 1
                k += step
        return run

    for round_idx, team_a, team_b, home_team in marquees:
        pair_key = frozenset((team_a, team_b))
        force(round_idx, pair_key, home_team)
        leg = other_leg(pair_key, round_idx)
        if leg is not None:
            force(leg, pair_key, next(iter(pair_key - {home_team})))

    def pick(options):
        if not options:
            return None
        rng.shuffle(options)
        options.sort(key=lambda option: option[0])
        return options[0][1]

    # Pass 1: the rematch block. Each choice fixes two games - the rematch
    # and its first leg - so both are checked against forced neighbours.
    block_home = {t: 0 for t in teams}
    for round_idx in range(rematch_start, len(rounds)):
        round_pairs = rounds[round_idx]
        order = list(range(len(round_pairs)))
        rng.shuffle(order)
        for i in order:
            team_a, team_b = round_pairs[i]
            pair_key = frozenset((team_a, team_b))
            if (round_idx, pair_key) in forced_home:
                continue
            leg = other_leg(pair_key, round_idx)
            options = []
            for home, away in ((team_a, team_b), (team_b, team_a)):
                runs = [forced_run(home, round_idx, "home"), forced_run(away, round_idx, "away")]
                if leg is not None:
                    runs += [forced_run(home, leg, "away"), forced_run(away, leg, "home")]
                if max(runs) > MAX_HOME_AWAY_STREAK:
                    continue
                balance = abs(2 * (block_home[home] + 1) - (round_idx - rematch_start + 1)) + \
                    abs(2 * block_home[away] - (round_idx - rematch_start + 1))
                options.append((sum(runs) * 10 + balance, home))
            home = pick(options)
            if home is None:
                return None
            force(round_idx, pair_key, home)
            if leg is not None:
                force(leg, pair_key, next(iter(pair_key - {home})))
        for team_a, team_b in round_pairs:
            block_home[forced_home[(round_idx, frozenset((team_a, team_b)))]] += 1

    # Pass 2: the whole season in play order.
    home_count = {t: 0 for t in teams}
    away_count = {t: 0 for t in teams}
    streak = {t: (None, 0) for t in teams}

    # Per team, for the rounds AFTER each round: the home-minus-away of the
    # games already forced there, and how many games are still free to set.
    total_rounds = len(rounds)
    forced_net_after = {}
    free_after = {}
    for team in teams:
        net = free = 0
        nets, frees = [0] * total_rounds, [0] * total_rounds
        for k in reversed(range(total_rounds)):
            nets[k], frees[k] = net, free
            venue = team_forced[team].get(k)
            if venue is None:
                free += 1
            else:
                net += 1 if venue == "home" else -1
        forced_net_after[team], free_after[team] = nets, frees

    def run_if(team, round_idx, venue):
        """Streak length the team would be on if it plays `venue` this
        round, including the already-forced games straight after it."""
        last_venue, length = streak[team]
        run = (length + 1) if last_venue == venue else 1
        k = round_idx + 1
        while team_forced[team].get(k) == venue:
            run += 1
            k += 1
        return run

    def projected_imbalance(team, round_idx, venue):
        """|home - away| the team would finish on if every game still free
        after this one went perfectly - so balance is planned for along the
        way, rather than only checked once it's too late to fix."""
        diff = home_count[team] - away_count[team] + (1 if venue == "home" else -1)
        return max(0, abs(diff + forced_net_after[team][round_idx]) - free_after[team][round_idx])

    def legal(team, round_idx, venue):
        return (run_if(team, round_idx, venue) <= MAX_HOME_AWAY_STREAK
                and projected_imbalance(team, round_idx, venue) <= MAX_HOME_AWAY_IMBALANCE)

    def cost(team, round_idx, venue):
        diff = home_count[team] - away_count[team] + (1 if venue == "home" else -1)
        target = abs(diff + forced_net_after[team][round_idx])
        # How close this leaves the team to being unable to recover its
        # balance - weighted up sharply as the free games run out.
        tightness = max(0, target - free_after[team][round_idx] + 2)
        return run_if(team, round_idx, venue) * 10 + target + tightness * 20

    assigned_rounds = []
    for round_idx, round_pairs in enumerate(rounds):
        assigned = [None] * len(round_pairs)
        order = list(range(len(round_pairs)))
        rng.shuffle(order)
        # Forced games first, so the streak state they set is in place
        # before the round's free choices are made around them.
        order.sort(key=lambda i: (round_idx, frozenset(round_pairs[i])) not in forced_home)

        for i in order:
            team_a, team_b = round_pairs[i]
            pair_key = frozenset((team_a, team_b))
            home = forced_home.get((round_idx, pair_key))
            if home is None:
                options = []
                for h, a in ((team_a, team_b), (team_b, team_a)):
                    if not (legal(h, round_idx, "home") and legal(a, round_idx, "away")):
                        continue
                    options.append((cost(h, round_idx, "home") + cost(a, round_idx, "away"), h))
                home = pick(options)
                if home is None:
                    return None
            away = next(iter(pair_key - {home}))
            for team, venue in ((home, "home"), (away, "away")):
                last_venue, length = streak[team]
                streak[team] = (venue, length + 1) if last_venue == venue else (venue, 1)
                if streak[team][1] > MAX_HOME_AWAY_STREAK:
                    return None
            home_count[home] += 1
            away_count[away] += 1
            assigned[i] = (home, away)

        assigned_rounds.append(assigned)

    for team in teams:
        if abs(home_count[team] - away_count[team]) > MAX_HOME_AWAY_IMBALANCE:
            return None

    return assigned_rounds


# How many games a balancing chain can flip (see _even_out_home_away), and
# how many chains it checks per uneven team before giving up on it.
BALANCE_CHAIN_MAX_LENGTH = 4
BALANCE_CHAIN_CHECKS = 400


def _even_out_home_away(rounds, doubleup_pairs, pinned, rng):
    """Evens out home/away splits after assign_home_away, in place: moves
    home games from teams with more home than away games (13-11) to teams
    with fewer (11-13) by flipping venues along a chain - A hosts B, B
    hosts C: flip both, and A has one fewer home game, C one more, and B
    the same. Only games between teams who meet once are flipped - a
    rematch's two legs must stay at opposite venues, and pinned marquee
    venues stay put - and a chain is only kept if no team ends up with more
    than MAX_HOME_AWAY_STREAK games in a row at the same venue.

    Best effort: rematch, marquee and streak rules can leave a team with no
    legal chain, so a few splits may stay uneven.

    rounds: assign_home_away's output - lists of (home_id, away_id).
    pinned: {(round_index, frozenset(pair))} - games whose venue is fixed."""
    teams = {t for rnd in rounds for pair in rnd for t in pair}
    venue = {t: [None] * len(rounds) for t in teams}
    balance = dict.fromkeys(teams, 0)
    for r, round_pairs in enumerate(rounds):
        for home, away in round_pairs:
            venue[home][r], venue[away][r] = "H", "A"
            balance[home] += 1
            balance[away] -= 1

    def flippable(r, i):
        pair = frozenset(rounds[r][i])
        return pair not in doubleup_pairs and (r, pair) not in pinned

    def flip(r, i):
        home, away = rounds[r][i]
        rounds[r][i] = (away, home)
        venue[home][r], venue[away][r] = "A", "H"

    def streak_ok(team):
        run, last = 0, None
        for v in venue[team]:
            run = run + 1 if v == last else 1
            last = v
            if run > MAX_HOME_AWAY_STREAK:
                return False
        return True

    def find_chain(source, sinks):
        hosting = {t: [] for t in teams}
        for r, round_pairs in enumerate(rounds):
            for i, (home, _) in enumerate(round_pairs):
                if flippable(r, i):
                    hosting[home].append((r, i))
        checks = [0]
        chain, visited = [], {source}

        def extend(team):
            games = hosting[team][:]
            rng.shuffle(games)
            for r, i in games:
                guest = rounds[r][i][1]
                if guest in visited or checks[0] >= BALANCE_CHAIN_CHECKS:
                    continue
                chain.append((r, i))
                visited.add(guest)
                if guest in sinks:
                    checks[0] += 1
                    for game in chain:
                        flip(*game)
                    if all(streak_ok(t) for t in visited):
                        return True
                    for game in chain:
                        flip(*game)
                elif len(chain) < BALANCE_CHAIN_MAX_LENGTH and extend(guest):
                    return True
                chain.pop()
                visited.discard(guest)
            return False

        return extend(source)

    stuck = set()
    while True:
        sinks = {t for t in teams if balance[t] <= -2}
        sources = [t for t in teams if balance[t] >= 2 and t not in stuck]
        if not sinks or not sources:
            return
        source = rng.choice(sources)
        before = {t: venue[t].count("H") for t in teams}
        if not find_chain(source, sinks):
            stuck.add(source)
            continue
        for t in teams:
            balance[t] += 2 * (venue[t].count("H") - before[t])
        stuck.clear()


def generate_fixture(team_ids, rounds, marquees=None, guaranteed_doubleups=None, ladder_order=None, rng=None,
                     home_games=None, accept=None, random_hosts=None):
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
    home_games: list of (home_team_id, away_team_id) - marquee games with no
        set round: the pair's first meeting is played at that home team,
        in whichever of the fresh rounds the generator puts it.
    random_hosts: pairs (team_a_id, team_b_id) of `marquees` whose round is
        pinned but whose home team isn't - the generator picks it like any
        other game's, so the listed home/away order doesn't matter.
    guaranteed_doubleups: list of (team_a_id, team_b_id) - these two teams
        are guaranteed to be paired twice (once each way) somewhere in the
        season; raises FixtureGenerationError if this over-commits a team
        beyond the available double-up slots (D above).
    ladder_order: optional list of every team id in last season's finishing
        order (1st first). When given, rematch opponents are weighted toward
        teams that finished near each other - see ladder_groups and
        rematch_group_targets. When omitted, rematch opponents are random.
    rng: random.Random instance - defaults to a fresh, unseeded Random()
        (each call produces a different valid fixture, not a reproducible
        one, matching match_sim.py's own convention for match simulation).
    accept: optional callable(fixture) for checks the generator itself
        doesn't know about - it returns the final result (which
        generate_fixture then returns instead of the fixture), or raises
        FixtureRejected to reject this fixture, which counts as a failed
        attempt. Any other FixtureGenerationError is passed straight on. E.g. timeslots + grounds (assign_grounds), whose
        clashes depend on which teams are home in the same round.

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

    groups = None
    if ladder_order is not None:
        if len(ladder_order) != n or set(ladder_order) != set(team_ids):
            raise FixtureGenerationError(
                "The ladder used for rematch weighting must list every team exactly once."
            )
        groups = ladder_groups(ladder_order)

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

    marquee_round_by_pair = {}
    for round_number, home_id, away_id in marquees:
        pair = frozenset((home_id, away_id))
        if pair in marquee_round_by_pair:
            raise FixtureGenerationError(
                f"Teams {home_id} and {away_id} have two marquee games (rounds "
                f"{marquee_round_by_pair[pair]} and {round_number}), but they only meet once "
                "before the rematch rounds. Keep one, and make them a guaranteed double-up "
                "if they should play twice."
            )
        marquee_round_by_pair[pair] = round_number

    home_games = home_games or []
    home_game_pairs = set()
    for home_id, away_id in home_games:
        if home_id not in team_ids or away_id not in team_ids:
            raise FixtureGenerationError(f"Marquee game references an unknown team: {home_id}, {away_id}")
        if home_id == away_id:
            raise FixtureGenerationError(f"Marquee game can't have a team play itself: {home_id}")
        pair = frozenset((home_id, away_id))
        if pair in marquee_round_by_pair or pair in home_game_pairs:
            raise FixtureGenerationError(
                f"Teams {home_id} and {away_id} have two marquee games, but they only meet once "
                "before the rematch rounds."
            )
        home_game_pairs.add(pair)

    if doubleup_count == 0 and guaranteed_doubleups:
        raise FixtureGenerationError(
            "Guaranteed double-ups were specified, but this round count leaves no "
            "double-up slots at all (every team already plays every opponent exactly once)."
        )

    # Every stage below is randomized and can fail on an unlucky draw, so
    # the whole schedule is rebuilt from scratch until one satisfies every
    # constraint. See MAX_GENERATION_ATTEMPTS - a single attempt is cheap.
    # The play order of base vs double-up rounds is fixed up front, so a
    # marquee's final round number can be mapped to the specific base round
    # it lands on before any pairings exist - which is what lets marquees
    # be built in rather than hoped for.
    layout = _round_layout(base_rounds_needed, doubleup_count)
    if len(layout) != rounds:
        raise FixtureGenerationError(
            f"Internal scheduling error: laid out {len(layout)} rounds, expected {rounds}."
        )

    base_marquees = []   # (base_round_index, home_id, away_id)
    for round_number, home_id, away_id in marquees:
        kind, idx = layout[round_number - 1]
        if kind == "base":
            base_marquees.append((idx, home_id, away_id))

    # A marquee in the rematch block would need those two teams to be
    # double-up partners AND have their rematch fall in that exact round -
    # both decided by the double-up draw, which has its own
    # guaranteed-pairs mechanism instead. Rather than silently producing a
    # fixture missing the marquee, say so.
    if len(base_marquees) != len(marquees):
        raise FixtureGenerationError(
            f"Rounds {base_rounds_needed + 1}-{rounds} are rematch rounds, which can't host a "
            "marquee game - who plays whom then is decided by the rematch draw. Pick an "
            f"earlier round (1-{base_rounds_needed}), or set it up as a guaranteed double-up instead."
        )
    pinned_base_rounds = {idx for idx, _, _ in base_marquees}
    random_host_pairs = {frozenset(pair) for pair in random_hosts or ()}

    rejected = None
    for attempt in range(MAX_GENERATION_ATTEMPTS):
        # The rounds themselves (especially the ladder-weighted rematch
        # draw) are the expensive part, and a failed attempt is almost
        # always down to the venue step - so one set of rounds is kept for
        # several attempts at it.
        if attempt % ATTEMPTS_PER_REMATCH_DRAW == 0:
            base_rounds = round_robin_pairings(team_ids, rng, base_marquees)
            if base_rounds is None:
                # No seating satisfies every marquee - structural, so
                # retrying with a different draw won't help.
                raise FixtureGenerationError(
                    "These marquee games can't all be scheduled together - two of them need the "
                    "same team in incompatible places. Try removing one, or moving it to another round."
                )
            base_rounds, extra_rounds = _order_and_draw(team_ids, base_rounds, guaranteed_doubleups,
                                                        pinned_base_rounds, doubleup_count, rng, groups)
            doubleup_pairs = {frozenset(pair) for rnd in extra_rounds for pair in rnd}

        merged_rounds = base_rounds + extra_rounds

        marquee_triples = [
            (round_number - 1, home_id, away_id, home_id)
            for round_number, home_id, away_id in marquees
            if frozenset((home_id, away_id)) not in random_host_pairs
        ]
        # A marquee game without a round pins the venue of the pair's
        # first meeting, wherever the round-robin put it.
        meeting_round = {
            frozenset(pair): round_idx
            for round_idx, round_pairs in enumerate(base_rounds) for pair in round_pairs
        }
        marquee_triples += [
            (meeting_round[frozenset((home_id, away_id))], home_id, away_id, home_id)
            for home_id, away_id in home_games
        ]

        assigned_rounds = assign_home_away(
            merged_rounds, doubleup_pairs, marquee_triples, rng, rematch_start=base_rounds_needed
        )
        if assigned_rounds is None:
            continue
        _even_out_home_away(
            assigned_rounds, doubleup_pairs,
            {(round_idx, frozenset((team_a, team_b))) for round_idx, team_a, team_b, _ in marquee_triples}, rng,
        )

        fixture = []
        for round_idx, round_pairs in enumerate(assigned_rounds):
            for home_id, away_id in round_pairs:
                fixture.append((round_idx + 1, home_id, away_id))
        if accept is None:
            return fixture
        try:
            return accept(fixture)
        except FixtureRejected as e:
            rejected = e
            continue

    if accept is not None and rejected is not None:
        raise FixtureGenerationError(
            f"Could not build a fixture that fits every ground after {MAX_GENERATION_ATTEMPTS} attempts "
            f"({rejected}) - too many teams sharing a ground are at home in the same rounds. Give some of "
            "them an alternate ground, or check each team's home ground in Stadium Settings."
        )
    if doubleup_count > RELIABLE_DOUBLEUPS_PER_TEAM:
        max_reliable = base_rounds_needed + RELIABLE_DOUBLEUPS_PER_TEAM
        detail = (
            f" {rounds} rounds means every team plays {doubleup_count} rematches, which is "
            f"more than can be scheduled reliably for {n} teams (about {RELIABLE_DOUBLEUPS_PER_TEAM} "
            f"is the limit). Try {max_reliable} rounds or fewer."
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


# One round's standard timeslots, earliest first, as (name, prime_time). A
# round has exactly one game per entry, so this must have team_count / 2
# entries. Names are always "<Day> <time of day>" - see DAYS / TIMES_OF_DAY.
TIMESLOTS = (
    ("Thursday night", True),
    ("Friday night", True),
    ("Saturday afternoon", False),
    ("Saturday afternoon", False),
    ("Saturday twilight", False),
    ("Saturday night", False),
    ("Saturday night", False),
    ("Sunday afternoon", False),
    ("Sunday afternoon", False),
    ("Sunday twilight", False),
)

# A round's days, in play order. A round runs Wednesday to Tuesday, so an
# irregular Wednesday game (Anzac Eve style) opens the round and a Monday
# one (King's Birthday style) comes after Sunday.
DAYS = ("Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "Monday", "Tuesday")
TIMES_OF_DAY = ("afternoon", "twilight", "night")

# A marquee game pinned to a time with no free standard slot left for it
# (e.g. "Monday afternoon", or a second Thursday night game) is played in
# that time anyway, in place of a standard slot: a round's first such game
# replaces a Saturday afternoon slot, its second a Sunday afternoon slot.
# No more than this many are allowed per round.
IRREGULAR_SLOT_REPLACES = ("Saturday afternoon", "Sunday afternoon")

# Spread of the random nudge added to each game's prime-time score (see
# assign_timeslots). Tuned so a 20-team league averages out like the real
# AFL: about 10 prime-time games a season for each top-4 team and 0-1 for
# each bottom-4 team, with individual seasons varying either side of that.
# Higher spreads prime time more evenly; lower favours top teams harder.
PRIME_TIME_RANDOMNESS = 8


def timeslot_name(name):
    """Normalises a timeslot name ("monday NIGHT" -> "Monday night"), or
    raises FixtureGenerationError if it isn't a day plus a time of day."""
    parts = str(name).strip().split()
    if len(parts) == 2:
        day, time_of_day = parts[0].capitalize(), parts[1].lower()
        if day in DAYS and time_of_day in TIMES_OF_DAY:
            return f"{day} {time_of_day}"
    raise FixtureGenerationError(
        f"'{name}' isn't a timeslot - use a day and a time of day, e.g. \"Monday afternoon\" "
        f"(times: {', '.join(TIMES_OF_DAY)})."
    )


def timeslot_order(name):
    """Sort key putting timeslot names in play order within a round."""
    day, time_of_day = name.split()
    return DAYS.index(day), TIMES_OF_DAY.index(time_of_day)


def short_timeslot(name):
    """"Thursday night" -> "Thu night", for compact fixture lists."""
    day, time_of_day = name.split()
    return f"{day[:3]} {time_of_day}"


def _place_pinned_timeslots(round_number, names, timeslots=TIMESLOTS):
    """Fits one round's pinned timeslot names into its slots. Returns the
    slot indices left free for every other game. A pin takes a matching
    free standard slot if there is one; otherwise it's an irregular game,
    played in place of the next slot in IRREGULAR_SLOT_REPLACES. Raises
    FixtureGenerationError if they don't fit."""
    free_slots = list(range(len(timeslots)))
    # Pins that match a standard slot go first, so an irregular pin can't
    # take a slot another pin was asking for.
    standard_names = {name for name, _ in timeslots}
    irregular = []
    for name in sorted(names, key=lambda n: n not in standard_names):
        slot = next((s for s in free_slots if timeslots[s][0] == name), None)
        if slot is None:
            if len(irregular) >= len(IRREGULAR_SLOT_REPLACES):
                raise FixtureGenerationError(
                    f"Round {round_number} has {len(irregular) + 1} games outside the standard timeslots "
                    f"({', '.join(sorted(irregular + [name], key=timeslot_order))}) - only "
                    f"{len(IRREGULAR_SLOT_REPLACES)} per round "
                    "are allowed."
                )
            replaces = IRREGULAR_SLOT_REPLACES[len(irregular)]
            irregular.append(name)
            slot = next((s for s in free_slots if timeslots[s][0] == replaces), None)
            if slot is None:
                raise FixtureGenerationError(
                    f"Round {round_number} has no {replaces} slot left to give up for {name}."
                )
        free_slots.remove(slot)
    return free_slots


def check_round_timeslots(round_number, names):
    """Raises FixtureGenerationError if these timeslot names can't all be
    pinned in one round - lets a menu reject a marquee game's timeslot as
    soon as it's added, rather than at generation time."""
    _place_pinned_timeslots(round_number, [timeslot_name(name) for name in names])


def assign_timeslots(fixture, ladder_order=None, marquee_slots=None, rng=None, timeslots=TIMESLOTS):
    """Gives every game in a fixture a timeslot. Returns rows of
    (round_number, home_id, away_id, timeslot_name), each round's games in
    play order (Thursday night first, Sunday twilight last - plus any
    irregular day, per DAYS).

    Prime-time slots go to the round's most attractive games: each game
    scores its two teams' strength from last season's ladder (1st highest)
    plus a random nudge (PRIME_TIME_RANDOMNESS), and the top scorers take
    the prime slots, in random order between them. No caps or quotas - top
    teams simply tend to score highest, so they get the most prime-time
    games over a season. Without a ladder, prime-time games are random.
    Every other game gets one of the remaining slots at random.

    marquee_slots: optional {(round_number, home_id, away_id): timeslot_name}
        pinning those games to a time before anything else is placed. Any
        day and time of day works: a pinned game takes a matching free
        standard slot if there is one, and otherwise is played at its time
        anyway in place of a slot from IRREGULAR_SLOT_REPLACES - one
        irregular game per entry there, per round.

    Raises FixtureGenerationError if a round's game count doesn't match the
    timeslot list, a pinned game isn't in the fixture, a timeslot name isn't
    a day plus a time of day, a round has more irregular games than
    IRREGULAR_SLOT_REPLACES allows, or the slot one should replace is
    already taken."""
    if rng is None:
        rng = random.Random()
    marquee_slots = {game: timeslot_name(name) for game, name in (marquee_slots or {}).items()}

    fixture_games = set(fixture)
    for game in marquee_slots:
        if game not in fixture_games:
            round_number, home_id, away_id = game
            raise FixtureGenerationError(
                f"Round {round_number}: {home_id} v {away_id} has a timeslot but isn't in the fixture."
            )

    strength = {}
    if ladder_order is not None:
        n = len(ladder_order)
        strength = {team: n - 1 - position for position, team in enumerate(ladder_order)}

    by_round = {}
    for game in fixture:
        by_round.setdefault(game[0], []).append(game)

    result = []
    for round_number in sorted(by_round):
        games = by_round[round_number]
        if len(games) != len(timeslots):
            raise FixtureGenerationError(
                f"Round {round_number} has {len(games)} games but there are {len(timeslots)} timeslots per round."
            )

        pinned = [game for game in games if game in marquee_slots]
        free_slots = _place_pinned_timeslots(round_number, [marquee_slots[g] for g in pinned], timeslots)
        slot_name_of = {game: marquee_slots[game] for game in pinned}

        unplaced = [game for game in games if game not in slot_name_of]
        prime_slots = [s for s in free_slots if timeslots[s][1]]
        other_slots = [s for s in free_slots if not timeslots[s][1]]

        def score(game):
            _, home_id, away_id = game
            return strength.get(home_id, 0) + strength.get(away_id, 0) + rng.gauss(0, PRIME_TIME_RANDOMNESS)

        unplaced.sort(key=score, reverse=True)
        rng.shuffle(prime_slots)
        rng.shuffle(other_slots)
        for game, slot in zip(unplaced, prime_slots + other_slots):
            slot_name_of[game] = timeslots[slot][0]

        for game in sorted(games, key=lambda g: timeslot_order(slot_name_of[g])):
            result.append((*game, slot_name_of[game]))

    return result


# ---------------------------------------------------------------------------
# Grounds
# ---------------------------------------------------------------------------

class TeamGrounds:
    """One team's ground setup for assign_grounds. Grounds are opaque ids -
    the generator only compares them.

    home: its home ground (None - no ground set up, so its home games get
        no ground and are never checked for clashes).
    alternate: an optional second home ground.
    draw: crowd draw, 1 (smallest club) to 5 (biggest).
    alternate_games: how many home games a season to play at the alternate.
    alternate_opponents: the opponent crowd draws those games can be
        against - e.g. {4, 5} for big games only."""

    def __init__(self, home, alternate=None, draw=3, alternate_games=0, alternate_opponents=(1, 2, 3, 4, 5)):
        self.home = home
        self.alternate = alternate if alternate != home else None
        self.draw = draw
        self.alternate_games = alternate_games if self.alternate is not None else 0
        self.alternate_opponents = frozenset(alternate_opponents)


# Never swapped into or out of when resolving a ground clash: prime time
# stays decided purely by how attractive each game is (see assign_timeslots).
CLASH_SWAP_EXCLUDED_SLOTS = ("Thursday night", "Friday night")


def slots_overlap(slot_a, slot_b):
    """Whether two timeslots would have games running at the same time:
    the same day and time of day, or the same day with either one at
    twilight (which runs into both the afternoon and the night). An
    afternoon and a night game on the same day don't overlap."""
    day_a, time_a = slot_a.split()
    day_b, time_b = slot_b.split()
    return day_a == day_b and (time_a == time_b or "twilight" in (time_a, time_b))


def _same_day(slot_a, slot_b):
    return slot_a.split()[0] == slot_b.split()[0]


def assign_grounds(rows, team_grounds, capacity=None, blockbuster_draw=5, pinned_grounds=None,
                   pinned_slots=None, home_ground_pairs=None, rng=None):
    """Gives every game of a timeslotted fixture a ground. Returns rows of
    (round_number, home_id, away_id, timeslot, ground), each round still
    in play order - resolving a clash can swap two games' timeslots.

    rows: assign_timeslots' output.
    team_grounds: {team_id: TeamGrounds}. A team missing from it, or with
        no home ground, gets no ground for its home games.
    capacity: {ground: seats} - decides a blockbuster's ground.
    blockbuster_draw: two teams both rated at least this make a
        blockbuster, played at the bigger of the home team's grounds.
    pinned_grounds: {(round_number, home_id, away_id): ground} - marquee
        games with a set ground.
    pinned_slots: games whose timeslot is pinned (never swapped).
    home_ground_pairs: pairs of teams (forced double-ups) whose games are
        always at the home team's home ground - never a blockbuster or
        quota game.

    Every game is at its home team's home ground, except pinned grounds,
    blockbusters (the bigger of the home team's grounds), and alternate-
    ground quota games (TeamGrounds.alternate_games home games, against
    opponents of an allowed crowd draw, picked at random - not counting
    blockbusters played there). A game's ground never changes after that.

    Two games at one ground on the same day are fine as an afternoon game
    and a night game. Any other pair overlaps, and one of them swaps
    timeslots with another game in its round - never into or out of
    Thursday or Friday night (CLASH_SWAP_EXCLUDED_SLOTS) or a pinned
    timeslot.

    Raises FixtureRejected if a round can't be resolved - too many
    overlapping games at one ground for any swap to fix - so it can be
    generate_fixture's `accept` check, which then tries another fixture."""
    if rng is None:
        rng = random.Random()
    capacity = capacity or {}
    pinned_grounds = pinned_grounds or {}
    pinned_slots = set(pinned_slots or ())
    home_ground_pairs = {frozenset(pair) for pair in home_ground_pairs or ()}

    def setup(team):
        grounds = team_grounds.get(team)
        return grounds if grounds is not None and grounds.home is not None else None

    def draw(team):
        grounds = team_grounds.get(team)
        return grounds.draw if grounds is not None else 0

    games = [tuple(row[:3]) for row in rows]
    slot = {tuple(row[:3]): row[3] for row in rows}
    ground = {}
    protected = set()
    blockbusters = set()
    for game in games:
        _, home, away = game
        grounds = setup(home)
        if game in pinned_grounds:
            ground[game] = pinned_grounds[game]
            protected.add(game)
        elif grounds is None:
            ground[game] = None
        elif frozenset((home, away)) in home_ground_pairs:
            ground[game] = grounds.home
            protected.add(game)
        elif grounds.alternate is not None and min(draw(home), draw(away)) >= blockbuster_draw:
            ground[game] = max((grounds.home, grounds.alternate), key=lambda g: capacity.get(g) or 0)
            protected.add(game)
            blockbusters.add(game)
        else:
            ground[game] = grounds.home

    for team, grounds in team_grounds.items():
        if setup(team) is None or not grounds.alternate_games:
            continue
        already = sum(1 for game in games
                      if game[1] == team and ground[game] == grounds.alternate and game not in blockbusters)
        candidates = [game for game in games
                      if game[1] == team and game not in protected and ground[game] == grounds.home
                      and draw(game[2]) in grounds.alternate_opponents]
        wanted = max(grounds.alternate_games - already, 0)
        for game in rng.sample(candidates, min(wanted, len(candidates))):
            ground[game] = grounds.alternate

    by_round = {}
    for game in games:
        by_round.setdefault(game[0], []).append(game)

    for round_number in sorted(by_round):
        round_games = by_round[round_number]
        while True:
            overlapping = [
                (a, b) for i, a in enumerate(round_games) for b in round_games[i + 1:]
                if ground[a] is not None and ground[a] == ground[b] and slots_overlap(slot[a], slot[b])
            ]
            if not overlapping:
                break
            if not _swap_clash_timeslot(overlapping, round_games, ground, slot, pinned_slots, rng):
                raise FixtureRejected(
                    f"Round {round_number} has more games at one ground than its timeslots can fit."
                )

    result = []
    for round_number in sorted(by_round):
        for game in sorted(by_round[round_number], key=lambda g: timeslot_order(slot[g])):
            result.append((*game, slot[game], ground[game]))
    return result


def _swap_clash_timeslot(overlapping, round_games, ground, slot, pinned_slots, rng):
    """Swaps one of an overlapping pair's games into another game's
    timeslot, picking the swap that leaves the fewest overlapping games at
    a shared ground (then the fewest afternoon + night double-headers).
    Returns whether a swap helped."""
    def swappable(game):
        return game not in pinned_slots and slot[game] not in CLASH_SWAP_EXCLUDED_SLOTS

    def cost():
        overlaps = same_day = 0
        for i, a in enumerate(round_games):
            for b in round_games[i + 1:]:
                if ground[a] is not None and ground[a] == ground[b] and _same_day(slot[a], slot[b]):
                    same_day += 1
                    overlaps += slots_overlap(slot[a], slot[b])
        return overlaps, same_day

    current = cost()
    best = None
    for a, b in overlapping:
        for game in (a, b):
            if not swappable(game):
                continue
            for partner in round_games:
                if partner == game or not swappable(partner) or slot[partner] == slot[game]:
                    continue
                slot[game], slot[partner] = slot[partner], slot[game]
                option = (cost(), rng.random(), game, partner)
                slot[game], slot[partner] = slot[partner], slot[game]
                if option[0][0] < current[0] and (best is None or option < best):
                    best = option
    if best is None:
        return False
    _, _, game, partner = best
    slot[game], slot[partner] = slot[partner], slot[game]
    return True


# Finals timeslots, per week, keyed by slot_code prefix (see
# finals_bracket.py). Two-game weeks: the bigger-drawing game gets the
# first slot. The QF/EF week is handled separately (see finals_timeslots).
FINALS_TIMESLOTS = {
    "WC": ("Friday night", "Saturday night"),
    "SF": ("Friday night", "Saturday night"),
    "PF": ("Friday night", "Saturday twilight"),
    "GF": ("Saturday afternoon",),
}


def finals_timeslots(matches, draw, rng=None):
    """Timeslots for one finals week. matches: [(slot_code, home_id,
    away_id)]; draw: {team_id: crowd draw 1-5} (missing teams count as 3).
    A game's draw is its two teams' combined; ties are broken randomly.
    Returns [(slot_code, home_id, away_id, timeslot)] in play order.

    Qualifying/Elimination Finals week: the bigger-drawing QF on Thursday
    night, then of the other three the biggest on Friday night, the
    smallest on Saturday afternoon and the last on Saturday night - if
    those two draw the same, a QF gets the night slot over an EF. Other
    weeks follow FINALS_TIMESLOTS."""
    if rng is None:
        rng = random.Random()
    noise = {match: rng.random() for match in matches}

    def size(match):
        return draw.get(match[1], 3) + draw.get(match[2], 3), noise[match]

    by_size = sorted(matches, key=size, reverse=True)
    if any(code.startswith(("QF", "EF")) for code, _, _ in matches):
        qf = next(m for m in by_size if m[0].startswith("QF"))
        rest = [m for m in by_size if m != qf]
        afternoon, night = sorted(rest[1:], key=lambda m: (size(m)[0], m[0].startswith("QF"), noise[m]))
        slots = {qf: "Thursday night", rest[0]: "Friday night", afternoon: "Saturday afternoon",
                 night: "Saturday night"}
    else:
        week = FINALS_TIMESLOTS[matches[0][0][:2]]
        slots = dict(zip(by_size, week))
    return sorted(((*match, slots[match]) for match in matches), key=lambda row: timeslot_order(row[3]))
