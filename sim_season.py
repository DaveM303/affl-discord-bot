"""Simulate a full round-robin season using match_sim.py and print the
resulting ladder, with optional top-10 leaderboards (goals, disposals,
marks, tackles, spoils, hitouts, brownlow votes, best & fairest votes) as
season totals and/or single-game bests.
Every team plays every other team --rounds times (default 1 - once each),
using their current lineups - fast way to sanity-check the model's overall
season-shape (spread of the ladder, whether OVR-ranking roughly holds).

Teams with an incomplete lineup are skipped (with a warning) rather than
blocking the whole run.

Usage:
    py sim_season.py
    py sim_season.py --rounds 2
    py sim_season.py --variance 0.5
    py sim_season.py --seed 42
    py sim_season.py --goals --disposals             (season-total leaderboards)
    py sim_season.py --best-goals --best-disposals    (single-game best performances)
    py sim_season.py --brownlow                       (season Brownlow medal tally)
    py sim_season.py --bestfairest                    (season best & fairest tally, per club)
    py sim_season.py --bestfairest-team Eagles         (full B&F vote count for one team)
"""

import argparse
import random
import sqlite3

from config import DB_PATH
from match_sim import simulate_match, DEFAULT_VARIANCE

# stat_name -> (flag name, display label, unit label, skip-zero-only-scorers)
LEADERBOARD_STATS = [
    ("goals", "goals", "Goalkickers", "goals", True),
    ("disposals", "disposals", "Disposal winners", "disposals", False),
    ("marks", "marks", "Mark winners", "marks", False),
    ("tackles", "tackles", "Tackle winners", "tackles", False),
    ("spoils", "spoils", "Spoil winners", "spoils", False),
    ("hitouts", "hitouts", "Hitout winners", "hitouts", True),
]
# brownlow_votes/best_fairest_votes aren't PlayerStatLine attributes -
# brownlow_votes is a per-MATCH ranking across both teams combined (see
# MatchResult.brownlow_votes) and best_fairest_votes is a per-TEAM ranking
# among just that club's own 23 (see TeamMatchResult.best_and_fairest_votes)
# - both are handled separately from LEADERBOARD_STATS throughout this
# file rather than folded into that getattr-driven loop.


def get_all_team_lineups(conn):
    """Returns {team_name: lineup_rows} for every team with a complete 23-player
    lineup, printing a warning and skipping any team that doesn't have one."""
    cursor = conn.execute("SELECT team_id, team_name FROM teams WHERE team_name != 'Draft Pool' ORDER BY team_name")
    teams = cursor.fetchall()

    lineups = {}
    for team_id, team_name in teams:
        cursor = conn.execute(
            """SELECT p.player_id, p.name, p.position, p.overall_rating, l.position_name
               FROM lineups l
               JOIN players p ON l.player_id = p.player_id AND p.team_id = l.team_id
               WHERE l.team_id = ?""",
            (team_id,)
        )
        lineup = cursor.fetchall()
        if len(lineup) < 23:
            print(f"⚠️  Skipping {team_name}: lineup incomplete ({len(lineup)}/23 filled)")
            continue
        lineups[team_name] = lineup

    return lineups


def get_league_avg_ovr(conn):
    """Average OVR of players actually selected in a current lineup - not every
    rostered player, since bench/reserve players who never take the field
    would drag the baseline down below what real match-day rosters look like."""
    cursor = conn.execute(
        """SELECT AVG(p.overall_rating) FROM lineups l
           JOIN players p ON l.player_id = p.player_id AND p.team_id = l.team_id"""
    )
    return cursor.fetchone()[0] or 85.0


class LadderRow:
    def __init__(self, team_name):
        self.team_name = team_name
        self.wins = 0
        self.losses = 0
        self.draws = 0
        self.points_for = 0
        self.points_against = 0

    @property
    def games_played(self):
        return self.wins + self.losses + self.draws

    @property
    def percentage(self):
        if self.points_against == 0:
            return 100.0 if self.points_for == 0 else float("inf")
        return 100.0 * self.points_for / self.points_against

    @property
    def sort_key(self):
        # Standard AFL ladder ordering: wins first (draws count as half a
        # win for ranking purposes), percentage as the tiebreaker
        return (-(self.wins + 0.5 * self.draws), -self.percentage)


def run_season(variance, seed, rounds, requested_totals, requested_bests, show_brownlow, show_best_fairest, best_fairest_team=None):
    conn = sqlite3.connect(DB_PATH)
    lineups = get_all_team_lineups(conn)
    league_avg_ovr = get_league_avg_ovr(conn)
    conn.close()

    team_names = sorted(lineups.keys())
    if len(team_names) < 2:
        print("Need at least 2 teams with complete lineups to simulate a season.")
        return

    rng = random.Random(seed) if seed is not None else random.Random()

    ladder = {name: LadderRow(name) for name in team_names}
    # stat_name -> {player_id -> [name, position, slot, overall_rating, team_name, total]}
    player_stats = {stat_name: {} for stat_name in requested_totals}
    # stat_name -> [(value, name, position, slot, ovr, team_name, opponent_name), ...] - one
    # entry per player per match, kept unsorted/untrimmed until printing (season-length
    # totals are small enough that this is cheap, no need to maintain a running top-10)
    single_game_bests = {stat_name: [] for stat_name in requested_bests}
    # player_id -> [name, position, slot, overall_rating, team_name, total_votes] - same shape
    # as player_stats' per-stat dicts, but keyed off MatchResult.brownlow_votes() (a per-MATCH
    # combined-both-teams ranking) rather than a flat PlayerStatLine attribute.
    brownlow_totals = {}
    # Same shape again, but keyed off TeamMatchResult.best_and_fairest_votes()
    # (a per-TEAM ranking among just that club's own 23) - each player's own
    # club total, not comparable across clubs the way brownlow_totals is.
    best_fairest_totals = {}

    matchup_count = 0
    for round_num in range(rounds):
        # Alternate home/away each round-robin, same as a real double (or
        # triple, etc.) round-robin fixture would - not just re-simulating
        # the identical pairing with the same team always at home
        swap_home_away = round_num % 2 == 1

        for i in range(len(team_names)):
            for j in range(i + 1, len(team_names)):
                home_name, away_name = team_names[i], team_names[j]
                if swap_home_away:
                    home_name, away_name = away_name, home_name

                result = simulate_match(
                    home_name, lineups[home_name],
                    away_name, lineups[away_name],
                    league_avg_ovr, variance=variance, rng=rng
                )
                matchup_count += 1

                home_row = ladder[home_name]
                away_row = ladder[away_name]
                home_row.points_for += result.home.score
                home_row.points_against += result.away.score
                away_row.points_for += result.away.score
                away_row.points_against += result.home.score

                if result.home.score > result.away.score:
                    home_row.wins += 1
                    away_row.losses += 1
                elif result.away.score > result.home.score:
                    away_row.wins += 1
                    home_row.losses += 1
                else:
                    home_row.draws += 1
                    away_row.draws += 1

                for team_result, team_name_for_player, opponent_name in (
                    (result.home, home_name, away_name),
                    (result.away, away_name, home_name),
                ):
                    for stat_line in team_result.stat_lines.values():
                        p = stat_line.player
                        for stat_name, _, _, _, skip_zero in LEADERBOARD_STATS:
                            if stat_name not in requested_totals and stat_name not in requested_bests:
                                continue
                            value = getattr(stat_line, stat_name)
                            if skip_zero and value == 0:
                                continue

                            if stat_name in requested_totals:
                                totals = player_stats[stat_name]
                                if p.player_id not in totals:
                                    totals[p.player_id] = [p.name, p.position, p.slot, p.overall_rating, team_name_for_player, 0]
                                totals[p.player_id][5] += value

                            if stat_name in requested_bests:
                                single_game_bests[stat_name].append(
                                    (value, p.name, p.position, p.slot, p.overall_rating, team_name_for_player, opponent_name)
                                )

                if show_brownlow:
                    # One ranking across BOTH teams combined per match (see
                    # MatchResult.brownlow_votes) - not folded into the
                    # LEADERBOARD_STATS loop above since it isn't a flat
                    # PlayerStatLine attribute to getattr().
                    votes_by_player_id = result.brownlow_votes(rng=rng)
                    for team_result, team_name_for_player in ((result.home, home_name), (result.away, away_name)):
                        for stat_line in team_result.stat_lines.values():
                            p = stat_line.player
                            votes = votes_by_player_id.get(p.player_id, 0)
                            if votes == 0:
                                continue
                            if p.player_id not in brownlow_totals:
                                brownlow_totals[p.player_id] = [p.name, p.position, p.slot, p.overall_rating, team_name_for_player, 0]
                            brownlow_totals[p.player_id][5] += votes

                if show_best_fairest or best_fairest_team is not None:
                    # A SEPARATE 5-4-3-2-1 vote per team, among just that
                    # team's own 23 players (see
                    # TeamMatchResult.best_and_fairest_votes) - unlike
                    # brownlow_votes above, this is never a combined
                    # both-teams ranking.
                    for team_result, team_name_for_player in ((result.home, home_name), (result.away, away_name)):
                        votes_by_player_id = team_result.best_and_fairest_votes(rng=rng)
                        for stat_line in team_result.stat_lines.values():
                            p = stat_line.player
                            votes = votes_by_player_id.get(p.player_id, 0)
                            if votes == 0:
                                continue
                            if p.player_id not in best_fairest_totals:
                                best_fairest_totals[p.player_id] = [p.name, p.position, p.slot, p.overall_rating, team_name_for_player, 0]
                            best_fairest_totals[p.player_id][5] += votes

    round_label = "round-robin" if rounds == 1 else f"{rounds}x round-robins"
    print(f"\nSeason simulation - {len(team_names)} teams, {round_label}, {matchup_count} matches")
    print(f"(variance={variance}, league_avg_ovr={league_avg_ovr:.1f})")
    print("=" * 78)
    print(f"{'Pos':<4}{'Team':<24}{'P':>3}{'W':>4}{'L':>4}{'D':>4}{'For':>7}{'Against':>9}{'Pct':>9}")
    print("-" * 78)

    ranked = sorted(ladder.values(), key=lambda row: row.sort_key)
    for pos, row in enumerate(ranked, 1):
        print(
            f"{pos:<4}{row.team_name:<24}{row.games_played:>3}{row.wins:>4}{row.losses:>4}{row.draws:>4}"
            f"{row.points_for:>7}{row.points_against:>9}{row.percentage:>8.1f}%"
        )
    print()

    for stat_name, _, display_label, unit_label, _ in LEADERBOARD_STATS:
        if stat_name in requested_totals:
            print(f"Top 10 {display_label.lower()} ({round_label}, season totals):")
            ranked_stat = sorted(player_stats[stat_name].values(), key=lambda row: -row[5])[:10]
            for rank, (name, position, slot, ovr, team_name_for_player, total) in enumerate(ranked_stat, 1):
                print(f"  {rank:2d}. {name:22s} {position:10s} {slot:5s} OVR {ovr:3d}   {team_name_for_player:22s} {total:5d} {unit_label}")
            print()

        if stat_name in requested_bests:
            print(f"Top 10 single-game {display_label.lower()} ({round_label}):")
            ranked_bests = sorted(single_game_bests[stat_name], key=lambda row: -row[0])[:10]
            for rank, (value, name, position, slot, ovr, team_name_for_player, opponent_name) in enumerate(ranked_bests, 1):
                print(f"  {rank:2d}. {name:22s} {position:10s} {slot:5s} OVR {ovr:3d}   {team_name_for_player:22s} vs {opponent_name:22s} {value:5d} {unit_label}")
            print()

    if show_brownlow:
        print(f"Top 10 Brownlow Medal votes ({round_label}, season totals):")
        ranked_brownlow = sorted(brownlow_totals.values(), key=lambda row: -row[5])[:10]
        for rank, (name, position, slot, ovr, team_name_for_player, total) in enumerate(ranked_brownlow, 1):
            print(f"  {rank:2d}. {name:22s} {position:10s} {slot:5s} OVR {ovr:3d}   {team_name_for_player:22s} {total:5d} votes")
        print()

    if show_best_fairest or best_fairest_team is not None:
        by_team = {}
        for name, position, slot, ovr, team_name_for_player, total in best_fairest_totals.values():
            by_team.setdefault(team_name_for_player, []).append((name, position, slot, ovr, total))

    if show_best_fairest:
        # Voted per-team (see best_and_fairest_votes' docstring), so shown
        # per-team too - each club's own top 5, not one combined cross-club
        # list, matching how a real club best & fairest count is announced.
        print(f"Club Best & Fairest - top 5 per team ({round_label}, season totals):")
        for team_name_for_player in team_names:
            print(f"  {team_name_for_player}:")
            ranked_team = sorted(by_team.get(team_name_for_player, []), key=lambda row: -row[4])[:5]
            for rank, (name, position, slot, ovr, total) in enumerate(ranked_team, 1):
                print(f"    {rank}. {name:22s} {position:10s} {slot:5s} OVR {ovr:3d}   {total:5d} votes")
        print()

    if best_fairest_team is not None:
        # Full vote count for ONE specified team - a case-insensitive
        # substring match against team_names, same lookup convention the
        # bot's own /removeteam-style commands use. Shows EVERY player who
        # polled at least one vote (not just the top 5), since this is a
        # targeted lookup for one club's count, not a leaderboard scan.
        matches = [name for name in team_names if best_fairest_team.lower() in name.lower()]
        if not matches:
            print(f"No team found matching '{best_fairest_team}'.\n")
        elif len(matches) > 1:
            print(f"'{best_fairest_team}' matches multiple teams: {', '.join(matches)} - be more specific.\n")
        else:
            matched_team = matches[0]
            print(f"Club Best & Fairest - full votes for {matched_team} ({round_label}, season totals):")
            ranked_team = sorted(by_team.get(matched_team, []), key=lambda row: -row[4])
            if not ranked_team:
                print("  No votes recorded.")
            for rank, (name, position, slot, ovr, total) in enumerate(ranked_team, 1):
                print(f"  {rank:2d}. {name:22s} {position:10s} {slot:5s} OVR {ovr:3d}   {total:5d} votes")
            print()


def main():
    parser = argparse.ArgumentParser(description="Simulate a full round-robin season and print the resulting ladder.")
    parser.add_argument("--rounds", type=int, default=1, help="Number of round-robins to play - 1 = every team plays each other once, 2 = twice (home and away), etc. (default: 1)")
    parser.add_argument("--variance", type=float, default=DEFAULT_VARIANCE, help=f"match_sim_variance override (default: {DEFAULT_VARIANCE})")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for reproducible results (default: random each run)")
    for stat_name, flag_name, display_label, unit_label, _ in LEADERBOARD_STATS:
        parser.add_argument(f"--{flag_name}", action="store_true", help=f"Show the top 10 {display_label.lower()} season-total leaderboard (off by default)")
        parser.add_argument(f"--best-{flag_name}", action="store_true", help=f"Show the top 10 single-game {display_label.lower()} performances (off by default)")
    parser.add_argument("--brownlow", action="store_true", help="Show the top 10 Brownlow Medal votes season-total leaderboard (off by default) - 3/2/1 votes per match, ranked across both teams combined (see MatchResult.brownlow_votes)")
    parser.add_argument("--bestfairest", action="store_true", help="Show each team's own top 5 club best & fairest votes (off by default) - 5/4/3/2/1 votes per match, voted separately per team, position-neutral (see TeamMatchResult.best_and_fairest_votes)")
    parser.add_argument("--bestfairest-team", type=str, default=None, metavar="NAME", help="Show the FULL club best & fairest vote count (every vote-getter, not just top 5) for one specified team (case-insensitive substring match)")
    args = parser.parse_args()

    if args.rounds < 1:
        print("--rounds must be at least 1.")
        return

    requested_totals = {stat_name for stat_name, flag_name, _, _, _ in LEADERBOARD_STATS if getattr(args, flag_name)}
    requested_bests = {stat_name for stat_name, flag_name, _, _, _ in LEADERBOARD_STATS if getattr(args, f"best_{flag_name}")}

    run_season(args.variance, args.seed, args.rounds, requested_totals, requested_bests,
               args.brownlow, args.bestfairest, best_fairest_team=args.bestfairest_team)


if __name__ == "__main__":
    main()
