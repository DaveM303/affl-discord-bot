import asyncio
import os
import random

import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from config import DB_PATH
from utils import is_admin_user, get_team_emoji_str
from match_sim import (
    simulate_match, simulate_match_with_events, format_score, DEFAULT_VARIANCE,
    Player, MatchEvent, simulate_extra_time_half, simulate_extra_time_after_siren_shot,
    EXTRA_TIME_HALF_LENGTH_MINUTES,
)

# Pacing (Live Match Feed design doc, §04) - the baseline/floor delay is
# adjustable live per match via the control panel's speed buttons (see
# SPEED_PRESETS/DEFAULT_SPEED_SECONDS below _paced_delay)
REAL_SECONDS_PER_SIM_MINUTE = 2.0
# How often the posting loop checks pause/skip/abandon state while waiting
# out a delay - short enough that a button press feels responsive.
POLL_INTERVAL_SECONDS = 0.5

# After-siren goal easter egg pacing: SIREN -> (5s) -> audio clip (if a
# siren-beater winner) -> (10s) -> the goal/behind message itself. Builds
# suspense the same way the real broadcast moment does - siren sounds,
# then a pause before the commentary/result lands.
AFTER_SIREN_AUDIO_DELAY_SECONDS = 5
AFTER_SIREN_MESSAGE_DELAY_SECONDS = 10

# Gap between the final full-time siren and the match-summary embed for
# every OTHER game (no after-siren shot) - a beat of pause before the
# result posts, rather than an instant cut.
FULL_TIME_SUMMARY_DELAY_SECONDS = 5

# Easter egg: posted right before a Q4 after-siren goal that actually wins
# the match (see match_sim.MatchEvent.siren_beater_winner) - the Sam Lloyd
# 2016 siren-beater call. None disables the audio post entirely (falls back
# to just the text message) if the file isn't present.
#
# Deliberately bland on-disk filename AND a bland display name (set where
# discord.File is constructed) - Discord shows the attachment's filename
# before anyone plays it, so anything referencing Sam Lloyd/2016/the siren
# by name would spoil the moment before the clip even plays.
SAM_LLOYD_AUDIO_PATH = os.path.join(os.path.dirname(__file__), "..", "assets", "live_match_clip.mp3")
if not os.path.isfile(SAM_LLOYD_AUDIO_PATH):
    SAM_LLOYD_AUDIO_PATH = None
SAM_LLOYD_AUDIO_DISPLAY_NAME = "audio.mp3"

# Only one live match may run at a time (see design doc §09) - keyed by
# nothing in particular, just a single module-level slot, since a live match
# is a whole-server sandbox event, not a per-channel or per-team thing.
_active_live_match = None


def _group_events_by_quarter(events):
    by_quarter = {1: [], 2: [], 3: [], 4: []}
    for e in events:
        by_quarter[e.quarter].append(e)
    for q in by_quarter:
        by_quarter[q].sort(key=lambda e: e.minute)
    return by_quarter


def _quarter_score_summary(events_up_to_and_including_quarter, home_name, away_name, home_emoji, away_emoji):
    home_goals = sum(1 for e in events_up_to_and_including_quarter if e.kind == "goal" and e.team_name == home_name)
    home_behinds = sum(1 for e in events_up_to_and_including_quarter if e.kind == "behind" and e.team_name == home_name)
    away_goals = sum(1 for e in events_up_to_and_including_quarter if e.kind == "goal" and e.team_name == away_name)
    away_behinds = sum(1 for e in events_up_to_and_including_quarter if e.kind == "behind" and e.team_name == away_name)
    return (
        f"{home_emoji}**{home_name}**  {format_score(home_goals, home_behinds)}\n"
        f"{away_emoji}**{away_name}**  {format_score(away_goals, away_behinds)}"
    )


QUARTER_START_LABELS = {1: "1ST QUARTER", 2: "2ND QUARTER", 3: "3RD QUARTER", 4: "4TH QUARTER"}
QUARTER_END_LABELS = {1: "QUARTER TIME", 2: "HALF TIME", 3: "THREE QUARTER TIME", 4: "END OF 4TH QUARTER"}

# /matchcentre's match stats stat-switch buttons (see _MatchStatsView) - keys
# match player_match_stats' own column names so a selected stat can index
# straight into a player dict from _fetch_box_score_data with no translation.
# "goals" doubles as the goals/behinds view (shown as "G.B", sorted by goals
# then behinds) rather than a bare goals count - there's no separate
# Behinds button, a behind on its own isn't worth its own leaderboard view.
STAT_LABELS = {
    "goals": "Goals", "disposals": "Disposals",
    "marks": "Marks", "tackles": "Tackles", "spoils": "Spoils", "hitouts": "Hitouts",
}
BOX_SCORE_PLAYERS_PER_PAGE = 16


def _event_message(event, team_emoji, home_emoji, away_emoji, running_home_goals, running_home_behinds,
                    running_away_goals, running_away_behinds):
    """Plain-text live update line, e.g.:
    "6:25 - GOAL! :Hawks: **Nate Caddy** - :Hawks: 2.2 (20) - :Dees: 1.2 (12)"
    Running score already includes this event."""
    p = event.player
    clock = _clock(event.minute)
    score = f"{home_emoji}{format_score(running_home_goals, running_home_behinds)} - {away_emoji}{format_score(running_away_goals, running_away_behinds)}"

    if event.kind == "goal":
        return f"{clock} - GOAL! {team_emoji}**{p.name}** - {score}"
    if event.kind == "behind":
        if event.rushed:
            # No individual credit for a rushed behind - the defense saved
            # the goal, not the shooter's own accuracy (see
            # match_sim.RUSHED_BEHIND_CHANCE) - so no player name shown.
            return f"{clock} - Behind {team_emoji}(Rushed) - {score}"
        return f"{clock} - Behind {team_emoji}**{p.name}** - {score}"
    if event.kind == "report":
        # category only, exact charge/suspension length withheld until the
        # Match Review Panel hands down its sanction - same withhold-the-
        # detail pattern as injuries above.
        return f"{clock} - :rotating_light: REPORTED - {team_emoji}**{p.name}** - {event.report_category}"
    # injury - category only, diagnosis/recovery withheld per design doc §03
    return f"{clock} - :ambulance: INJURY - {team_emoji}**{p.name}** - {event.injury_category}"


def _clock(minute):
    total_seconds = int(minute * 60)
    return f"{total_seconds // 60}:{total_seconds % 60:02d}"


def format_match_result_line(home_emoji, away_emoji, home_goals, home_behinds, away_goals, away_behinds):
    """"{home emoji} 10.10 (70) DEF {away emoji} 9.5 (59)" (DEF BY / DREW for
    an away win / a draw) - shared by build_final_result_embed's title and
    the results-channel poster (post_match_result_to_results_channel) so
    the exact wording only lives in one place."""
    home_score = home_goals * 6 + home_behinds
    away_score = away_goals * 6 + away_behinds
    home_str = format_score(home_goals, home_behinds)
    away_str = format_score(away_goals, away_behinds)

    if home_score > away_score:
        verb = "DEF"
    elif away_score > home_score:
        verb = "DEF BY"
    else:
        verb = "DREW"

    return f"{home_emoji}{home_str} {verb} {away_emoji}{away_str}"


def _rank_box_score_players(data, stat_key):
    """Both teams' players combined into one list, sorted by whichever
    stat is currently selected. "goals" sorts by goals then behinds (the
    G.B display doubles as the tiebreak order, matching how a real
    goalkicking list reads). Module-level (not a MatchCommands method) -
    it's pure and needs no cog/bot state, and _MatchStatsView is opened
    from more than one fixture view (MatchCentreView or _TeamMatchesView),
    which don't share a common "self.cog" path back to the same cog
    instance - keeping this a free function sidesteps that entirely."""
    if stat_key == "goals":
        return sorted(data["players"], key=lambda p: (-p["goals"], -p["behinds"]))
    return sorted(data["players"], key=lambda p: -p[stat_key])


def build_box_score_embed(data, stat_key, page=0):
    """The /matchcentre analogue of build_final_result_embed - shows every
    player who played (not just the top 3 goalkickers/disposal-getters),
    both teams combined into one list sorted by whichever single stat is
    currently selected (stat_key, e.g. "goals"), paginated
    BOX_SCORE_PLAYERS_PER_PAGE at a time rather than relying on
    embed-field chunking. `data` is MatchCommands._fetch_box_score_data's
    return value. Module-level for the same reason as
    _rank_box_score_players above - see that docstring."""
    from commands.season_commands import get_round_name

    result_line = format_match_result_line(
        data["home_emoji"], data["away_emoji"],
        data["home_goals"], data["home_behinds"], data["away_goals"], data["away_behinds"],
    )
    home_score = data["home_goals"] * 6 + data["home_behinds"]
    away_score = data["away_goals"] * 6 + data["away_behinds"]

    embed = discord.Embed(
        title=result_line,
        description=f"{get_round_name(data['round_number'], data['regular_rounds'])}, Season {data['season_number']}",
        color=discord.Color.green() if home_score >= away_score else discord.Color.orange()
    )

    ranked = _rank_box_score_players(data, stat_key)
    start = page * BOX_SCORE_PLAYERS_PER_PAGE
    page_players = ranked[start:start + BOX_SCORE_PLAYERS_PER_PAGE]

    lines = []
    for p in page_players:
        stat_display = f"{p['goals']}.{p['behinds']}" if stat_key == "goals" else str(p[stat_key])
        lines.append(f"{p['emoji']}{p['name']} ({p['ovr']}) — **{stat_display}**")

    total_pages = max(1, -(-len(ranked) // BOX_SCORE_PLAYERS_PER_PAGE))
    field_name = STAT_LABELS[stat_key]
    if total_pages > 1:
        field_name += f" (Page {page + 1}/{total_pages})"
    embed.add_field(name=field_name, value="\n".join(lines) or "No players.", inline=False)

    return embed


SPEED_PRESETS = [1, 5, 15]  # seconds - selectable live from the control panel
DEFAULT_SPEED_SECONDS = 15  # matches the original fixed BASELINE_DELAY_SECONDS


def _paced_delay(minutes_apart, baseline_seconds=DEFAULT_SPEED_SECONDS):
    """Real-world delay for a simulated gap of `minutes_apart` sim-minutes -
    a flat baseline (reading time - adjustable live via the control panel's
    speed buttons, see LiveMatchState.speed_seconds) plus the scaled sim-time
    gap, so even back-to-back events always get the full baseline on top of
    whatever the gap itself is worth. Same formula used for the gap between
    two events, the quarter-start-to-first-event gap, and the
    last-event-to-quarter-end gap (design doc §04)."""
    return baseline_seconds + minutes_apart * REAL_SECONDS_PER_SIM_MINUTE


class LiveMatchState:
    """Holds everything the control panel and posting loop share for one
    live match. Pure in-memory - lost on bot restart (design doc §09)."""

    def __init__(self, home_name, away_name, home_emoji, away_emoji, result, events, variance, quarter_lengths,
                 home_lineup=None, away_lineup=None, league_avg_ovr=None, home_ground_advantage=True,
                 match_id=None, home_team_id=None, away_team_id=None, current_round=None, season_id=None,
                 is_finals=False, finals_slot_code=None, sim_panel_view=None):
        self.home_name = home_name
        self.away_name = away_name
        self.home_emoji = home_emoji
        self.away_emoji = away_emoji

        # Set only for a real round match (via /matchsimulation) - None for
        # a /scratchmatch preview. _post_final_result uses this to decide
        # whether the result needs persisting (see MatchCommands._persist_match_result).
        self.match_id = match_id
        self.home_team_id = home_team_id
        self.away_team_id = away_team_id
        self.current_round = current_round
        self.season_id = season_id

        # True for a finals-round match (current_round > regular_rounds) -
        # always False for /scratchmatch previews and regular-season round
        # matches. Drives two things: LiveMatchControlView disables "End
        # Match as Draw" so a finals draw can only be resolved via extra
        # time (see accept_draw_button's _refresh_button_states check), and
        # _post_final_result triggers finalize_finals_ladder once the Grand
        # Final specifically (finals_slot_code == "GF") is decided.
        # finals_slot_code is the bracket slot ("WC1", "SF2", "GF", etc,
        # see finals_bracket.py) - used for the live intro embed's title
        # and to detect the GF match specifically.
        self.is_finals = is_finals
        self.finals_slot_code = finals_slot_code

        # The /matchsimulation panel (MatchSimulationView) this live match
        # was started from, if any - None for a /scratchmatch preview.
        # _post_final_result refreshes it once the match ends, since a live
        # match plays out over real time via its own control panel and
        # returns control to the admin's command long before that happens -
        # unlike the batch/Result Only path, nothing else would tell the
        # panel its "Advance to Next Round" button should unlock.
        self.sim_panel_view = sim_panel_view
        self.result = result
        self.events_by_quarter = _group_events_by_quarter(events)
        self.variance = variance
        self.quarter_lengths = quarter_lengths  # index 0 = Q1's rolled length in minutes

        self.quarter = 1
        self.status = "idle"  # idle / running / paused / finished / abandoned / draw_pending
        self.skip_quarter_requested = False
        self.skip_match_requested = False
        self.abandon_requested = False
        self.speed_seconds = DEFAULT_SPEED_SECONDS  # baseline delay between posts - live-adjustable via the panel

        # Needed to construct fresh Player objects for extra time - see
        # match_sim.simulate_extra_time_half(). None for anything that never
        # ends up needing extra time (kept optional so existing call sites/
        # tests that don't pass these don't break).
        self.home_lineup = home_lineup
        self.away_lineup = away_lineup
        self.league_avg_ovr = league_avg_ovr
        self.home_ground_advantage = home_ground_advantage

        # Extra time (finals-only draw-breaker, see design doc addendum) -
        # only ever used once state.status == "draw_pending" or later.
        self.extra_time_period = 0  # 1st pair of halves = period 1, 2nd pair = period 2, etc.
        self.extra_time_half = 0  # 1 or 2 within the current period
        self.extra_time_events = []  # events for the CURRENT half only, cleared each half
        self.extra_time_requested = False  # admin pressed "Start Extra Time"
        self.draw_accepted = False  # admin pressed "End Match as Draw"

    def score_through_quarter(self, quarter):
        events_so_far = [e for q in range(1, quarter + 1) for e in self.events_by_quarter[q]]
        return _quarter_score_summary(events_so_far, self.home_name, self.away_name, self.home_emoji, self.away_emoji)

    def status_line(self):
        if self.status == "finished":
            return "Full time."
        if self.status == "abandoned":
            return "Match abandoned."
        if self.status == "draw_pending":
            return "Scores level at full time — extra time?"
        if self.status == "extra_time_idle":
            return f"Extra Time — Period {self.extra_time_period}, Half {self.extra_time_half} — not started"
        if self.status == "extra_time_running":
            return f"Extra Time — Period {self.extra_time_period}, Half {self.extra_time_half} — in progress"
        if self.status == "extra_time_paused":
            return f"Extra Time — Period {self.extra_time_period}, Half {self.extra_time_half} — paused"
        if self.status == "idle":
            return f"Q{self.quarter} — not started"
        if self.status == "paused":
            return f"Q{self.quarter} — paused"
        return f"Q{self.quarter} — in progress"


class LiveMatchControlView(discord.ui.View):
    """Quarter-gated control panel for one live match - Start Qtr, Pause/
    Resume, Skip to end of quarter, Skip to end of match, Abandon Match.
    See the Live Match Feed design doc §07 for the full behavior spec."""

    def __init__(self, cog, state, feed_channel):
        super().__init__(timeout=None)
        self.cog = cog
        self.state = state
        self.feed_channel = feed_channel
        self._refresh_button_states()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await is_admin_user(interaction):
            await interaction.response.send_message(
                "❌ You need admin permissions to control a live match.", ephemeral=True
            )
            return False
        return True

    def _refresh_button_states(self):
        finished = self.state.status in ("finished", "abandoned")
        draw_pending = self.state.status == "draw_pending"
        idle = self.state.status in ("idle", "extra_time_idle") and not finished
        running_or_paused = self.state.status in ("running", "paused", "extra_time_running", "extra_time_paused")

        # Start Qtr doubles as "Start Half" once in extra time - same
        # action (advance from idle to running), just a different label so
        # it reads correctly in both contexts.
        self.start_qtr_button.disabled = finished or draw_pending or not idle
        self.start_qtr_button.label = "Start Half" if self.state.status == "extra_time_idle" else "Start Qtr"

        self.pause_resume_button.disabled = finished or draw_pending or not running_or_paused
        self.pause_resume_button.label = "Resume" if self.state.status in ("paused", "extra_time_paused") else "Pause"

        self.skip_qtr_button.disabled = finished or draw_pending
        self.skip_qtr_button.label = "Skip to End of Half" if self.state.status in ("extra_time_idle", "extra_time_running", "extra_time_paused") else "Skip to End of Qtr"

        self.skip_match_button.disabled = finished or draw_pending
        self.abandon_button.disabled = finished

        # Speed buttons stay enabled any time the match isn't over - takes
        # effect on the very next delay calculated, mid-quarter included
        # (state.speed_seconds is read fresh at each _paced_delay call).
        for button, seconds in ((self.speed_1s_button, 1), (self.speed_5s_button, 5), (self.speed_15s_button, 15)):
            button.disabled = finished
            button.style = discord.ButtonStyle.green if self.state.speed_seconds == seconds else discord.ButtonStyle.secondary

        # Draw-resolution buttons - only meaningful/enabled while a draw is
        # actually pending a decision. A finals match must produce a real
        # winner (see LiveMatchState.is_finals), so "End Match as Draw" is
        # never offered there - Start Extra Time is the only path forward.
        self.start_extra_time_button.disabled = not draw_pending
        self.accept_draw_button.disabled = not draw_pending or self.state.is_finals

    def panel_embed(self):
        embed = discord.Embed(
            title=f"🎛️ Live Match Control — {self.state.home_name} vs {self.state.away_name}",
            color=discord.Color.blurple(),
        )
        # No score field here - the panel only refreshes on button presses
        # and quarter boundaries, not on every event, so a mid-quarter score
        # would show events that haven't actually posted to the feed
        # channel yet. Score is always visible in the feed channel itself.
        embed.add_field(name="Status", value=self.state.status_line(), inline=False)
        embed.add_field(name="Speed", value=f"{self.state.speed_seconds}s between updates", inline=False)
        return embed

    async def _refresh_panel_message(self, interaction=None):
        self._refresh_button_states()
        embed = self.panel_embed()
        if interaction is not None and not interaction.response.is_done():
            await interaction.response.edit_message(embed=embed, view=self)
        elif self.message is not None:
            await self.message.edit(embed=embed, view=self)

    message = None  # set by the caller after the initial post

    async def _start_idle_period(self, interaction=None):
        """Advances the current idle period (a quarter waiting on "Start
        Qtr" or an extra-time half waiting on "Start Half") into running -
        called by the Start Qtr/Start Half button press."""
        if self.state.status == "extra_time_idle":
            self.state.status = "extra_time_running"
            await self._refresh_panel_message(interaction)
            asyncio.create_task(self.cog.run_extra_time_half(self.state, self))
            return
        if self.state.status != "idle" or self.state.quarter > 4:
            if interaction is not None:
                await interaction.response.defer()
            return
        self.state.status = "running"
        await self._refresh_panel_message(interaction)
        asyncio.create_task(self.cog.run_quarter(self.state, self))

    @discord.ui.button(label="Start Qtr", style=discord.ButtonStyle.green)
    async def start_qtr_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._start_idle_period(interaction)

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.secondary)
    async def pause_resume_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.state.status == "running":
            self.state.status = "paused"
        elif self.state.status == "paused":
            self.state.status = "running"
        elif self.state.status == "extra_time_running":
            self.state.status = "extra_time_paused"
        elif self.state.status == "extra_time_paused":
            self.state.status = "extra_time_running"
        else:
            # Nothing running yet (idle/extra_time_idle) or already
            # finished - Pause has nothing to do.
            await interaction.response.defer()
            return
        await self._refresh_panel_message(interaction)

    @discord.ui.button(label="Skip to End of Qtr", style=discord.ButtonStyle.secondary)
    async def skip_qtr_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.state.status in ("finished", "abandoned", "draw_pending"):
            await interaction.response.defer()
            return
        was_idle = self.state.status in ("idle", "extra_time_idle")
        await interaction.response.defer()
        if not was_idle and self.feed_channel is not None:
            await self.feed_channel.send("*Skipping...*")

        if self.state.status == "extra_time_idle":
            # Unlike a never-started regular quarter (whose score was
            # already computed up front by simulate_match_with_events, so
            # finish_quarter can resolve it with nothing left to simulate),
            # an extra-time half is only ever simulated lazily inside
            # run_extra_time_half itself (see simulate_extra_time_half) - it
            # hasn't happened yet at all here. Calling finish_extra_time_half
            # directly would skip the simulation entirely, leaving the score
            # exactly as it was at the end of the previous half/Q4 and
            # posting nothing. Route through run_extra_time_half (with the
            # skip flag already set) so the half is actually simulated and
            # its result applied to state.result - the flag makes it jump
            # straight past posting individual events, same as skipping a
            # half already in progress.
            self.state.skip_quarter_requested = True
            self.state.status = "extra_time_running"
            await self._refresh_panel_message()
            asyncio.create_task(self.cog.run_extra_time_half(self.state, self))
            return
        if self.state.status in ("extra_time_running", "extra_time_paused"):
            self.state.skip_quarter_requested = True
            return

        if self.state.status == "idle":
            # Quarter never started - resolve it silently right here, same
            # end state as a normal/skipped in-progress quarter. Still a
            # skip as far as finish_quarter's own "was this skipped"
            # check is concerned (see its Q4 embed-posting logic), so the
            # flag is set here too even though nothing was actually
            # in-flight to interrupt.
            self.state.skip_quarter_requested = True
            await self.cog.finish_quarter(self.state, self)
            return
        self.state.skip_quarter_requested = True

    @discord.ui.button(label="Skip to End of Match", style=discord.ButtonStyle.secondary)
    async def skip_match_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.state.status in ("finished", "abandoned", "draw_pending"):
            await interaction.response.defer()
            return
        was_idle = self.state.status in ("idle", "extra_time_idle")
        await interaction.response.defer()
        if not was_idle and self.feed_channel is not None:
            await self.feed_channel.send("*Skipping...*")
        self.state.skip_match_requested = True
        if was_idle:
            await self.cog.finish_match_via_skip(self.state, self)

    @discord.ui.button(label="Start Extra Time", style=discord.ButtonStyle.green, row=2)
    async def start_extra_time_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.state.status != "draw_pending":
            await interaction.response.defer()
            return
        self.state.extra_time_requested = True
        self.state.extra_time_period += 1
        self.state.extra_time_half = 1
        self.state.status = "extra_time_idle"
        await self._refresh_panel_message(interaction)

    @discord.ui.button(label="End Match as Draw", style=discord.ButtonStyle.secondary, row=2)
    async def accept_draw_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.state.status != "draw_pending" or self.state.is_finals:
            await interaction.response.defer()
            return
        self.state.status = "finished"
        await self._refresh_panel_message(interaction)
        await self.cog._post_final_result(self.state, self)
        global _active_live_match
        _active_live_match = None

    @discord.ui.button(label="Abandon Match", style=discord.ButtonStyle.danger)
    async def abandon_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        confirm_view = AbandonConfirmView(self)
        await interaction.response.send_message(
            "Abandon this match? Scores will not be saved to the database.",
            view=confirm_view,
            ephemeral=True,
        )

    # Speed presets - live-adjustable pacing (see _paced_delay/SPEED_PRESETS).
    # Row 1 (row 0 is already full with the 5 buttons above).
    @discord.ui.button(label="1s", style=discord.ButtonStyle.secondary, row=1)
    async def speed_1s_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.state.speed_seconds = 1
        await self._refresh_panel_message(interaction)

    @discord.ui.button(label="5s", style=discord.ButtonStyle.secondary, row=1)
    async def speed_5s_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.state.speed_seconds = 5
        await self._refresh_panel_message(interaction)

    @discord.ui.button(label="15s", style=discord.ButtonStyle.secondary, row=1)
    async def speed_15s_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.state.speed_seconds = 15
        await self._refresh_panel_message(interaction)


class AbandonConfirmView(discord.ui.View):
    def __init__(self, control_view):
        super().__init__(timeout=60)
        self.control_view = control_view

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await is_admin_user(interaction)

    @discord.ui.button(label="Abandon Match", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Match abandoned.", view=self)
        state = self.control_view.state
        state.abandon_requested = True
        state.status = "abandoned"
        await self.control_view._refresh_panel_message()
        feed_channel = self.control_view.feed_channel
        if feed_channel is not None:
            await feed_channel.send(embed=discord.Embed(
                title="MATCH ABANDONED",
                description=f"{state.home_name} vs {state.away_name} — this match was abandoned before completion.",
                color=discord.Color.red(),
            ))
        global _active_live_match
        _active_live_match = None

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Not abandoned.", view=self)


class MatchCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def is_admin(self, interaction: discord.Interaction) -> bool:
        return await is_admin_user(interaction)

    async def team_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for team names (exclude Draft Pool - it has no real lineup)"""
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT team_name FROM teams WHERE team_name != 'Draft Pool' ORDER BY team_name"
            )
            teams = await cursor.fetchall()

        choices = [
            app_commands.Choice(name=team_name, value=team_name)
            for (team_name,) in teams
            if current.lower() in team_name.lower()
        ]
        return choices[:25]

    async def get_team_lineup(self, db, team_id):
        """Fetch a team's current lineup as (player_id, name, position, overall_rating, slot) rows"""
        cursor = await db.execute(
            """SELECT p.player_id, p.name, p.position, p.overall_rating, l.position_name
               FROM lineups l
               JOIN players p ON l.player_id = p.player_id AND p.team_id = l.team_id
               WHERE l.team_id = ?""",
            (team_id,)
        )
        return await cursor.fetchall()

    async def _persist_match_result(self, db, match_id, home_team_id, away_team_id, result, events, current_round):
        """Writes a simulated round match's outcome to the database - the
        one place that turns match_sim.py's in-memory result into durable
        state. Called once a match is fully decided (immediately for a
        batch-resolved round match, or from _post_final_result once a live
        one reaches Full Time). Writes:
          - matches.home_score/away_score/simulated
          - one player_match_stats row per player who had a lineup slot
            (TeamMatchResult.stat_lines already covers everyone who took
            the field, not just those with a nonzero stat), including
            brownlow_votes - 3/2/1 to the match's top 3 (both teams ranked
            together, see match_sim.MatchResult.brownlow_votes) - and
            best_fairest_votes - 5/4/3/2/1 to EACH team's own top 5,
            voted separately per team (see
            match_sim.TeamMatchResult.best_and_fairest_votes) - 0 for
            everyone else in both cases
          - one injuries row per injury MatchEvent the sim rolled, mirroring
            /addinjury's return_round math exactly (injury_round=current_round,
            return_round=current_round+recovery_weeks)
          - one suspensions row per report MatchEvent the sim rolled (TBC
            games_missed/games_removed, resolved the same way injuries are -
            see advance_to_next_round's _roll_pending_report_suspensions)
        Idempotent is NOT guaranteed by this function alone - the caller
        (round-sim command) is responsible for only calling this once per
        match (matches.simulated is the guard callers should check first).
        That guard only covers the bot's own command surface though - if a
        match_id is EVER re-simulated by some other path (e.g. /importdb
        resetting matches.simulated back to 0 for a re-import), the old
        player_match_stats rows for it are cleared first so a player who
        was in the lineup for an earlier simulation but not this one
        doesn't linger as a false "played" record - INSERT OR REPLACE alone
        only overwrites rows for players present in BOTH simulations, never
        removes ones that dropped out."""
        await db.execute("DELETE FROM player_match_stats WHERE match_id = ?", (match_id,))

        await db.execute(
            "UPDATE matches SET home_score = ?, away_score = ?, simulated = 1 WHERE match_id = ?",
            (result.home.score, result.away.score, match_id)
        )

        brownlow_votes_by_player_id = result.brownlow_votes()

        for team_id, team_result in ((home_team_id, result.home), (away_team_id, result.away)):
            # Best & fairest is voted per-TEAM (each club's own 5-4-3-2-1
            # among its own 23 players), unlike brownlow_votes above which
            # ranks both teams together in one combined pool.
            best_fairest_votes_by_player_id = team_result.best_and_fairest_votes()
            for stat_line in team_result.stat_lines.values():
                p = stat_line.player
                await db.execute(
                    """INSERT OR REPLACE INTO player_match_stats
                       (match_id, player_id, team_id, disposals, goals, behinds, marks, tackles, spoils, hitouts, brownlow_votes, best_fairest_votes)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (match_id, p.player_id, team_id, stat_line.disposals, stat_line.goals, stat_line.behinds,
                     stat_line.marks, stat_line.tackles, stat_line.spoils, stat_line.hitouts,
                     brownlow_votes_by_player_id.get(p.player_id, 0),
                     best_fairest_votes_by_player_id.get(p.player_id, 0))
                )

        for event in events:
            if event.kind != "injury":
                continue
            # Recovery length is deliberately left TBC here (recovery_rounds/
            # return_round both NULL) - a natural in-match injury's actual
            # weeks-out isn't decided until the round it happened in is fully
            # over (see advance_to_next_round's _roll_pending_injury_recoveries),
            # matching "the full extent of the injury takes time to assess."
            # event.injury_recovery_weeks is intentionally NOT used here even
            # though match_sim.py already rolled it - it gets re-rolled for
            # real at reveal time instead (see that function for why: doing
            # it there, after injury_round has fully ended, means the return
            # round math no longer needs the +1 correction /addinjury needs -
            # current_round + recovery_weeks is already correct once
            # current_round has moved past the injury round).
            await db.execute(
                """INSERT INTO injuries (player_id, injury_type, injury_round, recovery_rounds, return_round, status)
                   VALUES (?, ?, ?, NULL, NULL, 'injured')""",
                (event.player.player_id, event.injury_diagnosis, current_round)
            )

        for event in events:
            if event.kind != "report":
                continue
            # Suspension length is deliberately left TBC here (games_missed/
            # games_remaining both NULL), exactly mirroring the injuries
            # loop above - the Match Review Panel's actual sanction isn't
            # decided until the round it happened in is fully over (see
            # advance_to_next_round's _roll_pending_report_suspensions).
            # event.report_suspension_games is intentionally NOT used here
            # even though match_sim.py already rolled it, for the same
            # reason event.injury_recovery_weeks isn't used above - it gets
            # re-rolled for real at reveal time instead.
            await db.execute(
                """INSERT INTO suspensions (player_id, suspension_reason, suspension_round, games_missed, games_remaining, status)
                   VALUES (?, ?, ?, NULL, NULL, 'suspended')""",
                (event.player.player_id, event.report_charge, current_round)
            )

        await db.commit()

    async def _post_match_result_to_results_channel(self, db, home_team_id, away_team_id,
                                                      home_goals, home_behinds, away_goals, away_behinds):
        """Posts one line to the configured results channel:
        "{home emoji} 10.10 (70) DEF {away emoji} 9.5 (59)" - only ever
        called for real in-season matches (round-sim's batch/live paths),
        never for /scratchmatch, since those never reach this code at all."""
        cursor = await db.execute(
            "SELECT setting_value FROM settings WHERE setting_key = 'results_channel_id'"
        )
        channel_row = await cursor.fetchone()
        if not channel_row or not channel_row[0]:
            return
        results_channel = self.bot.get_channel(int(channel_row[0]))
        if not results_channel:
            return

        cursor = await db.execute("SELECT emoji_id FROM teams WHERE team_id = ?", (home_team_id,))
        home_emoji_id = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT emoji_id FROM teams WHERE team_id = ?", (away_team_id,))
        away_emoji_id = (await cursor.fetchone())[0]
        home_emoji = get_team_emoji_str(self.bot, home_emoji_id)
        away_emoji = get_team_emoji_str(self.bot, away_emoji_id)

        line = format_match_result_line(home_emoji, away_emoji, home_goals, home_behinds, away_goals, away_behinds)
        await results_channel.send(line)

    async def _run_full_round_simulation(self, unsimulated, season_id, current_round, panel_view):
        """Simulates a round's remaining matches one at a time - sim, persist,
        post to the results channel, wait the configured delay, then move to
        the next - rather than simulating the whole round up front and only
        staggering the posts. Used by /matchsimulation's Sim Full Round
        button. Runs as a background task (fire-and-forget from the caller)
        since the admin's own command response shouldn't wait through the
        whole round.
        unsimulated is a list of (match_id, simulated, home_team_id,
        away_team_id, home_name, away_name) rows. Refreshes the panel after
        each match so "Advance to Next Round" only unlocks once the last one
        lands, and posts the completion separator at the end (see
        _post_separator_if_round_complete). The round header (see
        _post_round_header_if_first_result) is checked right after the
        FIRST match here is actually simulated+persisted (it needs that -
        "first result" is detected by counting already-simulated matches,
        which only means anything once there's one to count), with its own
        pause (same delay) before that match's result posts - same pacing as
        every later gap between results, so the header doesn't read as
        instant/disconnected from what follows it. If some of the round's
        matches were already simulated individually before Sim Full Round
        was pressed, the header has already posted from that earlier call
        and this one correctly no-ops (see the function's own doc).
        Delay is /config's result_delay_seconds (default 15s if unset); 0
        means no delay at all - every result posts back-to-back."""
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT setting_value FROM settings WHERE setting_key = 'result_delay_seconds'"
            )
            delay_setting = await cursor.fetchone()
        delay_seconds = int(delay_setting[0]) if delay_setting and delay_setting[0] is not None else 15

        for i, (match_id, simulated, home_team_id, away_team_id, home_name, away_name) in enumerate(unsimulated):
            if i > 0 and delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            async with aiosqlite.connect(DB_PATH) as db:
                result = await self._simulate_match_for_round(
                    db, match_id, home_team_id, away_team_id, current_round
                )
                if i == 0:
                    await self._post_round_header_if_first_result(db, season_id, current_round)
                    if delay_seconds > 0:
                        await asyncio.sleep(delay_seconds)
                await self._post_match_result_to_results_channel(
                    db, home_team_id, away_team_id,
                    result.home.goals, result.home.behinds, result.away.goals, result.away.behinds,
                )
            await panel_view._refresh_panel()

        if unsimulated:
            async with aiosqlite.connect(DB_PATH) as db:
                await self._post_separator_if_round_complete(db, season_id, current_round)

    async def _post_round_header_if_first_result(self, db, season_id, current_round):
        """Posts the "# {round name}" header to the results channel the
        moment the round's FIRST match result lands - checked right after
        _post_match_result_to_results_channel at each of the three places a
        match can finish (live, single-match batch, Sim Full Round's
        staggered loop), same "whichever path gets there first wins"
        pattern as _post_separator_if_round_complete's end-of-round
        marker. Previously this posted the instant lineups locked in,
        which could be well before anyone actually simmed anything -
        moved here so the header only appears once real results are about
        to follow it. Must be called AFTER the match has been persisted
        (simulated=1) - "first result" is detected as exactly one
        simulated match in the round existing at call time, not zero,
        since the just-posted match already counts itself. Silently
        no-ops if the results channel isn't configured."""
        cursor = await db.execute(
            "SELECT COUNT(*) FROM matches WHERE season_id = ? AND round_number = ? AND simulated = 1",
            (season_id, current_round)
        )
        simulated_count = (await cursor.fetchone())[0]
        if simulated_count != 1:
            return

        cursor = await db.execute(
            "SELECT setting_value FROM settings WHERE setting_key = 'results_channel_id'"
        )
        channel_row = await cursor.fetchone()
        if not channel_row or not channel_row[0]:
            return
        results_channel = self.bot.get_channel(int(channel_row[0]))
        if not results_channel:
            return

        from commands.season_commands import get_round_name
        cursor = await db.execute(
            "SELECT regular_rounds FROM seasons WHERE season_id = ?", (season_id,)
        )
        regular_rounds = (await cursor.fetchone())[0]
        round_display = get_round_name(current_round, regular_rounds)
        await results_channel.send(f"# {round_display}")

    async def _post_separator_if_round_complete(self, db, season_id, current_round):
        """Posts a "----" divider line to the results channel the moment
        every match in the round has been simulated - checked after each of
        the three ways a match can finish (live, single-match batch, and
        the tail of Sim Full Round's staggered batch above), so whichever
        one happens to resolve the round's LAST unsimulated match is the one
        that posts it, regardless of how the round got finished. Silently
        no-ops if the results channel isn't configured."""
        cursor = await db.execute(
            "SELECT COUNT(*) FROM matches WHERE season_id = ? AND round_number = ? AND simulated = 0",
            (season_id, current_round)
        )
        remaining = (await cursor.fetchone())[0]
        if remaining > 0:
            return

        cursor = await db.execute(
            "SELECT setting_value FROM settings WHERE setting_key = 'results_channel_id'"
        )
        channel_row = await cursor.fetchone()
        if not channel_row or not channel_row[0]:
            return
        results_channel = self.bot.get_channel(int(channel_row[0]))
        if not results_channel:
            return
        await results_channel.send("-" * 57)

    async def get_match_sim_variance(self, db):
        cursor = await db.execute(
            "SELECT setting_value FROM settings WHERE setting_key = 'match_sim_variance'"
        )
        result = await cursor.fetchone()
        if not result or not result[0]:
            return DEFAULT_VARIANCE
        try:
            return float(result[0])
        except ValueError:
            return DEFAULT_VARIANCE

    async def get_live_match_channels(self, db, guild):
        """Returns (control_channel, feed_channel), either of which may be
        None if not configured or no longer resolvable."""
        cursor = await db.execute(
            """SELECT setting_key, setting_value FROM settings
               WHERE setting_key IN ('live_match_control_channel_id', 'live_match_feed_channel_id')"""
        )
        settings = {key: value for key, value in await cursor.fetchall()}

        control_id = settings.get('live_match_control_channel_id')
        feed_id = settings.get('live_match_feed_channel_id')
        control_channel = guild.get_channel(int(control_id)) if control_id else None
        feed_channel = guild.get_channel(int(feed_id)) if feed_id else None
        return control_channel, feed_channel

    def build_final_result_embed(self, team1_name, team2_name, result, home_emoji, away_emoji):
        home, away = result.home, result.away

        result_line = format_match_result_line(home_emoji, away_emoji, home.goals, home.behinds, away.goals, away.behinds)
        title = f"Full time - {result_line}"

        embed = discord.Embed(
            title=title,
            color=discord.Color.green() if home.score >= away.score else discord.Color.orange()
        )

        # Goals + Disposals combined into one field per team, rather than 4
        # separate fields - Discord computes each row's column widths
        # independently, so two side-by-side rows of inline fields don't
        # reliably line up with each other; one field per team sidesteps
        # that entirely.
        for team_name, team_result, emoji in ((team1_name, home, home_emoji), (team2_name, away, away_emoji)):
            sections = []

            top_scorers = sorted(
                (s for s in team_result.stat_lines.values() if s.goals > 0),
                key=lambda s: (-s.goals, -s.behinds)
            )[:3]
            if top_scorers:
                sections.append("**Goals**\n" + "\n".join(f"{s.player.name} {s.goals}.{s.behinds}" for s in top_scorers))

            top_disposals = sorted(
                team_result.stat_lines.values(),
                key=lambda s: -s.disposals
            )[:3]
            if top_disposals:
                sections.append("**Disposals**\n" + "\n".join(f"{s.player.name} {s.disposals}" for s in top_disposals))

            if sections:
                embed.add_field(
                    name=f"{emoji}{team_name}",
                    value="\n\n".join(sections),
                    inline=True
                )

        return embed

    async def _fetch_box_score_data(self, db, match_id):
        """Fetches everything build_box_score_embed needs to render any
        stat view of a completed match - returns None if the match has no
        player_match_stats rows (not yet simulated). Split from the embed
        builder itself so /matchcentre's stat-switch buttons can re-render
        without re-querying the DB on every click - the match stats view
        holds onto this dict and just rebuilds the embed from it."""
        from commands.season_commands import get_round_name

        cursor = await db.execute(
            """SELECT m.round_number, m.home_team_id, m.away_team_id, m.home_score, m.away_score,
                      s.season_number, s.regular_rounds, h.team_name, h.emoji_id, a.team_name, a.emoji_id
               FROM matches m
               JOIN seasons s ON m.season_id = s.season_id
               JOIN teams h ON m.home_team_id = h.team_id
               JOIN teams a ON m.away_team_id = a.team_id
               WHERE m.match_id = ?""",
            (match_id,)
        )
        match_row = await cursor.fetchone()
        if not match_row:
            return None
        (round_number, home_team_id, away_team_id, home_score, away_score, season_number, regular_rounds,
         home_name, home_emoji_id, away_name, away_emoji_id) = match_row

        cursor = await db.execute(
            """SELECT pms.team_id, p.name, p.overall_rating, pms.disposals, pms.goals, pms.behinds,
                      pms.marks, pms.tackles, pms.spoils, pms.hitouts
               FROM player_match_stats pms
               JOIN players p ON pms.player_id = p.player_id
               WHERE pms.match_id = ?""",
            (match_id,)
        )
        stat_rows = await cursor.fetchall()
        if not stat_rows:
            return None

        # matches.home_score/away_score are the authoritative combined AFL
        # score - a rushed behind (match_sim.py's RUSHED_BEHIND_CHANCE)
        # counts on the scoreboard but is deliberately NOT credited to any
        # player's stat line, so summing behinds straight from
        # player_match_stats silently undercounts by 1-4 points per match.
        # Goals ARE always credited to a player, so: sum goals from the
        # player rows (accurate), then derive the true behind count as
        # score - goals*6 using the authoritative match score.
        home_goals = away_goals = 0
        team_emoji_by_id = {
            home_team_id: get_team_emoji_str(self.bot, home_emoji_id),
            away_team_id: get_team_emoji_str(self.bot, away_emoji_id),
        }
        players = []
        for team_id, name, ovr, disposals, goals, behinds, marks, tackles, spoils, hitouts in stat_rows:
            if team_id == home_team_id:
                home_goals += goals
            else:
                away_goals += goals
            players.append({
                "name": name, "ovr": ovr, "emoji": team_emoji_by_id[team_id],
                "disposals": disposals, "goals": goals, "behinds": behinds,
                "marks": marks, "tackles": tackles, "spoils": spoils, "hitouts": hitouts,
            })

        return {
            "round_number": round_number, "season_number": season_number, "regular_rounds": regular_rounds,
            "home_name": home_name, "away_name": away_name,
            "home_emoji": team_emoji_by_id[home_team_id], "away_emoji": team_emoji_by_id[away_team_id],
            "home_goals": home_goals, "home_behinds": home_score - home_goals * 6,
            "away_goals": away_goals, "away_behinds": away_score - away_goals * 6,
            "players": players,
        }

    async def run_quarter(self, state: LiveMatchState, view: LiveMatchControlView):
        """Posts one quarter's events to the feed channel, paced per §04,
        checking in with `state` between every post so Pause/Skip/Abandon
        can interrupt it. Runs as a background task kicked off by Start Qtr."""
        # After-siren events (see match_sim.AFTER_SIREN_SHOT_CHANCE_*) are
        # handled separately, AFTER the SIREN line posts below - they must
        # never be treated as just another event in the normal pre-siren
        # timeline, or they'd end up posting before the siren that's
        # supposed to precede them.
        events = [e for e in state.events_by_quarter[state.quarter] if not e.after_siren]
        after_siren_events = [e for e in state.events_by_quarter[state.quarter] if e.after_siren]
        feed_channel = view.feed_channel

        if feed_channel is not None:
            await feed_channel.send(f"**{QUARTER_START_LABELS[state.quarter]}**")

        if events:
            await self._wait_with_controls(state, _paced_delay(events[0].minute, state.speed_seconds))
            if state.abandon_requested:
                return

        # Running score starts from every prior quarter's total - each event
        # within this quarter adds to it as it's posted, so the message
        # always shows the score AS OF that event, not the eventual final.
        home_goals = home_behinds = away_goals = away_behinds = 0
        if state.quarter > 1:
            prior_events = [e for q in range(1, state.quarter) for e in state.events_by_quarter[q]]
            home_goals = sum(1 for e in prior_events if e.kind == "goal" and e.team_name == state.home_name)
            home_behinds = sum(1 for e in prior_events if e.kind == "behind" and e.team_name == state.home_name)
            away_goals = sum(1 for e in prior_events if e.kind == "goal" and e.team_name == state.away_name)
            away_behinds = sum(1 for e in prior_events if e.kind == "behind" and e.team_name == state.away_name)

        for i, event in enumerate(events):
            if state.abandon_requested:
                return
            if state.skip_quarter_requested or state.skip_match_requested:
                break

            is_home = event.team_name == state.home_name
            if event.kind == "goal":
                if is_home:
                    home_goals += 1
                else:
                    away_goals += 1
            elif event.kind == "behind":
                if is_home:
                    home_behinds += 1
                else:
                    away_behinds += 1

            if feed_channel is not None:
                team_emoji = state.home_emoji if is_home else state.away_emoji
                message = _event_message(
                    event, team_emoji, state.home_emoji, state.away_emoji,
                    home_goals, home_behinds, away_goals, away_behinds,
                )
                await feed_channel.send(message)

            if i == len(events) - 1:
                break

            next_event = events[i + 1]
            delay = _paced_delay(next_event.minute - event.minute, state.speed_seconds)
            await self._wait_with_controls(state, delay)
            if state.abandon_requested:
                return
            if state.skip_quarter_requested or state.skip_match_requested:
                break

        if state.abandon_requested:
            return

        if not state.skip_quarter_requested and not state.skip_match_requested:
            quarter_length = state.quarter_lengths[state.quarter - 1]
            last_event_minute = events[-1].minute if events else 0.0
            await self._wait_with_controls(state, _paced_delay(quarter_length - last_event_minute, state.speed_seconds))
            if state.abandon_requested:
                return
            if feed_channel is not None:
                await feed_channel.send(f"{_clock(quarter_length)} - 📢 SIREN")

            # After-siren easter egg (see match_sim.AFTER_SIREN_SHOT_CHANCE_*)
            # - SIREN already posted above, then a beat before the audio
            # clip (if this is the Sam Lloyd siren-beater-winner case), then
            # another beat before the actual goal/behind message.
            for event in after_siren_events:
                if state.abandon_requested:
                    return

                is_home = event.team_name == state.home_name
                if event.kind == "goal":
                    if is_home:
                        home_goals += 1
                    else:
                        away_goals += 1
                elif event.kind == "behind":
                    if is_home:
                        home_behinds += 1
                    else:
                        away_behinds += 1

                if event.siren_beater_winner and SAM_LLOYD_AUDIO_PATH is not None:
                    await self._wait_with_controls(state, AFTER_SIREN_AUDIO_DELAY_SECONDS)
                    if state.abandon_requested:
                        return
                    if feed_channel is not None:
                        await feed_channel.send(file=discord.File(SAM_LLOYD_AUDIO_PATH, filename=SAM_LLOYD_AUDIO_DISPLAY_NAME))
                    await self._wait_with_controls(state, AFTER_SIREN_MESSAGE_DELAY_SECONDS)
                    if state.abandon_requested:
                        return
                else:
                    # No audio step for a non-winning after-siren shot - just
                    # the one beat after the siren before the result posts.
                    await self._wait_with_controls(state, AFTER_SIREN_AUDIO_DELAY_SECONDS)
                    if state.abandon_requested:
                        return

                if feed_channel is not None:
                    team_emoji = state.home_emoji if is_home else state.away_emoji
                    message = _event_message(
                        event, team_emoji, state.home_emoji, state.away_emoji,
                        home_goals, home_behinds, away_goals, away_behinds,
                    )
                    await feed_channel.send(message)

        await self.finish_quarter(state, view)

    async def run_extra_time_half(self, state: LiveMatchState, view: LiveMatchControlView):
        """Posts one 3-minute extra-time half, paced the same way as a
        normal quarter (see run_quarter) - genuinely simulated fresh right
        now, not part of the original up-front simulate_match_with_events
        call, since extra time can't be known to be needed (or to have
        ended) until Q4 - or a prior extra-time half - actually plays out.
        See match_sim.simulate_extra_time_half for why the SAME Player
        objects' underlying stat lines keep accumulating rather than a
        fresh match starting."""
        feed_channel = view.feed_channel
        home_players = [Player(*row) for row in state.home_lineup]
        away_players = [Player(*row) for row in state.away_lineup]

        # Displayed period number increments once per half played (Period 1
        # = first half of extra_time_period 1, Period 2 = its second half,
        # Period 3 = first half of extra_time_period 2, etc.) - distinct
        # from state.extra_time_period, which counts PAIRS of halves and
        # drives the actual "check for a result" logic in finish_extra_time_half.
        displayed_period = (state.extra_time_period - 1) * 2 + state.extra_time_half
        # Suppressed when skipping straight from extra_time_idle (Skip to
        # End of Half pressed before Start Half) - same as a skipped
        # never-started regular quarter posting no QUARTER N header either,
        # via finish_quarter's was_idle path in skip_qtr_button.
        skipped_from_idle = state.skip_quarter_requested
        if feed_channel is not None and not skipped_from_idle:
            await feed_channel.send(f"**EXTRA TIME — PERIOD {displayed_period}**")

        # simulate_extra_time_half adds every goal/behind straight into
        # state.result.home/away as it simulates the whole half up front, so
        # by the time we get here the result already reflects the half's
        # END state - snapshot the score as it stood BEFORE the half so the
        # loop below can build a running total that increments per event,
        # same as run_quarter does for a normal quarter.
        home_goals = state.result.home.goals
        home_behinds = state.result.home.behinds
        away_goals = state.result.away.goals
        away_behinds = state.result.away.behinds

        events = simulate_extra_time_half(
            home_players, away_players, state.result.home, state.result.away,
            state.home_name, state.away_name, state.league_avg_ovr, state.variance, random.Random(),
            home_ground_advantage=state.home_ground_advantage,
        )
        state.extra_time_events = events

        if events:
            await self._wait_with_controls(state, _paced_delay(events[0].minute, state.speed_seconds))
            if state.abandon_requested:
                return

        for i, event in enumerate(events):
            if state.abandon_requested:
                return
            if state.skip_quarter_requested or state.skip_match_requested:
                break

            is_home = event.team_name == state.home_name
            if event.kind == "goal":
                if is_home:
                    home_goals += 1
                else:
                    away_goals += 1
            elif event.kind == "behind":
                if is_home:
                    home_behinds += 1
                else:
                    away_behinds += 1

            if feed_channel is not None:
                team_emoji = state.home_emoji if is_home else state.away_emoji
                message = _event_message(
                    event, team_emoji, state.home_emoji, state.away_emoji,
                    home_goals, home_behinds, away_goals, away_behinds,
                )
                await feed_channel.send(message)

            if i == len(events) - 1:
                break

            next_event = events[i + 1]
            delay = _paced_delay(next_event.minute - event.minute, state.speed_seconds)
            await self._wait_with_controls(state, delay)
            if state.abandon_requested:
                return
            if state.skip_quarter_requested or state.skip_match_requested:
                break

        if state.abandon_requested:
            return

        if not state.skip_quarter_requested and not state.skip_match_requested:
            last_event_minute = events[-1].minute if events else 0.0
            await self._wait_with_controls(state, _paced_delay(EXTRA_TIME_HALF_LENGTH_MINUTES - last_event_minute, state.speed_seconds))
            if state.abandon_requested:
                return
            if feed_channel is not None:
                await feed_channel.send(f"{_clock(EXTRA_TIME_HALF_LENGTH_MINUTES)} - 📢 SIREN")

            after_siren_event = simulate_extra_time_after_siren_shot(
                home_players, away_players, state.result.home, state.result.away,
                state.home_name, state.away_name, state.league_avg_ovr, random.Random(),
            )
            if after_siren_event is not None:
                is_home = after_siren_event.team_name == state.home_name
                if after_siren_event.siren_beater_winner and SAM_LLOYD_AUDIO_PATH is not None:
                    await self._wait_with_controls(state, AFTER_SIREN_AUDIO_DELAY_SECONDS)
                    if state.abandon_requested:
                        return
                    if feed_channel is not None:
                        await feed_channel.send(file=discord.File(SAM_LLOYD_AUDIO_PATH, filename=SAM_LLOYD_AUDIO_DISPLAY_NAME))
                    await self._wait_with_controls(state, AFTER_SIREN_MESSAGE_DELAY_SECONDS)
                    if state.abandon_requested:
                        return
                else:
                    await self._wait_with_controls(state, AFTER_SIREN_AUDIO_DELAY_SECONDS)
                    if state.abandon_requested:
                        return

                if feed_channel is not None:
                    team_emoji = state.home_emoji if is_home else state.away_emoji
                    message = _event_message(
                        after_siren_event, team_emoji, state.home_emoji, state.away_emoji,
                        state.result.home.goals, state.result.home.behinds,
                        state.result.away.goals, state.result.away.behinds,
                    )
                    await feed_channel.send(message)

        await self.finish_extra_time_half(state, view)

    async def finish_extra_time_half(self, state: LiveMatchState, view: LiveMatchControlView):
        """Called once one extra-time half's events are done posting -
        advances to the second half of the period, or (after the second
        half) checks whether the scores are still level and either offers
        another period or concludes the match. Every half gets its own
        score-summary embed on the way out (naturally finished or skipped),
        the same "every quarter always gets one" convention finish_quarter
        uses for a normal quarter - previously the FIRST half of a period
        posted nothing at all here, silently falling straight into
        extra_time_idle with no embed, most noticeable when Skip to End of
        Half was used (nothing at all showed the half had ended)."""
        if state.abandon_requested:
            return
        if state.skip_match_requested:
            await self._conclude_or_offer_extra_time(state, view)
            await view._refresh_panel_message()
            return

        state.skip_quarter_requested = False

        # Matches run_extra_time_half's own "PERIOD N" header numbering -
        # Period 1 = first half of extra_time_period 1, Period 2 = its
        # second half, etc - so the half that just ended and the summary
        # labeling it always agree.
        displayed_period = (state.extra_time_period - 1) * 2 + state.extra_time_half

        if state.extra_time_half == 1:
            if view.feed_channel is not None:
                await view.feed_channel.send(embed=discord.Embed(
                    title=f"END OF EXTRA TIME — PERIOD {displayed_period}",
                    description=(
                        f"{state.home_emoji}**{state.home_name}**  {format_score(state.result.home.goals, state.result.home.behinds)}\n"
                        f"{state.away_emoji}**{state.away_name}**  {format_score(state.result.away.goals, state.result.away.behinds)}"
                    ),
                    color=discord.Color.blurple(),
                ))
            state.extra_time_half = 2
            state.status = "extra_time_idle"
        else:
            # Both halves of this period are done - a score-summary posts
            # either way, then either the match is decided or another
            # period is offered.
            if view.feed_channel is not None:
                await view.feed_channel.send(embed=discord.Embed(
                    title=f"END OF EXTRA TIME — PERIOD {displayed_period}",
                    description=(
                        f"{state.home_emoji}**{state.home_name}**  {format_score(state.result.home.goals, state.result.home.behinds)}\n"
                        f"{state.away_emoji}**{state.away_name}**  {format_score(state.result.away.goals, state.result.away.behinds)}"
                    ),
                    color=discord.Color.blurple(),
                ))

            if state.result.home.score == state.result.away.score:
                state.status = "draw_pending"
                state.extra_time_requested = False
            else:
                state.status = "finished"
                await self._post_final_result(state, view)
                global _active_live_match
                _active_live_match = None

        await view._refresh_panel_message()

    async def _wait_with_controls(self, state: LiveMatchState, total_delay):
        """Sleeps out `total_delay` seconds in short polling slices so a
        pause/skip/abandon pressed mid-wait takes effect promptly instead of
        only being noticed after a single long asyncio.sleep completes."""
        elapsed = 0.0
        while elapsed < total_delay:
            if state.abandon_requested or state.skip_quarter_requested or state.skip_match_requested:
                return
            while state.status in ("paused", "extra_time_paused"):
                if state.abandon_requested or state.skip_quarter_requested or state.skip_match_requested:
                    return
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
            slice_len = min(POLL_INTERVAL_SECONDS, total_delay - elapsed)
            await asyncio.sleep(slice_len)
            elapsed += slice_len

    async def finish_quarter(self, state: LiveMatchState, view: LiveMatchControlView):
        """Called once a quarter's events are done posting (normally or via
        skip) - posts the end-of-quarter score summary and either advances
        to idle for the next quarter or wraps up the match at Q4."""
        if state.abandon_requested:
            return

        if state.skip_match_requested:
            await self.finish_match_via_skip(state, view)
            return

        # Q1-3 always get their score summary (QUARTER TIME/HALF TIME/etc),
        # played out naturally or skipped. Q4 is different - the summary
        # embed there is only wanted when Skip to End of Qtr was actually
        # used, or the match ends in a draw (handled below via
        # _conclude_or_offer_extra_time's post_draw_summary) - not on every
        # ordinary Q4 finish, since the full-time result embed that follows
        # already covers a decisive result.
        was_skipped = state.skip_quarter_requested
        if view.feed_channel is not None and (state.quarter < 4 or was_skipped):
            await view.feed_channel.send(embed=discord.Embed(
                title=QUARTER_END_LABELS[state.quarter],
                description=state.score_through_quarter(state.quarter),
                color=discord.Color.blurple(),
            ))

        state.skip_quarter_requested = False

        if state.quarter >= 4:
            # A brief pause after the final siren (or, if an after-siren
            # shot just happened, after ITS goal/behind message - see
            # run_quarter) before the full-time result posts, rather than
            # an instant cut straight into the embed either way.
            if not state.abandon_requested:
                await self._wait_with_controls(state, FULL_TIME_SUMMARY_DELAY_SECONDS)
            if not state.abandon_requested:
                # Already posted the END OF 4TH QUARTER summary above if this
                # was a skip - don't post it again here on a draw. If it
                # WASN'T a skip, nothing has posted yet, so let this call
                # post it after all, but only if it turns out to be a draw.
                await self._conclude_or_offer_extra_time(state, view, post_draw_summary=not was_skipped)
        else:
            state.quarter += 1
            state.status = "idle"

        await view._refresh_panel_message()

    async def _conclude_or_offer_extra_time(self, state: LiveMatchState, view: LiveMatchControlView, post_draw_summary=True):
        """Called once Q4 (or extra time) is genuinely over and about to be
        finalized - checks for a draw first (see design doc addendum on
        extra time) before actually posting a final result. A draw with a
        usable lineup/league_avg_ovr on state offers extra time instead of
        finishing outright. post_draw_summary controls the score-line embed
        posted on a draw - False when the caller (finish_quarter) already
        posted an equivalent END OF 4TH QUARTER summary just before calling
        in, so it isn't posted twice."""
        # Consumed here - a skip-to-end-of-match request only ever means
        # "fast-forward past whatever's currently in progress", not "skip
        # every future extra-time period too". Leaving it True after this
        # point would make the NEXT extra-time half silently skip itself
        # and jump straight back here.
        state.skip_match_requested = False

        if state.result.home.score == state.result.away.score and state.home_lineup is not None:
            state.status = "draw_pending"
            if view.feed_channel is not None and post_draw_summary:
                await view.feed_channel.send(embed=discord.Embed(
                    title="END OF 4TH QUARTER",
                    description=(
                        f"{state.home_emoji}**{state.home_name}**  {format_score(state.result.home.goals, state.result.home.behinds)}\n"
                        f"{state.away_emoji}**{state.away_name}**  {format_score(state.result.away.goals, state.result.away.behinds)}"
                    ),
                    color=discord.Color.blurple(),
                ))
            return

        state.status = "finished"
        await self._post_final_result(state, view)
        global _active_live_match
        _active_live_match = None

    async def finish_match_via_skip(self, state: LiveMatchState, view: LiveMatchControlView):
        """Skip to end of match - posts nothing further per-quarter, jumps
        straight to the final result (design doc §07)."""
        if state.status in ("finished", "abandoned"):
            return
        await self._conclude_or_offer_extra_time(state, view)
        await view._refresh_panel_message()

    async def _post_final_result(self, state: LiveMatchState, view: LiveMatchControlView):
        # The live feed's own full-time summary embed posts FIRST, before
        # anything goes to the results channel - it's the match's own
        # "home ground" record of what just happened, so it should always
        # be up before the more terse results-channel score line and round
        # header appear elsewhere. (Previously this posted LAST, after the
        # results channel had already gotten the score line - reordered
        # per user report.)
        if view.feed_channel is not None:
            embed = self.build_final_result_embed(
                state.home_name, state.away_name, state.result,
                state.home_emoji, state.away_emoji,
            )
            await view.feed_channel.send(embed=embed)

        if state.match_id is not None:
            all_events = [e for q in state.events_by_quarter.values() for e in q]
            async with aiosqlite.connect(DB_PATH) as db:
                await self._persist_match_result(
                    db, state.match_id, state.home_team_id, state.away_team_id,
                    state.result, all_events, state.current_round,
                )
                # Round header must post BEFORE this round's first result
                # line, not after - it's checked here, ahead of
                # _post_match_result_to_results_channel, specifically so a
                # live-simmed first match doesn't post its score before the
                # "# Round N" header that's supposed to introduce it.
                await self._post_round_header_if_first_result(db, state.season_id, state.current_round)
                await self._post_match_result_to_results_channel(
                    db, state.home_team_id, state.away_team_id,
                    state.result.home.goals, state.result.home.behinds,
                    state.result.away.goals, state.result.away.behinds,
                )
                await self._post_separator_if_round_complete(db, state.season_id, state.current_round)

                # The Grand Final just got its decisive result (finals
                # matches are always live-mode and can never be batch-simmed
                # or end in a draw - see LiveMatchState.is_finals) - this is
                # the ONE moment the season's true final 1-10 finish can be
                # computed, since advance_to_next_round's own guard would
                # otherwise refuse to run at all once current_round reaches
                # total_rounds (the GF round IS the last round).
                if state.is_finals and state.finals_slot_code == "GF":
                    from commands.season_commands import finalize_finals_ladder
                    await finalize_finals_ladder(db, state.season_id)

        # Refresh the /matchsimulation panel this live match was started
        # from, if any - this is the moment "Advance to Next Round" should
        # unlock once every match in the round is done. Live matches play
        # out over real time via their own control panel, so nothing else
        # tells the sim panel this match just finished. Best-effort: a live
        # match can run for many minutes, long enough for the ephemeral
        # panel message to become unreachable - that must never block the
        # result itself from being persisted and posted above.
        if state.sim_panel_view is not None:
            try:
                await state.sim_panel_view._refresh_panel()
            except discord.HTTPException:
                pass

    @app_commands.command(name="scratchmatch", description="[ADMIN] Simulate a one-off match between two teams")
    @app_commands.describe(
        team1="First team (treated as the home team)",
        team2="Second team",
        mode="Result only (one final result) or live (paced event feed with a control panel)",
        home_ground_advantage="Give team1 home ground advantage (default: on)",
        force="Force a specific test scenario instead of a natural result. 'After-siren winner' only applies in Live mode."
    )
    @app_commands.rename(force="force_scenario")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Result only (default)", value="batch"),
        app_commands.Choice(name="Live", value="live"),
    ])
    @app_commands.choices(force=[
        app_commands.Choice(name="Draw", value="draw"),
        app_commands.Choice(name="After-siren winner (Live only)", value="after_siren_winner"),
    ])
    @app_commands.autocomplete(team1=team_autocomplete, team2=team_autocomplete)
    async def scratch_match(self, interaction: discord.Interaction, team1: str, team2: str, mode: str = "batch",
                             home_ground_advantage: bool = True, force: str = None):
        if not await self.is_admin(interaction):
            await interaction.response.send_message(
                "❌ You need admin permissions to use this command.",
                ephemeral=True
            )
            return

        if force == "after_siren_winner" and mode != "live":
            await interaction.response.send_message(
                "❌ Forcing an after-siren winner only makes sense in Live mode (Batch has no event feed to show it happening).",
                ephemeral=True
            )
            return

        if mode == "live":
            await self._scratch_match_live(interaction, team1, team2, home_ground_advantage, force)
        else:
            await self._scratch_match_batch(interaction, team1, team2, home_ground_advantage, force)

    @staticmethod
    def _force_draw(result):
        """Nudges the final score to an exact draw, for testing scenarios
        that need one (e.g. extra time only triggers on a real draw). Adds
        behinds via TeamMatchResult.rushed_behinds - a plain scoreboard
        counter that's already invisible on the live event feed by design
        (see the rushed-behind feature), so this never fabricates a fake
        goal/behind event or misattributes a shot to a player who didn't
        take it."""
        deficit = result.home.score - result.away.score
        if deficit > 0:
            result.away.rushed_behinds += deficit
        elif deficit < 0:
            result.home.rushed_behinds += -deficit

    @staticmethod
    def _force_after_siren_winner(result, events, quarter_lengths, home_players, away_players,
                                   home_team_name, away_team_name, rng):
        """Guarantees Q4 ends with a siren-beating winning goal, for testing
        that specific moment (see match_sim.AFTER_SIREN_*) without relying on
        its normal ~10% random chance. Strips out any after-siren event(s)
        that were already naturally rolled for Q4 (undoing their score
        impact first) so exactly one forced event remains, then picks
        whichever team is trailing (or either, if already level) and adds a
        goal for a random on-field player from that side, appended straight
        into `events` so the live feed's existing after-siren posting logic
        (run_quarter, filtering state.events_by_quarter[4] by .after_siren)
        picks it up with no special-casing needed. A single goal only
        genuinely wins the match if the trailing team was within 6 points
        (one goal) to start with, so the pre-siren margin is trimmed down to
        exactly a 1-point deficit first via TeamMatchResult.rushed_behinds
        (see _force_draw) - invisible on the feed, same as a forced draw -
        guaranteeing this is always a real, match-deciding winner, not just
        a garbage-time major that doesn't change the result."""
        quarter = 4
        for event in [e for e in events if e.after_siren and e.quarter == quarter]:
            events.remove(event)
            line = result.home.stat_lines.get(event.player.player_id) if event.team_name == home_team_name \
                else result.away.stat_lines.get(event.player.player_id)
            if line is not None:
                if event.kind == "goal":
                    line.goals -= 1
                elif event.kind == "behind":
                    line.behinds -= 1

        if result.home.score == result.away.score:
            sides = [(home_players, result.home, home_team_name), (away_players, result.away, away_team_name)]
            trailing_players, trailing_result, trailing_team_name = rng.choice(sides)
        elif result.home.score < result.away.score:
            trailing_players, trailing_result, trailing_team_name = home_players, result.home, home_team_name
        else:
            trailing_players, trailing_result, trailing_team_name = away_players, result.away, away_team_name
        team_players, team_result, team_name = trailing_players, trailing_result, trailing_team_name

        # Trim the deficit to exactly 1 point (if it's currently more) so the
        # forced goal below is guaranteed to be a real winner.
        leading_result = result.away if team_result is result.home else result.home
        deficit = leading_result.score - team_result.score
        if deficit > 1:
            team_result.rushed_behinds += (deficit - 1)
        elif deficit < 1:
            leading_result.rushed_behinds += (1 - deficit)

        shooter = rng.choice(team_players)
        line = team_result.stat_lines[shooter.player_id]
        line.goals += 1

        quarter_length = quarter_lengths[quarter - 1]
        minute = quarter_length + 0.05
        match_minute = sum(quarter_lengths[:quarter - 1]) + minute
        event = MatchEvent("goal", quarter, minute, match_minute, team_name, shooter,
                            after_siren=True, siren_beater_winner=True)
        events.append(event)

    async def _resolve_teams_and_lineups(self, interaction, team1, team2):
        """Shared validation for both batch and live modes. Returns
        (team1_name, team1_emoji_id, team1_lineup, team2_name, team2_emoji_id,
        team2_lineup, league_avg_ovr, variance) or None if it already sent an
        error response."""
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT team_id, team_name, emoji_id FROM teams WHERE team_name = ?", (team1,)
            )
            team1_row = await cursor.fetchone()
            cursor = await db.execute(
                "SELECT team_id, team_name, emoji_id FROM teams WHERE team_name = ?", (team2,)
            )
            team2_row = await cursor.fetchone()

            if not team1_row:
                await interaction.followup.send(f"❌ Team '{team1}' not found. Please select from the autocomplete suggestions.", ephemeral=True)
                return None
            if not team2_row:
                await interaction.followup.send(f"❌ Team '{team2}' not found. Please select from the autocomplete suggestions.", ephemeral=True)
                return None
            if team1_row[0] == team2_row[0]:
                await interaction.followup.send("❌ Choose two different teams.", ephemeral=True)
                return None

            team1_id, team1_name, team1_emoji_id = team1_row
            team2_id, team2_name, team2_emoji_id = team2_row

            team1_lineup = await self.get_team_lineup(db, team1_id)
            team2_lineup = await self.get_team_lineup(db, team2_id)

            errors = []
            if len(team1_lineup) < 23:
                errors.append(f"❌ **{team1_name}**'s lineup is incomplete: {23 - len(team1_lineup)} position(s) empty")
            if len(team2_lineup) < 23:
                errors.append(f"❌ **{team2_name}**'s lineup is incomplete: {23 - len(team2_lineup)} position(s) empty")
            if errors:
                await interaction.followup.send("\n".join(errors), ephemeral=True)
                return None

            # Average OVR of players actually selected in a current lineup - not
            # every rostered player, since bench/reserve players who never take
            # the field would drag the baseline below real match-day rosters
            cursor = await db.execute(
                """SELECT AVG(p.overall_rating) FROM lineups l
                   JOIN players p ON l.player_id = p.player_id AND p.team_id = l.team_id"""
            )
            league_avg_result = await cursor.fetchone()
            league_avg_ovr = league_avg_result[0] if league_avg_result and league_avg_result[0] else 85.0

            variance = await self.get_match_sim_variance(db)

        return team1_name, team1_emoji_id, team1_lineup, team2_name, team2_emoji_id, team2_lineup, league_avg_ovr, variance

    async def _scratch_match_batch(self, interaction, team1, team2, home_ground_advantage, force=None):
        await interaction.response.defer(ephemeral=True)

        resolved = await self._resolve_teams_and_lineups(interaction, team1, team2)
        if resolved is None:
            return
        team1_name, team1_emoji_id, team1_lineup, team2_name, team2_emoji_id, team2_lineup, league_avg_ovr, variance = resolved

        result = simulate_match(
            team1_name, team1_lineup,
            team2_name, team2_lineup,
            league_avg_ovr, variance=variance, home_ground_advantage=home_ground_advantage
        )
        if force == "draw":
            self._force_draw(result)

        home_emoji = get_team_emoji_str(self.bot, team1_emoji_id)
        away_emoji = get_team_emoji_str(self.bot, team2_emoji_id)

        embed = self.build_final_result_embed(team1_name, team2_name, result, home_emoji, away_emoji)
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _scratch_match_live(self, interaction, team1, team2, home_ground_advantage, force=None):
        global _active_live_match

        if _active_live_match is not None:
            await interaction.response.send_message(
                "❌ A live match is already in progress. Only one live match can run at a time.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as db:
            control_channel, feed_channel = await self.get_live_match_channels(db, interaction.guild)
        if control_channel is None or feed_channel is None:
            await interaction.followup.send(
                "❌ Live match channels not configured — set them with /config first.",
                ephemeral=True
            )
            return

        resolved = await self._resolve_teams_and_lineups(interaction, team1, team2)
        if resolved is None:
            return
        team1_name, team1_emoji_id, team1_lineup, team2_name, team2_emoji_id, team2_lineup, league_avg_ovr, variance = resolved

        result, events, quarter_lengths = simulate_match_with_events(
            team1_name, team1_lineup,
            team2_name, team2_lineup,
            league_avg_ovr, variance=variance, home_ground_advantage=home_ground_advantage
        )

        force_note = ""
        if force == "draw":
            self._force_draw(result)
            force_note = (
                f"\n⚠️ Forced draw applied to the final score only — quarter-by-quarter "
                f"score displays before Full Time reflect the natural simulated events "
                f"and won't match the forced total until Q4 ends."
            )
        elif force == "after_siren_winner":
            home_players = [Player(*row) for row in team1_lineup]
            away_players = [Player(*row) for row in team2_lineup]
            self._force_after_siren_winner(
                result, events, quarter_lengths, home_players, away_players,
                team1_name, team2_name, random.Random(),
            )
            force_note = "\n⚠️ Q4's after-siren shot has been forced to be a winning goal."

        home_emoji = get_team_emoji_str(self.bot, team1_emoji_id)
        away_emoji = get_team_emoji_str(self.bot, team2_emoji_id)

        await feed_channel.send(embed=discord.Embed(
            title=f"{home_emoji}{team1_name} vs {away_emoji}{team2_name}",
            color=discord.Color.blurple(),
        ))

        state = LiveMatchState(team1_name, team2_name, home_emoji, away_emoji, result, events, variance, quarter_lengths,
                                home_lineup=team1_lineup, away_lineup=team2_lineup, league_avg_ovr=league_avg_ovr,
                                home_ground_advantage=home_ground_advantage)
        view = LiveMatchControlView(self, state, feed_channel)
        panel_message = await control_channel.send(embed=view.panel_embed(), view=view)
        view.message = panel_message
        _active_live_match = state

        if force_note:
            await interaction.followup.send(force_note.lstrip("\n"), ephemeral=True)

    async def _resolve_season(self, db, season_number):
        """Returns (season_id, season_number, current_round) for the given
        season_number, or for the active season if season_number is None.
        Returns None (and lets the caller report the error) if not found."""
        if season_number is not None:
            cursor = await db.execute(
                "SELECT season_id, season_number, current_round FROM seasons WHERE season_number = ?",
                (season_number,)
            )
        else:
            cursor = await db.execute(
                "SELECT season_id, season_number, current_round FROM seasons WHERE status = 'active' LIMIT 1"
            )
        return await cursor.fetchone()


    async def _simulate_match_for_round(self, db, match_id, home_team_id, away_team_id, current_round):
        """Simulates and immediately persists one round match with no live
        feed - used by /matchsimulation's Sim Full Round button. Always goes through
        simulate_match_with_events (not the plain simulate_match /scratchmatch's
        batch mode uses) since injuries only ever get rolled when build_events
        is True (see match_sim.py) - a round match must be able to produce
        them regardless of whether anyone watches it live. Returns the full
        match_sim MatchResult (callers need home/away goals+behinds, not
        just the final score, to post to the results channel)."""
        cursor = await db.execute("SELECT team_name FROM teams WHERE team_id = ?", (home_team_id,))
        home_name = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT team_name FROM teams WHERE team_id = ?", (away_team_id,))
        away_name = (await cursor.fetchone())[0]

        home_lineup = await self.get_team_lineup(db, home_team_id)
        away_lineup = await self.get_team_lineup(db, away_team_id)

        cursor = await db.execute(
            """SELECT AVG(p.overall_rating) FROM lineups l
               JOIN players p ON l.player_id = p.player_id AND p.team_id = l.team_id"""
        )
        league_avg_result = await cursor.fetchone()
        league_avg_ovr = league_avg_result[0] if league_avg_result and league_avg_result[0] else 85.0
        variance = await self.get_match_sim_variance(db)

        result, events, quarter_lengths = simulate_match_with_events(
            home_name, home_lineup, away_name, away_lineup, league_avg_ovr, variance=variance,
        )

        await self._persist_match_result(db, match_id, home_team_id, away_team_id, result, events, current_round)

        return result

    async def _sim_round_match_batch(self, interaction, match_row, current_round, season_id):
        match_id, simulated, home_team_id, away_team_id, home_name, away_name = match_row
        async with aiosqlite.connect(DB_PATH) as db:
            result = await self._simulate_match_for_round(
                db, match_id, home_team_id, away_team_id, current_round
            )
            # Round header must post BEFORE this round's first result line -
            # see _post_final_result's identical ordering for the live-mode
            # path (this is the batch/no-feed equivalent).
            await self._post_round_header_if_first_result(db, season_id, current_round)
            await self._post_match_result_to_results_channel(
                db, home_team_id, away_team_id,
                result.home.goals, result.home.behinds, result.away.goals, result.away.behinds,
            )
            await self._post_separator_if_round_complete(db, season_id, current_round)
        home_score, away_score = result.home.score, result.away.score
        await interaction.followup.send(
            f"✅ **{home_name}** {home_score} - {away_score} **{away_name}**",
            ephemeral=True
        )

    async def _sim_round_match_live(self, interaction, match_row, current_round, season_id, sim_panel_view=None):
        from commands.season_commands import FINALS_SLOT_LABELS

        match_id, simulated, home_team_id, away_team_id, home_name, away_name = match_row
        global _active_live_match

        if _active_live_match is not None:
            await interaction.followup.send(
                "❌ A live match is already in progress. Only one live match can run at a time.",
                ephemeral=True
            )
            return

        async with aiosqlite.connect(DB_PATH) as db:
            control_channel, feed_channel = await self.get_live_match_channels(db, interaction.guild)
            if control_channel is None or feed_channel is None:
                await interaction.followup.send(
                    "❌ Live match channels not configured — set them with /config first.",
                    ephemeral=True
                )
                return

            home_lineup = await self.get_team_lineup(db, home_team_id)
            away_lineup = await self.get_team_lineup(db, away_team_id)
            cursor = await db.execute(
                """SELECT AVG(p.overall_rating) FROM lineups l
                   JOIN players p ON l.player_id = p.player_id AND p.team_id = l.team_id"""
            )
            league_avg_result = await cursor.fetchone()
            league_avg_ovr = league_avg_result[0] if league_avg_result and league_avg_result[0] else 85.0
            variance = await self.get_match_sim_variance(db)

            cursor = await db.execute("SELECT emoji_id FROM teams WHERE team_id = ?", (home_team_id,))
            home_emoji_id = (await cursor.fetchone())[0]
            cursor = await db.execute("SELECT emoji_id FROM teams WHERE team_id = ?", (away_team_id,))
            away_emoji_id = (await cursor.fetchone())[0]

            # Finals detection - a real round match is a finals match once
            # current_round is past the season's regular_rounds. If so,
            # look up its bracket slot_code (WC1/QF1/.../GF) for the live
            # intro embed's title and to detect the Grand Final specifically
            # (see LiveMatchState.is_finals/finals_slot_code and
            # _post_final_result's finalize_finals_ladder trigger).
            cursor = await db.execute("SELECT regular_rounds FROM seasons WHERE season_id = ?", (season_id,))
            regular_rounds_row = await cursor.fetchone()
            regular_rounds = regular_rounds_row[0] if regular_rounds_row else None
            is_finals = regular_rounds is not None and current_round > regular_rounds
            finals_slot_code = None
            if is_finals:
                cursor = await db.execute(
                    "SELECT slot_code FROM finals_bracket WHERE season_id = ? AND match_id = ?",
                    (season_id, match_id)
                )
                slot_row = await cursor.fetchone()
                finals_slot_code = slot_row[0] if slot_row else None

        # The Grand Final is played at a neutral venue in the real AFL -
        # neither side gets HOME_GROUND_ADVANTAGE, unlike every other round
        # (regular season or earlier finals weeks) where the home team
        # always does. finals_slot_code is already resolved above, before
        # this point, specifically so it's available here.
        home_ground_advantage = finals_slot_code != "GF"

        result, events, quarter_lengths = simulate_match_with_events(
            home_name, home_lineup, away_name, away_lineup, league_avg_ovr, variance=variance,
            home_ground_advantage=home_ground_advantage,
        )

        home_emoji = get_team_emoji_str(self.bot, home_emoji_id)
        away_emoji = get_team_emoji_str(self.bot, away_emoji_id)

        if is_finals and finals_slot_code:
            title_prefix = FINALS_SLOT_LABELS.get(finals_slot_code, finals_slot_code)
        else:
            title_prefix = f"Round {current_round}"
        await feed_channel.send(embed=discord.Embed(
            title=f"{title_prefix}: {home_emoji}{home_name} vs {away_emoji}{away_name}",
            color=discord.Color.blurple(),
        ))

        state = LiveMatchState(home_name, away_name, home_emoji, away_emoji, result, events, variance, quarter_lengths,
                                home_lineup=home_lineup, away_lineup=away_lineup, league_avg_ovr=league_avg_ovr,
                                home_ground_advantage=home_ground_advantage,
                                match_id=match_id, home_team_id=home_team_id, away_team_id=away_team_id,
                                current_round=current_round, season_id=season_id,
                                is_finals=is_finals, finals_slot_code=finals_slot_code,
                                sim_panel_view=sim_panel_view)
        view = LiveMatchControlView(self, state, feed_channel)
        panel_message = await control_channel.send(embed=view.panel_embed(), view=view)
        view.message = panel_message
        _active_live_match = state

    async def _build_match_simulation_embed(self, db, season_id, season_number, current_round, lineups_locked):
        """The fixture list + status shown by /matchsimulation - a status
        line explaining why simming is or isn't available yet, plus each
        match as "{home emoji} vs {away emoji}" (emojis only, no team
        names), with "(completed)" appended once simulated. No scores here
        - see the results channel for those. For a finals round, each
        match line is prefixed with its own full bracket-slot name (e.g.
        "Qualifying Final 1") instead of being unlabeled - a finals week
        can have 2-4 concurrent matches, so the round-level title alone
        doesn't say which is which."""
        from commands.season_commands import get_round_name, FINALS_SLOT_LABELS

        cursor = await db.execute("SELECT regular_rounds FROM seasons WHERE season_id = ?", (season_id,))
        regular_rounds = (await cursor.fetchone())[0]
        is_finals_round = current_round > regular_rounds

        cursor = await db.execute(
            """SELECT h.emoji_id, a.emoji_id, m.simulated, fb.slot_code
               FROM matches m
               JOIN teams h ON m.home_team_id = h.team_id
               JOIN teams a ON m.away_team_id = a.team_id
               LEFT JOIN finals_bracket fb ON fb.match_id = m.match_id
               WHERE m.season_id = ? AND m.round_number = ?
               ORDER BY m.match_id""",
            (season_id, current_round)
        )
        matches = await cursor.fetchall()

        if matches:
            lines = []
            for home_emoji_id, away_emoji_id, simulated, slot_code in matches:
                home_emoji = get_team_emoji_str(self.bot, home_emoji_id)
                away_emoji = get_team_emoji_str(self.bot, away_emoji_id)
                prefix = ""
                if is_finals_round and slot_code:
                    prefix = f"**{FINALS_SLOT_LABELS.get(slot_code, slot_code)}**: "
                line = f"{prefix}{home_emoji} vs {away_emoji}"
                if simulated:
                    line += " (completed)"
                lines.append(line)
            description = "\n".join(lines)
        elif is_finals_round:
            description = "*This finals round's fixture hasn't been generated yet - it's created automatically once the previous round is fully simulated and advanced past.*"
        else:
            description = "*No fixture set for this round yet - import one via `/importdata`.*"

        if lineups_locked:
            status = "✅ Lineups are locked in - matches can be simulated."
        else:
            status = "⏳ Lineups aren't locked in yet - use **Announce Lineups** below first."
        description = f"{status}\n\n{description}"

        round_display = get_round_name(current_round, regular_rounds)
        return discord.Embed(
            title=f"🎛️ Match Simulation — {round_display} (Season {season_number})",
            description=description,
            color=discord.Color.blurple(),
        )

    @app_commands.command(name="matchsimulation", description="[ADMIN] Announce lineups and simulate the current round's matches")
    async def match_simulation(self, interaction: discord.Interaction):
        if not await self.is_admin(interaction):
            await interaction.response.send_message(
                "❌ You need admin permissions to use this command.",
                ephemeral=True
            )
            return

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT season_id, season_number, current_round, lineups_locked, regular_rounds FROM seasons WHERE status = 'active' LIMIT 1"
            )
            season = await cursor.fetchone()
            if not season:
                await interaction.response.send_message("❌ No active season!", ephemeral=True)
                return
            season_id, season_number, current_round, lineups_locked, regular_rounds = season

            embed = await self._build_match_simulation_embed(db, season_id, season_number, current_round, lineups_locked)

            cursor = await db.execute(
                """SELECT COUNT(*), COALESCE(SUM(simulated), 0) FROM matches
                   WHERE season_id = ? AND round_number = ?""",
                (season_id, current_round)
            )
            total_matches, simulated_matches = await cursor.fetchone()
            round_fully_simulated = total_matches > 0 and simulated_matches == total_matches

        is_finals_round = current_round > regular_rounds
        view = MatchSimulationView(self, season_id, season_number, current_round, lineups_locked, round_fully_simulated, is_finals_round)
        # Deliberately NOT ephemeral, and self.message is fetched as a
        # plain channel Message (NOT interaction.original_response()) -
        # this panel can legitimately stay open a long time (waiting on
        # lineups, Sim Full Round staggering results minutes apart with a
        # real delay between them), but an InteractionMessage's .edit()
        # ALWAYS routes through the original interaction's webhook token
        # regardless of ephemeral status (see discord.py's
        # InteractionMessage.edit -> _interaction.edit_original_response),
        # and Discord invalidates that token after ~15 minutes - any
        # _refresh_panel() call past that point used to fail with a "401
        # Invalid Webhook Token" HTTPException. A real Message fetched via
        # the channel uses the bot's own (non-expiring) token to edit
        # instead. interaction_check already restricts every button to
        # admins, so visibility to non-admins is harmless - they simply
        # can't use it. All BUTTON responses (the ephemeral confirmations/
        # errors each click produces) are unaffected - those are fresh
        # interactions each time, not this original one.
        await interaction.response.send_message(embed=embed, view=view)
        sent_message = await interaction.original_response()
        view.message = await sent_message.channel.fetch_message(sent_message.id)

    @app_commands.command(name="matchcentre", description="View fixtures, results, and player stats for AFFL matches")
    async def match_centre(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            season = await self._resolve_season(db, None)
            if not season:
                await interaction.response.send_message("❌ No active season!", ephemeral=True)
                return
            season_id, season_number, _current_round = season

            cursor = await db.execute("SELECT regular_rounds FROM seasons WHERE season_id = ?", (season_id,))
            regular_rounds = (await cursor.fetchone())[0]

            cursor = await db.execute(
                "SELECT DISTINCT round_number FROM matches WHERE season_id = ? ORDER BY round_number",
                (season_id,)
            )
            available_rounds = [row[0] for row in await cursor.fetchall()]
            if not available_rounds:
                await interaction.response.send_message(
                    "❌ No fixture has been set for this season yet.", ephemeral=True
                )
                return
            # Discord's Select hard-caps at 25 options - a season can have up
            # to 24 regular rounds + 5 finals weeks. Trim to the most recent
            # 25 (earliest regular-season rounds drop off first, as least
            # likely to be browsed) rather than erroring out.
            if len(available_rounds) > 25:
                available_rounds = available_rounds[-25:]

            # Default to the last FULLY COMPLETED round (every match
            # simulated), not the season's current_round - that's the round
            # about to be/still being played, which usually has no results
            # to show yet. Falls back to the earliest available round if
            # nothing has been completed at all (season just started).
            cursor = await db.execute(
                """SELECT round_number FROM matches
                   WHERE season_id = ?
                   GROUP BY round_number
                   HAVING SUM(CASE WHEN simulated = 0 THEN 1 ELSE 0 END) = 0
                   ORDER BY round_number DESC LIMIT 1"""
            , (season_id,))
            last_completed_row = await cursor.fetchone()
            if last_completed_row and last_completed_row[0] in available_rounds:
                default_round = last_completed_row[0]
            else:
                default_round = available_rounds[0]

            view = MatchCentreView(self, season_id, season_number, regular_rounds, available_rounds, default_round)
            await view.refresh(db)

        view.update_components()
        await interaction.response.send_message(embed=view.create_embed(), view=view, ephemeral=True)
        view.message = await interaction.original_response()


class MatchSimulationView(discord.ui.View):
    """Panel posted by /matchsimulation - a snapshot of the current round's
    fixture with buttons to announce lineups and simulate matches. Not a
    persistently-synced panel like the live match control panel; each
    action refreshes THIS message in place, but the admin re-runs
    /matchsimulation for a fresh snapshot after leaving and coming back."""
    def __init__(self, cog, season_id, season_number, current_round, lineups_locked, round_fully_simulated=False, is_finals_round=False):
        super().__init__(timeout=600)
        self.cog = cog
        self.season_id = season_id
        self.season_number = season_number
        self.current_round = current_round
        self.lineups_locked = lineups_locked
        self.round_fully_simulated = round_fully_simulated
        self.is_finals_round = is_finals_round
        self.message = None  # set by /matchsimulation right after posting
        self._refresh_button_states()

    def _refresh_button_states(self):
        self.announce_lineups_button.disabled = self.lineups_locked
        # Sim Full Round has no live-mode variant - it's inherently batch,
        # and finals matches require a decisive result via the live panel's
        # Start Extra Time button (never a batch-simulated draw), so it's
        # unavailable for the whole finals round, not just per-match.
        self.sim_full_round_button.disabled = not self.lineups_locked or self.is_finals_round
        self.sim_single_match_button.disabled = not self.lineups_locked
        self.advance_round_button.disabled = not self.round_fully_simulated

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await is_admin_user(interaction)

    async def _refresh_panel(self):
        """Re-reads season state and edits the ORIGINAL /matchsimulation
        panel message (self.message, not whatever interaction triggered the
        refresh - the Sim Single Match flow's later interactions belong to
        separate follow-up messages, not the panel itself). Used after any
        action so the admin sees the result without re-running the command."""
        if self.message is None:
            return
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT lineups_locked, current_round, regular_rounds FROM seasons WHERE season_id = ?",
                (self.season_id,)
            )
            row = await cursor.fetchone()
            if row:
                self.lineups_locked, self.current_round, regular_rounds = bool(row[0]), row[1], row[2]
                self.is_finals_round = self.current_round > regular_rounds

            cursor = await db.execute(
                """SELECT COUNT(*), COALESCE(SUM(simulated), 0) FROM matches
                   WHERE season_id = ? AND round_number = ?""",
                (self.season_id, self.current_round)
            )
            total_matches, simulated_matches = await cursor.fetchone()
            self.round_fully_simulated = total_matches > 0 and simulated_matches == total_matches

            embed = await self.cog._build_match_simulation_embed(
                db, self.season_id, self.season_number, self.current_round, self.lineups_locked
            )
        self._refresh_button_states()
        await self.message.edit(embed=embed, view=self)

    @discord.ui.button(label="Announce Lineups", style=discord.ButtonStyle.primary, row=0)
    async def announce_lineups_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        from commands.season_commands import _AnnounceLineupsMissingView

        # Disable immediately so a second press while this is still posting
        # every team's lineup (a loop with no pacing, but not instant either)
        # can't fire again and double up the posts. _refresh_panel() at the
        # end re-derives the real disabled state either way (still correctly
        # re-enabled if lineups remain unlocked, e.g. some teams were missing).
        button.disabled = True
        if self.message is not None:
            await self.message.edit(view=self)

        await interaction.response.defer(ephemeral=True)
        season_cog = self.cog.bot.get_cog('SeasonCommands')
        async with aiosqlite.connect(DB_PATH) as db:
            result = await season_cog._try_announce_lineups(db)

        if isinstance(result, str):
            await interaction.followup.send(result, ephemeral=True)
        else:
            blocking_team_ids, blocking_emojis = result
            blocking_view = _AnnounceLineupsMissingView(season_cog, blocking_team_ids, panel_view=self)
            await interaction.followup.send(
                f"❌ The following teams have not yet submitted their lineups: {' '.join(blocking_emojis)}\n\n"
                "Use the button below to force-submit remaining lineups - Auto-lineup will replace injured/suspended players where needed",
                view=blocking_view,
                ephemeral=True
            )

        await self._refresh_panel()

    @discord.ui.button(label="Sim Full Round", style=discord.ButtonStyle.success, row=0)
    async def sim_full_round_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                """SELECT m.match_id, m.simulated, m.home_team_id, m.away_team_id, h.team_name, a.team_name
                   FROM matches m
                   JOIN teams h ON m.home_team_id = h.team_id
                   JOIN teams a ON m.away_team_id = a.team_id
                   WHERE m.season_id = ? AND m.round_number = ? AND m.simulated = 0
                   ORDER BY m.match_id""",
                (self.season_id, self.current_round)
            )
            unsimulated = await cursor.fetchall()

            if not unsimulated:
                await interaction.followup.send(f"✅ Round {self.current_round} is already fully simulated.", ephemeral=True)
                await self._refresh_panel()
                return

            cursor = await db.execute(
                "SELECT setting_value FROM settings WHERE setting_key = 'result_delay_seconds'"
            )
            delay_setting = await cursor.fetchone()
            delay_seconds = int(delay_setting[0]) if delay_setting and delay_setting[0] is not None else 15

        # Disable immediately so a second press mid-round can't kick off a
        # second overlapping background run; _refresh_panel() inside the
        # background task re-derives the real state after each match anyway.
        self.sim_full_round_button.disabled = True
        self.sim_single_match_button.disabled = True
        if self.message is not None:
            await self.message.edit(view=self)

        pacing_desc = "all at once" if delay_seconds == 0 else f"one at a time, ~{delay_seconds}s apart"
        await interaction.followup.send(
            f"▶️ Simulating {len(unsimulated)} match(es) {pacing_desc}...", ephemeral=True
        )

        # Each match is simulated and posted in turn - not simulated all up
        # front - as a background task so the admin's own command response
        # above isn't held up waiting for the whole round to play out.
        asyncio.create_task(self.cog._run_full_round_simulation(
            unsimulated, self.season_id, self.current_round, self
        ))

    @discord.ui.button(label="Sim Single Match", style=discord.ButtonStyle.secondary, row=0)
    async def sim_single_match_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                """SELECT m.match_id, h.team_name, a.team_name
                   FROM matches m
                   JOIN teams h ON m.home_team_id = h.team_id
                   JOIN teams a ON m.away_team_id = a.team_id
                   WHERE m.season_id = ? AND m.round_number = ? AND m.simulated = 0
                   ORDER BY m.match_id""",
                (self.season_id, self.current_round)
            )
            unsimulated = await cursor.fetchall()

        if not unsimulated:
            await interaction.response.send_message(f"✅ Round {self.current_round} is already fully simulated.", ephemeral=True)
            return

        pick_view = MatchPickView(self, unsimulated)
        await interaction.response.send_message(
            "Pick a match to simulate:", view=pick_view, ephemeral=True
        )

    @discord.ui.button(label="Advance to Next Round", style=discord.ButtonStyle.success, row=1)
    async def advance_round_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Disable immediately so a second press before advance_to_next_round
        # finishes (it does a fair amount of work - injury/suspension
        # rolls, round summaries, ladder/draft updates) can't kick off a
        # second overlapping advance; _refresh_panel() at the end re-derives
        # the real disabled state either way, same pattern as
        # announce_lineups_button/sim_full_round_button above.
        button.disabled = True
        if self.message is not None:
            await self.message.edit(view=self)

        await interaction.response.defer(ephemeral=True)
        season_cog = self.cog.bot.get_cog('SeasonCommands')
        async with aiosqlite.connect(DB_PATH) as db:
            response = await season_cog.advance_to_next_round(db)
        await interaction.followup.send(response, ephemeral=True)
        await self._refresh_panel()


class MatchPickView(discord.ui.View):
    """Step 1 of Sim Single Match - a dropdown of the round's unsimulated
    matches. Picking one advances to MatchModeView (Result Only / Live)."""
    def __init__(self, panel_view, unsimulated_matches):
        super().__init__(timeout=300)
        self.panel_view = panel_view
        self.add_item(MatchPickSelect(panel_view, unsimulated_matches))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await is_admin_user(interaction)


class MatchPickSelect(discord.ui.Select):
    def __init__(self, panel_view, unsimulated_matches):
        self.panel_view = panel_view
        options = [
            discord.SelectOption(label=f"{home_name} vs {away_name}", value=str(match_id))
            for match_id, home_name, away_name in unsimulated_matches[:25]
        ]
        super().__init__(placeholder="Select a match...", options=options)

    async def callback(self, interaction: discord.Interaction):
        match_id = int(self.values[0])
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                """SELECT m.match_id, m.simulated, m.home_team_id, m.away_team_id, h.team_name, a.team_name
                   FROM matches m
                   JOIN teams h ON m.home_team_id = h.team_id
                   JOIN teams a ON m.away_team_id = a.team_id
                   WHERE m.match_id = ?""",
                (match_id,)
            )
            match_row = await cursor.fetchone()

        if not match_row or match_row[1]:
            await interaction.response.edit_message(
                content="❌ That match has already been simulated or no longer exists.", view=None
            )
            return

        home_name, away_name = match_row[4], match_row[5]
        mode_view = MatchModeView(self.panel_view, match_row)
        await interaction.response.edit_message(
            content=f"**{home_name}** vs **{away_name}** - choose a mode:", view=mode_view
        )


class MatchModeView(discord.ui.View):
    """Step 2 of Sim Single Match - Result Only or Live, for the match
    already picked in MatchPickView. Result Only is disabled for a finals
    round - finals matches must go through the live panel, where a draw
    forces Start Extra Time rather than being left unresolved."""
    def __init__(self, panel_view, match_row):
        super().__init__(timeout=300)
        self.panel_view = panel_view
        self.match_row = match_row
        if panel_view.is_finals_round:
            self.result_only.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await is_admin_user(interaction)

    @discord.ui.button(label="Result Only", style=discord.ButtonStyle.success)
    async def result_only(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Disable both buttons immediately, before anything else runs, so a
        # double-click can't fire this twice - same pattern as live() below.
        # This IS the interaction's response (edits the ephemeral component
        # message from MatchPickSelect); _sim_round_match_batch only ever
        # uses interaction.followup from here on, since the response is now used.
        self.result_only.disabled = True
        self.live.disabled = True
        await interaction.response.edit_message(view=self)

        cog = self.panel_view.cog
        await cog._sim_round_match_batch(interaction, self.match_row, self.panel_view.current_round, self.panel_view.season_id)
        await self.panel_view._refresh_panel()

    @discord.ui.button(label="Live / Qtr by Qtr", style=discord.ButtonStyle.primary)
    async def live(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Disable both buttons immediately, before anything else runs, so a
        # double-click (or a click while _sim_round_match_live is still
        # working through its own setup) can't fire this twice - mirrors
        # ConfirmActionView's same-turn disable-then-act pattern. This IS
        # the interaction's response (this message is an ephemeral
        # component message from MatchPickSelect's own edit_message, so
        # interaction.message.edit() 404s - NotFound: Unknown Message -
        # it must go through the interaction response/followup instead).
        # _sim_round_match_live is written to only ever use
        # interaction.followup from here on, since the response is now used.
        self.result_only.disabled = True
        self.live.disabled = True
        await interaction.response.edit_message(view=self)

        # Refreshed now (the live match has only just started, not finished)
        # so the panel picks up e.g. Sim Full Round staying available for
        # any other still-unsimulated match. _post_final_result does the
        # refresh that actually matters - the one once this match ends and
        # "Advance to Next Round" should unlock - via state.sim_panel_view,
        # since a live match plays out over real time and this call returns
        # almost immediately after just starting it.
        cog = self.panel_view.cog
        await cog._sim_round_match_live(
            interaction, self.match_row, self.panel_view.current_round, self.panel_view.season_id,
            sim_panel_view=self.panel_view,
        )
        await self.panel_view._refresh_panel()


class MatchCentreView(discord.ui.View):
    """Posted by /matchcentre - browse a season's fixture/results round by
    round, and drill into any completed match's full stats. Unlike
    SearchPlayersView's pure in-memory pagination (a fixed list just gets
    sliced per page), round selection here changes WHAT needs to be shown,
    not just which slice of an already-fetched list - so this re-queries
    the DB on every dropdown change via refresh(), each callback opening
    its own short-lived connection, matching how every other interaction
    handler in this file already does its own connection-per-action."""
    def __init__(self, cog, season_id, season_number, regular_rounds, available_rounds, current_round):
        super().__init__(timeout=1800)
        self.cog = cog
        self.season_id = season_id
        self.season_number = season_number
        self.regular_rounds = regular_rounds
        self.available_rounds = available_rounds  # round_numbers with a fixture so far, already trimmed to <=25
        self.current_round = current_round
        self.message = None
        self.matches = []  # (match_id, home_team_id, home_name, home_emoji_id, away_team_id, away_name, away_emoji_id, simulated, home_score, away_score)

    async def refresh(self, db):
        """Re-populates self.matches for self.current_round - called after
        the round dropdown changes, before rebuilding components/embed."""
        cursor = await db.execute(
            """SELECT m.match_id, h.team_id, h.team_name, h.emoji_id,
                      a.team_id, a.team_name, a.emoji_id, m.simulated, m.home_score, m.away_score
               FROM matches m
               JOIN teams h ON m.home_team_id = h.team_id
               JOIN teams a ON m.away_team_id = a.team_id
               WHERE m.season_id = ? AND m.round_number = ?
               ORDER BY m.match_id""",
            (self.season_id, self.current_round)
        )
        self.matches = await cursor.fetchall()

    def update_components(self):
        from commands.season_commands import get_round_name

        self.clear_items()

        round_options = [
            discord.SelectOption(
                label=get_round_name(r, self.regular_rounds),
                value=str(r),
                default=(r == self.current_round),
            )
            for r in self.available_rounds
        ]
        self.add_item(_RoundSelect(self, round_options))
        if self.matches:
            self.add_item(_MatchCentreSelect(self))

        filter_button = discord.ui.Button(label="Filter by Team", style=discord.ButtonStyle.secondary)
        filter_button.callback = self._open_team_filter
        self.add_item(filter_button)

    def create_embed(self):
        from commands.season_commands import get_round_name

        round_display = get_round_name(self.current_round, self.regular_rounds)
        embed = discord.Embed(
            title=f"Match Centre - {round_display} (Season {self.season_number})",
            color=discord.Color.blurple(),
        )

        if not self.matches:
            body = "No fixture for this round yet."
        else:
            lines = []
            for (match_id, home_id, home_name, home_emoji_id, away_id, away_name, away_emoji_id,
                 simulated, home_score, away_score) in self.matches:
                home_emoji = get_team_emoji_str(self.cog.bot, home_emoji_id)
                away_emoji = get_team_emoji_str(self.cog.bot, away_emoji_id)
                if simulated:
                    lines.append(f"{home_emoji} {home_score} - {away_score} {away_emoji}")
                else:
                    lines.append(f"{home_emoji} vs {away_emoji}")
            body = "\n".join(lines)

        embed.description = body
        embed.set_footer(text="Select match to view its stats")
        return embed

    async def _apply_and_render(self, interaction: discord.Interaction, db):
        await self.refresh(db)
        self.update_components()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    async def _open_team_filter(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT team_id, team_name FROM teams WHERE team_name != 'Draft Pool' ORDER BY team_name"
            )
            all_teams = await cursor.fetchall()
        team_view = _TeamMatchesView(self, all_teams)
        team_view.update_components()
        await interaction.response.edit_message(
            content=None, embed=team_view.create_embed(), view=team_view
        )


class _RoundSelect(discord.ui.Select):
    def __init__(self, parent_view: MatchCentreView, options):
        self.parent_view = parent_view
        super().__init__(placeholder="Select a round...", options=options)

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.current_round = int(self.values[0])
        async with aiosqlite.connect(DB_PATH) as db:
            await self.parent_view._apply_and_render(interaction, db)


class _MatchCentreSelect(discord.ui.Select):
    def __init__(self, parent_view: MatchCentreView):
        self.parent_view = parent_view
        options = []
        for (match_id, home_id, home_name, home_emoji_id, away_id, away_name, away_emoji_id,
             simulated, home_score, away_score) in parent_view.matches:
            label = f"{home_name} vs {away_name}"
            if simulated:
                label += f" ({home_score}-{away_score})"
            options.append(discord.SelectOption(label=label, value=str(match_id)))
        super().__init__(placeholder="Select match", options=options[:25])

    async def callback(self, interaction: discord.Interaction):
        match_id = int(self.values[0])
        match_row = next((m for m in self.parent_view.matches if m[0] == match_id), None)

        if not match_row or not match_row[7]:  # simulated flag
            await interaction.response.edit_message(
                content="This match hasn't been played yet.", embed=None, view=_BackToListView(self.parent_view)
            )
            return

        async with aiosqlite.connect(DB_PATH) as db:
            data = await self.parent_view.cog._fetch_box_score_data(db, match_id)
        match_stats_view = _MatchStatsView(self.parent_view, data)
        await interaction.response.edit_message(
            content=None, embed=match_stats_view.create_embed(), view=match_stats_view
        )


class _TeamMatchesView(discord.ui.View):
    """"Filter by Team" - a team-picker dropdown stays on the message at
    all times (so switching teams never needs a "Back" round-trip), plus,
    once a team is picked, a second dropdown of that team's whole-season
    fixture in round order. "Main menu" returns straight to the
    round-based MatchCentreView (no intermediate "team picker" step to
    step back through - the team dropdown IS this view, not a separate
    one before it)."""
    def __init__(self, round_view: MatchCentreView, all_teams, team_id=None, team_name=None):
        super().__init__(timeout=1800)
        self.round_view = round_view
        self.all_teams = all_teams  # [(team_id, team_name), ...] - fetched once
        self.team_id = team_id
        self.team_name = team_name
        self.season_id = round_view.season_id
        self.season_number = round_view.season_number
        self.regular_rounds = round_view.regular_rounds
        self.matches = []  # (match_id, round_number, home_team_id, home_name, home_emoji_id, away_team_id, away_name, away_emoji_id, simulated, home_score, away_score)

    async def refresh(self, db):
        """No-ops (leaves self.matches empty) until a team has been picked."""
        if self.team_id is None:
            self.matches = []
            return

        cursor = await db.execute(
            """SELECT m.match_id, m.round_number, h.team_id, h.team_name, h.emoji_id,
                      a.team_id, a.team_name, a.emoji_id, m.simulated, m.home_score, m.away_score
               FROM matches m
               JOIN teams h ON m.home_team_id = h.team_id
               JOIN teams a ON m.away_team_id = a.team_id
               WHERE m.season_id = ? AND (m.home_team_id = ? OR m.away_team_id = ?)
               ORDER BY m.round_number""",
            (self.season_id, self.team_id, self.team_id)
        )
        self.matches = await cursor.fetchall()

    def update_components(self):
        self.clear_items()
        self.add_item(_TeamPickSelect(self))
        if self.matches:
            self.add_item(_TeamMatchPickSelect(self))

        main_menu_button = discord.ui.Button(label="Main menu", style=discord.ButtonStyle.secondary)
        main_menu_button.callback = self._main_menu
        self.add_item(main_menu_button)

    def create_embed(self):
        if self.team_id is None:
            return discord.Embed(
                title="Match Centre - Filter by Team",
                description="Select a team to view its fixture.",
                color=discord.Color.blurple(),
            )

        embed = discord.Embed(
            title=f"Match Centre - {self.team_name} (Season {self.season_number})",
            color=discord.Color.blurple(),
        )

        if not self.matches:
            body = "No fixture for this team yet."
        else:
            from commands.season_commands import get_round_name

            lines = []
            for (match_id, round_number, home_id, home_name, home_emoji_id, away_id, away_name, away_emoji_id,
                 simulated, home_score, away_score) in self.matches:
                home_emoji = get_team_emoji_str(self.round_view.cog.bot, home_emoji_id)
                away_emoji = get_team_emoji_str(self.round_view.cog.bot, away_emoji_id)
                round_display = get_round_name(round_number, self.regular_rounds)
                if simulated:
                    lines.append(f"{round_display}: {home_emoji} {home_score} - {away_score} {away_emoji}")
                else:
                    lines.append(f"{round_display}: {home_emoji} vs {away_emoji}")
            body = "\n".join(lines)

        embed.description = body
        embed.set_footer(text="Select match to view its stats")
        return embed

    async def _main_menu(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content=None, embed=self.round_view.create_embed(), view=self.round_view
        )


class _TeamPickSelect(discord.ui.Select):
    def __init__(self, parent_view: _TeamMatchesView):
        self.parent_view = parent_view
        options = [
            discord.SelectOption(label=team_name, value=str(team_id), default=(team_id == parent_view.team_id))
            for team_id, team_name in parent_view.all_teams
        ]
        super().__init__(placeholder="Select a team...", options=options[:25])

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.team_id = int(self.values[0])
        self.parent_view.team_name = next(o.label for o in self.options if o.value == self.values[0])

        async with aiosqlite.connect(DB_PATH) as db:
            await self.parent_view.refresh(db)
        self.parent_view.update_components()
        await interaction.response.edit_message(
            content=None, embed=self.parent_view.create_embed(), view=self.parent_view
        )


class _TeamMatchPickSelect(discord.ui.Select):
    def __init__(self, parent_view: _TeamMatchesView):
        self.parent_view = parent_view
        options = []
        for (match_id, round_number, home_id, home_name, home_emoji_id, away_id, away_name, away_emoji_id,
             simulated, home_score, away_score) in parent_view.matches:
            from commands.season_commands import get_round_name
            label = f"{get_round_name(round_number, parent_view.regular_rounds)}: {home_name} vs {away_name}"
            if simulated:
                label += f" ({home_score}-{away_score})"
            options.append(discord.SelectOption(label=label, value=str(match_id)))
        super().__init__(placeholder="Select match", options=options[:25])

    async def callback(self, interaction: discord.Interaction):
        match_id = int(self.values[0])
        match_row = next((m for m in self.parent_view.matches if m[0] == match_id), None)

        if not match_row or not match_row[8]:  # simulated flag
            await interaction.response.edit_message(
                content="This match hasn't been played yet.", embed=None, view=_BackToListView(self.parent_view)
            )
            return

        async with aiosqlite.connect(DB_PATH) as db:
            data = await self.parent_view.round_view.cog._fetch_box_score_data(db, match_id)
        match_stats_view = _MatchStatsView(self.parent_view, data)
        await interaction.response.edit_message(
            content=None, embed=match_stats_view.create_embed(), view=match_stats_view
        )


class _BackToListView(discord.ui.View):
    def __init__(self, parent_view: MatchCentreView):
        super().__init__(timeout=1800)
        self.parent_view = parent_view

    @discord.ui.button(label="Main menu", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content=None, embed=self.parent_view.create_embed(), view=self.parent_view
        )


class _MatchStatsView(discord.ui.View):
    """A completed match's stats - both teams combined into one list,
    sorted by whichever single stat is currently selected (default Goals),
    paginated BOX_SCORE_PLAYERS_PER_PAGE at a time. `data` is
    MatchCommands._fetch_box_score_data's return value, fetched once and
    held here - switching stats or pages just re-sorts/re-renders in
    memory, no re-query needed. parent_view is whichever fixture view this
    was opened from (MatchCentreView or _TeamMatchesView) - both expose the
    same create_embed()/is-a-View interface "Main menu" needs."""
    def __init__(self, parent_view, data, selected_stat="goals"):
        super().__init__(timeout=1800)
        self.parent_view = parent_view
        self.data = data
        self.selected_stat = selected_stat
        self.page = 0
        self.update_components()

    def create_embed(self):
        return build_box_score_embed(self.data, self.selected_stat, self.page)

    def _total_pages(self):
        count = len(self.data["players"])
        return max(1, -(-count // BOX_SCORE_PLAYERS_PER_PAGE))

    def update_components(self):
        self.clear_items()
        for stat_key, label in STAT_LABELS.items():
            button = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.success if stat_key == self.selected_stat else discord.ButtonStyle.secondary,
            )
            button.callback = self._make_stat_callback(stat_key)
            self.add_item(button)

        total_pages = self._total_pages()
        prev_button = discord.ui.Button(
            label="◀ Previous", style=discord.ButtonStyle.primary, disabled=(self.page == 0)
        )
        prev_button.callback = self._previous_page
        self.add_item(prev_button)

        next_button = discord.ui.Button(
            label="Next ▶", style=discord.ButtonStyle.primary, disabled=(self.page >= total_pages - 1)
        )
        next_button.callback = self._next_page
        self.add_item(next_button)

        back_button = discord.ui.Button(label="Main menu", style=discord.ButtonStyle.secondary)
        back_button.callback = self._back
        self.add_item(back_button)

    def _make_stat_callback(self, stat_key):
        async def callback(interaction: discord.Interaction):
            self.selected_stat = stat_key
            self.page = 0
            self.update_components()
            await interaction.response.edit_message(embed=self.create_embed(), view=self)
        return callback

    async def _previous_page(self, interaction: discord.Interaction):
        self.page -= 1
        self.update_components()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    async def _next_page(self, interaction: discord.Interaction):
        self.page += 1
        self.update_components()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    async def _back(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content=None, embed=self.parent_view.create_embed(), view=self.parent_view
        )


async def setup(bot):
    await bot.add_cog(MatchCommands(bot))
