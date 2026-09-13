"""Fast batch match-simulation tool for tuning match_sim.py.

Runs many simulations between two teams' current lineups without going
through Discord, and prints win rate / score / margin stats, with optional
per-player goalkicker and disposal breakdowns.
Use this after any tuning change to match_sim.py to quickly see its effect.

Team names can be given as a prefix instead of typed out in full, e.g.
"north" for "North Melbourne" - matching is case-insensitive but only from
the START of the team name, so "melbourne" matches "Melbourne" but not
"North Melbourne".

Usage:
    py sim_batch.py "Geelong Cats" "Essendon"
    py sim_batch.py "Geelong Cats" "Essendon" --sims 1000
    py sim_batch.py "Geelong Cats" "Essendon" --variance 0.5
    py sim_batch.py "Geelong Cats" "Essendon" --seed 42
    py sim_batch.py "Geelong Cats" "Essendon" --goalkickers
    py sim_batch.py "Geelong Cats" "Essendon" --disposals
    py sim_batch.py "Geelong Cats" "Essendon" --marks
    py sim_batch.py "Geelong Cats" "Essendon" --tackles
    py sim_batch.py "Geelong Cats" "Essendon" --spoils
    py sim_batch.py "Geelong Cats" "Essendon" --hitouts
    py sim_batch.py "Geelong Cats" "Essendon" --no-home-advantage
"""

import argparse
import random
import sqlite3
import sys

from config import DB_PATH
from match_sim import simulate_match, DEFAULT_VARIANCE

# stat_name -> (flag attribute, display label, unit label, skip-zero-only-scorers)
STAT_BREAKDOWNS = [
    ("goals", "goalkickers", "Goals", "goals", True),
    ("disposals", "disposals", "Disposals", "disposals", False),
    ("marks", "marks", "Marks", "marks", False),
    ("tackles", "tackles", "Tackles", "tackles", False),
    ("spoils", "spoils", "Spoils", "spoils", False),
    ("hitouts", "hitouts", "Hitouts", "hitouts", True),
]


def resolve_team(conn, team_name):
    """Resolve a possibly-partial team name to (team_id, full_name). Matches
    case-insensitively from the START of the team name only, so "north"
    matches "North Melbourne" but "melbourne" does not (and still matches
    "Melbourne" on its own)."""
    cursor = conn.execute("SELECT team_id, team_name FROM teams WHERE team_name = ? COLLATE NOCASE", (team_name,))
    row = cursor.fetchone()
    if row:
        return row

    cursor = conn.execute("SELECT team_id, team_name FROM teams WHERE team_name LIKE ? ESCAPE '\\'", (_escape_like(team_name) + "%",))
    matches = cursor.fetchall()
    if not matches:
        print(f"Team '{team_name}' not found.")
        sys.exit(1)
    if len(matches) > 1:
        names = ", ".join(m[1] for m in matches)
        print(f"'{team_name}' matches multiple teams: {names}. Be more specific.")
        sys.exit(1)
    return matches[0]


def _escape_like(text):
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def get_lineup(conn, team_name):
    team_id, team_name = resolve_team(conn, team_name)

    cursor = conn.execute(
        """SELECT p.player_id, p.name, p.position, p.overall_rating, l.position_name
           FROM lineups l
           JOIN players p ON l.player_id = p.player_id AND p.team_id = l.team_id
           WHERE l.team_id = ?""",
        (team_id,)
    )
    lineup = cursor.fetchall()
    if len(lineup) < 23:
        print(f"❌ {team_name}'s lineup is incomplete: {23 - len(lineup)} position(s) empty. Fill it in before batch-testing.")
        sys.exit(1)
    return team_name, lineup


def get_league_avg_ovr(conn):
    """Average OVR of players actually selected in a current lineup - not every
    rostered player, since bench/reserve players who never take the field
    would drag the baseline down below what real match-day rosters look like."""
    cursor = conn.execute(
        """SELECT AVG(p.overall_rating) FROM lineups l
           JOIN players p ON l.player_id = p.player_id AND p.team_id = l.team_id"""
    )
    return cursor.fetchone()[0] or 85.0


def run_batch(team1, team2, sims, variance, seed, requested_breakdowns, home_ground_advantage=True):
    conn = sqlite3.connect(DB_PATH)
    team1, lineup1 = get_lineup(conn, team1)
    team2, lineup2 = get_lineup(conn, team2)
    league_avg_ovr = get_league_avg_ovr(conn)
    conn.close()

    rng = random.Random(seed) if seed is not None else random.Random()

    team1_wins = 0
    team2_wins = 0
    draws = 0
    team1_scores = []
    team2_scores = []
    margins = []
    # stat_name -> {player_id -> [name, position, slot, overall_rating, side, total]}
    player_stats = {stat_name: {} for stat_name in requested_breakdowns}

    for _ in range(sims):
        result = simulate_match(team1, lineup1, team2, lineup2, league_avg_ovr, variance=variance, rng=rng,
                                 home_ground_advantage=home_ground_advantage)
        h, a = result.home, result.away
        team1_scores.append(h.score)
        team2_scores.append(a.score)
        margins.append(h.score - a.score)

        if h.score > a.score:
            team1_wins += 1
        elif a.score > h.score:
            team2_wins += 1
        else:
            draws += 1

        for stat_name, skip_zero in requested_breakdowns.items():
            totals = player_stats[stat_name]
            for team_result, side in ((h, "Home"), (a, "Away")):
                for stat_line in team_result.stat_lines.values():
                    value = getattr(stat_line, stat_name)
                    if skip_zero and value == 0:
                        continue
                    p = stat_line.player
                    if p.player_id not in totals:
                        totals[p.player_id] = [p.name, p.position, p.slot, p.overall_rating, side, 0]
                    totals[p.player_id][5] += value

    home_adv_label = "on" if home_ground_advantage else "off"
    print(f"\n{team1} vs {team2}  -  {sims} simulations  (variance={variance}, league_avg_ovr={league_avg_ovr:.1f}, home_ground_advantage={home_adv_label})")
    print("=" * 70)
    print(f"{team1} wins:  {team1_wins:5d}  ({100 * team1_wins / sims:5.1f}%)")
    print(f"{team2} wins:  {team2_wins:5d}  ({100 * team2_wins / sims:5.1f}%)")
    if draws:
        print(f"Draws:      {draws:5d}  ({100 * draws / sims:5.1f}%)")
    print()
    print(f"{team1} avg score:  {sum(team1_scores) / sims:6.1f}  (min {min(team1_scores)}, max {max(team1_scores)})")
    print(f"{team2} avg score:  {sum(team2_scores) / sims:6.1f}  (min {min(team2_scores)}, max {max(team2_scores)})")
    abs_margins = [abs(m) for m in margins]
    avg_margin = sum(abs_margins) / sims
    print(f"Avg margin:  {avg_margin:6.1f}  (min {min(abs_margins)}, max {max(abs_margins)})")

    stat_labels = {stat_name: (display_label, unit_label) for stat_name, _, display_label, unit_label, _ in STAT_BREAKDOWNS}
    for stat_name, totals in player_stats.items():
        display_label, unit_label = stat_labels[stat_name]
        print()
        print(f"{display_label} by player (avg per match, across all {sims} sims, both teams):")
        ranked = sorted(totals.values(), key=lambda row: -row[5])
        for name, position, slot, ovr, side, total in ranked:
            print(f"  {name:22s} {position:10s} {slot:5s} OVR {ovr:3d}   {side:4s}   {total / sims:.2f} {unit_label}/match")
    print()


def main():
    parser = argparse.ArgumentParser(description="Batch-run match simulations between two teams' current lineups.")
    parser.add_argument("team1", help="First team name (or a prefix of it, e.g. 'north')")
    parser.add_argument("team2", help="Second team name (or a prefix of it, e.g. 'north')")
    parser.add_argument("--sims", type=int, default=300, help="Number of simulations to run (default: 300)")
    parser.add_argument("--variance", type=float, default=DEFAULT_VARIANCE, help=f"match_sim_variance override (default: {DEFAULT_VARIANCE})")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for reproducible results (default: random each run)")
    parser.add_argument("--no-home-advantage", action="store_true", help="Disable home ground advantage for team1 (on by default)")
    for stat_name, flag_name, display_label, unit_label, _ in STAT_BREAKDOWNS:
        parser.add_argument(f"--{flag_name}", action="store_true", help=f"Show the per-player {display_label.lower()} breakdown (off by default)")
    args = parser.parse_args()

    requested_breakdowns = {}
    for stat_name, flag_name, display_label, unit_label, skip_zero in STAT_BREAKDOWNS:
        if getattr(args, flag_name):
            requested_breakdowns[stat_name] = skip_zero

    run_batch(args.team1, args.team2, args.sims, args.variance, args.seed, requested_breakdowns,
              home_ground_advantage=not args.no_home_advantage)


if __name__ == "__main__":
    main()
