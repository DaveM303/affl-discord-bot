"""AFL finals bracket generation - pure functions, no discord.py/DB access.
The caller (commands/season_commands.py's advance_to_next_round) queries the
DB for whatever each function needs and passes in plain data; these functions
never touch aiosqlite themselves, matching the "own small module" pattern
already used by ladder_image.py/compensation_image.py in this codebase.

10-team, 5-week finals series (locked-in design, no pre-finals bye):

    Wildcard:     WC1: 7th vs 10th        WC2: 8th vs 9th
    Qual/Elim:    QF1: 1st vs 4th         EF1: 5th vs lowest-ranked WC winner
                  EF2: 6th vs highest-ranked WC winner    QF2: 2nd vs 3rd
    Semis:        SF1: QF1 loser vs EF1 winner    SF2: QF2 loser vs EF2 winner
    Prelims:      PF1: QF1 winner vs SF2 winner   PF2: QF2 winner vs SF1 winner
    Grand Final:  GF: PF1 winner vs PF2 winner

Every generate_* function returns a list of (slot_code, home_team_id,
away_team_id) tuples - the higher seed (or, once seeding no longer applies,
the team from the more advanced bracket position) is listed as "home" by
convention, matching how the real AFL grants home advantage to the
higher-placed team in each final. "Results" dicts passed between rounds are
always {slot_code: (winner_team_id, loser_team_id)}.

The Grand Final is the exception: it's played at a neutral venue, so no
home-ground advantage is applied in the simulation, and "home" there means
only "listed first" - awarded to whichever finalist placed higher on the
regular-season ladder (see generate_grand_final).
"""


def generate_wildcard_round(ranked_team_ids):
    """WC1: 7th vs 10th, WC2: 8th vs 9th. ranked_team_ids is a plain list of
    team_ids in ladder order (index 0 = 1st place) - the frozen final
    regular-season ladder. Requires at least 10 teams."""
    return [
        ("WC1", ranked_team_ids[6], ranked_team_ids[9]),
        ("WC2", ranked_team_ids[7], ranked_team_ids[8]),
    ]


def generate_qf_ef_round(ranked_team_ids, wc_results):
    """QF1: 1st vs 4th, QF2: 2nd vs 3rd, EF1: 5th vs the LOWER-ranked of the
    two Wildcard winners, EF2: 6th vs the HIGHER-ranked one. wc_results is
    {"WC1": (winner_team_id, loser_team_id), "WC2": (...)} - only the
    winners are used here (losers already finished 9th/10th). "Ranked"
    means each winner's own position in ranked_team_ids - e.g. if the 7-seed
    and 9-seed both win their wildcard matches, the 9-seed is the
    lower-ranked winner and faces 5th, while the 7-seed (better) faces 6th."""
    wc1_winner, _ = wc_results["WC1"]
    wc2_winner, _ = wc_results["WC2"]

    rank_of = {team_id: index for index, team_id in enumerate(ranked_team_ids)}
    if rank_of[wc1_winner] > rank_of[wc2_winner]:
        lower_ranked_winner, higher_ranked_winner = wc1_winner, wc2_winner
    else:
        lower_ranked_winner, higher_ranked_winner = wc2_winner, wc1_winner

    return [
        ("QF1", ranked_team_ids[0], ranked_team_ids[3]),
        ("QF2", ranked_team_ids[1], ranked_team_ids[2]),
        ("EF1", ranked_team_ids[4], lower_ranked_winner),
        ("EF2", ranked_team_ids[5], higher_ranked_winner),
    ]


def generate_semis_round(qf_ef_results):
    """SF1: QF1 loser vs EF1 winner, SF2: QF2 loser vs EF2 winner.
    qf_ef_results is {"QF1": (winner, loser), "QF2": (...), "EF1": (...),
    "EF2": (...)}. The QF loser (a top-4 team) is the home side, matching
    real AFL's higher-seed-hosts convention - a QF loser is guaranteed to
    have finished no worse than 4th, always outranking any EF winner."""
    qf1_winner, qf1_loser = qf_ef_results["QF1"]
    qf2_winner, qf2_loser = qf_ef_results["QF2"]
    ef1_winner, _ = qf_ef_results["EF1"]
    ef2_winner, _ = qf_ef_results["EF2"]

    return [
        ("SF1", qf1_loser, ef1_winner),
        ("SF2", qf2_loser, ef2_winner),
    ]


def generate_prelims_round(qf_ef_results, semis_results):
    """PF1: QF1 winner vs SF2 winner, PF2: QF2 winner vs SF1 winner. The QF
    winner (a top-4 team that won its qualifying final) hosts, same
    higher-seed-hosts convention as generate_semis_round."""
    qf1_winner, _ = qf_ef_results["QF1"]
    qf2_winner, _ = qf_ef_results["QF2"]
    sf1_winner, _ = semis_results["SF1"]
    sf2_winner, _ = semis_results["SF2"]

    return [
        ("PF1", qf1_winner, sf2_winner),
        ("PF2", qf2_winner, sf1_winner),
    ]


def generate_grand_final(prelims_results, ranked_team_ids):
    """GF: PF1 winner vs PF2 winner, with the team that finished HIGHER on
    the regular-season ladder listed as home.

    "Home" is nominal here: the Grand Final is played at a neutral venue, so
    neither side gets home-ground advantage in the simulation (see
    match_commands.py, which disables it for slot_code "GF"). Listing the
    higher-placed team first is purely presentational - it decides the order
    in the fixture, score line and box score - but it should still reflect
    the better season rather than which half of the bracket a team came
    through, which is what PF1-vs-PF2 would have meant.

    ranked_team_ids is the frozen final regular-season ladder (index 0 =
    1st place), the same list the earlier finals rounds are seeded from."""
    pf1_winner, _ = prelims_results["PF1"]
    pf2_winner, _ = prelims_results["PF2"]

    rank_of = {team_id: index for index, team_id in enumerate(ranked_team_ids)}
    # A team missing from the ladder shouldn't be possible (both grand
    # finalists played the whole season), but rank it last rather than
    # raising - a KeyError here would strand the finals with no GF fixture.
    if rank_of.get(pf2_winner, len(ranked_team_ids)) < rank_of.get(pf1_winner, len(ranked_team_ids)):
        return [("GF", pf2_winner, pf1_winner)]
    return [("GF", pf1_winner, pf2_winner)]


def compute_final_finish_order(ranked_team_ids, all_round_results):
    """Computes the season's true final 1-10 finish once the Grand Final has
    been played, per the user's spec: the GF winner finishes 1st, the GF
    loser 2nd, then each remaining pair of same-round losers (Preliminary,
    then Semi, then Elimination/Qualifying wildcard, then Wildcard) finishes
    as a pair, ordered within that pair by their ORIGINAL regular-season
    rank (whoever placed higher in ranked_team_ids finishes higher here
    too). ranked_team_ids is the frozen final regular-season ladder (same
    list generate_wildcard_round was seeded from). all_round_results is
    {slot_code: (winner_team_id, loser_team_id)} covering every finals slot
    that was played (WC1/WC2/QF1/QF2/EF1/EF2/SF1/SF2/PF1/PF2/GF).

    Returns a list of 10 team_ids, 1st to 10th."""
    rank_of = {team_id: index for index, team_id in enumerate(ranked_team_ids)}

    def by_original_rank(pair):
        return sorted(pair, key=lambda team_id: rank_of[team_id])

    gf_winner, gf_loser = all_round_results["GF"]

    pf1_loser = all_round_results["PF1"][1]
    pf2_loser = all_round_results["PF2"][1]
    sf1_loser = all_round_results["SF1"][1]
    sf2_loser = all_round_results["SF2"][1]
    ef1_loser = all_round_results["EF1"][1]
    ef2_loser = all_round_results["EF2"][1]
    wc1_loser = all_round_results["WC1"][1]
    wc2_loser = all_round_results["WC2"][1]

    order = [gf_winner, gf_loser]
    order += by_original_rank((pf1_loser, pf2_loser))
    order += by_original_rank((sf1_loser, sf2_loser))
    order += by_original_rank((ef1_loser, ef2_loser))
    order += by_original_rank((wc1_loser, wc2_loser))
    return order
