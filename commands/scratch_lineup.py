"""Scratch team management + lineup editor for /scratchmatch's
custom_teams option.

Lets an admin build two throwaway/reusable 23-player lineups from ANY
player in the database (real rostered players or draft-pool prospects,
filterable by team) and run a one-off exhibition sim between them - purely
for draft scouting, e.g. seeing how a handful of undrafted prospects
compare against each other or against a mix of rostered players.

Flow: /scratchmatch custom_teams:True opens ScratchMatchMainMenuView (pick
Team 1 / Team 2 from saved scratch teams, then Run Match once both are
selected and complete). "Manage Scratch Teams" opens
ScratchTeamManageView, which is where teams are created, have their
lineup edited (ScratchLineupEditorView, one team at a time), or deleted.

Deliberately much lighter than lineup_commands.py's TeamLineupMenu/
LineupView (the real team lineup editor): no lock/confirm state, no
injury/suspension badges, no autofill - just position -> team filter ->
player, repeated until all 23 slots are filled. Backed by scratch_teams/
scratch_team_players (see bot.py's init_db), never the real teams/lineups
tables - a scratch team is not a real team and never touches the ladder,
/awards, /matchcentre, /exportdata, or career games-played.
"""
import discord
import aiosqlite
from config import DB_PATH
from utils import fetch_teams_for_dropdown, build_team_options, DISCORD_SELECT_MAX_OPTIONS, get_team_emoji, get_team_emoji_str
from commands.lineup_commands import (
    AFL_POSITIONS, fits_without_penalty,
    build_position_options, build_player_options, build_lineup_field_text,
    get_key_position_overload,
)
from match_sim import INTERCHANGE_SLOTS, format_score

# Discord SelectOption caps at 100 chars per label - a scratch team name
# needs to survive round-tripping through that plus the "vs " prefix used
# when picking an opponent, so keep it comfortably short at creation time.
SCRATCH_TEAM_NAME_MAX_LENGTH = 60


async def fetch_scratch_teams(db):
    """All saved scratch teams as (scratch_team_id, team_name, emoji_id),
    name order. emoji_id is None until set via Manage Scratch Teams' "Set
    Emoji" button - same stored shape as teams.emoji_id (a custom emoji's
    numeric ID as TEXT, resolved via get_team_emoji_str)."""
    cursor = await db.execute(
        "SELECT scratch_team_id, team_name, emoji_id FROM scratch_teams ORDER BY team_name"
    )
    return await cursor.fetchall()


async def fetch_scratch_lineup(db, scratch_team_id):
    """A scratch team's lineup as {position_name: {name, pos, rating, player_id}}."""
    cursor = await db.execute(
        """SELECT stp.position_name, p.player_id, p.name, p.position, p.overall_rating
           FROM scratch_team_players stp
           JOIN players p ON stp.player_id = p.player_id
           WHERE stp.scratch_team_id = ?""",
        (scratch_team_id,)
    )
    rows = await cursor.fetchall()
    return {
        pos_name: {'player_id': player_id, 'name': name, 'pos': pos, 'rating': rating}
        for pos_name, player_id, name, pos, rating in rows
    }


async def fetch_scratch_lineup_tuples(db, scratch_team_id):
    """A scratch team's lineup as (player_id, name, position, overall_rating,
    slot) tuples - the exact shape match_sim.simulate_match expects."""
    cursor = await db.execute(
        """SELECT p.player_id, p.name, p.position, p.overall_rating, stp.position_name
           FROM scratch_team_players stp
           JOIN players p ON stp.player_id = p.player_id
           WHERE stp.scratch_team_id = ?""",
        (scratch_team_id,)
    )
    return await cursor.fetchall()


def _next_empty_position(lineup, after_position):
    """The next position in AFL_POSITIONS order after `after_position` that
    isn't in `lineup` yet - wraps around once. Returns None if every
    position is filled (23/23), which simply deselects the position
    dropdown rather than special-casing a "lineup complete" state."""
    start = AFL_POSITIONS.index(after_position)
    for offset in range(1, len(AFL_POSITIONS) + 1):
        candidate = AFL_POSITIONS[(start + offset) % len(AFL_POSITIONS)]
        if candidate not in lineup:
            return candidate
    return None


def _format_player_stat_line(stat_line):
    """One player's two-line entry for the full-stats embed:
        **Name** (Pos) - **Slot**
        1.1, 20 disposals, 5 marks, 4 tackles, 2 spoils, 20 hitouts
    The bolded name and bolded slot anchor the first line for scanning;
    goals/behinds are the standard AFL scoreboard "g.b" notation (e.g.
    "3.2", unlabelled - the format itself is the convention) and every
    other stat is spelled out in full, on their own line underneath.
    Hitouts are omitted entirely for a player with none (almost always
    everyone except rucks) rather than cluttering every line with a
    near-universal "0 hitouts"."""
    p = stat_line.player
    stat_parts = [
        f"{stat_line.goals}.{stat_line.behinds}",
        f"{stat_line.disposals} disposals",
        f"{stat_line.marks} marks",
        f"{stat_line.tackles} tackles",
        f"{stat_line.spoils} spoils",
    ]
    if stat_line.hitouts > 0:
        stat_parts.append(f"{stat_line.hitouts} hitouts")
    stats = ", ".join(stat_parts)
    return f"**{p.name}** ({p.position}) - **{p.slot}**\n{stats}"


def _format_team_stats_body(team_result):
    """One team's full player stats as a single newline-joined block,
    ordered by AFL_POSITIONS slot order (defense -> midfield -> forward ->
    ruck -> interchange) - no visible section headers, just the sort
    order, since a 23-player x 2-line list already exceeds a single
    embed field's 1024-char limit and goes in the embed's description
    instead (4096-char budget)."""
    stat_lines = sorted(team_result.stat_lines.values(), key=lambda s: AFL_POSITIONS.index(s.player.slot))
    return "\n".join(_format_player_stat_line(s) for s in stat_lines)


def _build_score_embed(team1_name, team2_name, result):
    """A standalone score embed - the full player stats post is a separate,
    non-ephemeral channel message from /scratchmatch's (ephemeral) main
    menu where the match was actually run, so anyone seeing the stats post
    needs the score restated here rather than assumed known."""
    return discord.Embed(
        title="Final Score",
        description=(
            f"**{team1_name}**: {format_score(result.home.goals, result.home.behinds)}\n"
            f"**{team2_name}**: {format_score(result.away.goals, result.away.behinds)}"
        ),
        color=discord.Color.green() if result.home.score >= result.away.score else discord.Color.orange()
    )


def build_full_stats_embeds(team1_name, team2_name, result):
    """A score embed followed by two per-team embeds with every player's
    full stat line, for PostFullStatsView. Separate per-team embeds
    rather than one combined embed - each team's full 23-player body goes
    in the embed's description (4096-char budget), not a field (1024-char
    cap, too small for a flat 23-player list) - and mirrors
    build_final_result_embed's one-embed/field-per-team pattern. A
    monospace grid was tried first but Discord's default font isn't
    reliably fixed-width across clients, so columns didn't actually line
    up - this instead reads as a short stat sentence per player."""
    embeds = [_build_score_embed(team1_name, team2_name, result)]
    for team_name, team_result in ((team1_name, result.home), (team2_name, result.away)):
        embed = discord.Embed(
            title=team_name,
            description=_format_team_stats_body(team_result),
            color=discord.Color.blurple()
        )
        embeds.append(embed)
    return embeds


class PostFullStatsView(discord.ui.View):
    """Attached to the full-time summary embed - "Post Full Player Stats"
    sends a score embed plus each team's full stats as a new,
    non-ephemeral message in the same channel (the summary itself may be
    ephemeral, e.g. behind /scratchmatch's main menu), then disables
    itself so the stats aren't posted twice."""
    def __init__(self, team1_name, team2_name, result):
        super().__init__(timeout=600)
        self.team1_name = team1_name
        self.team2_name = team2_name
        self.result = result

    @discord.ui.button(label="📊 Post Full Player Stats", style=discord.ButtonStyle.secondary)
    async def post_full_stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        embeds = build_full_stats_embeds(self.team1_name, self.team2_name, self.result)
        await interaction.channel.send(embeds=embeds)
        button.disabled = True
        button.label = "📊 Full Player Stats Posted"
        await interaction.response.edit_message(view=self)


class ScratchMatchMainMenuView(discord.ui.View):
    """/scratchmatch custom_teams:True's main menu - two dropdowns to pick
    Team 1 / Team 2 from saved scratch teams, a button to manage teams
    (create/edit lineup/delete), and Run Match, enabled only once both
    teams are selected, different, and have complete (23/23) lineups (and,
    in Live mode, both have an emoji set - see _run_enabled). on_run
    (interaction, team1_id, team1_name, team2_id, team2_name) is called
    once Run Match is pressed."""
    def __init__(self, cog, on_run, mode="batch", team1_id=None, team2_id=None):
        super().__init__(timeout=600)
        self.cog = cog
        self.on_run = on_run
        self.mode = mode
        self.team1_id = team1_id
        self.team2_id = team2_id
        self.message = None

    async def refresh(self, db):
        self.clear_items()
        teams = await fetch_scratch_teams(db)
        self.teams_by_id = {team_id: name for team_id, name, _ in teams}
        self.emoji_by_id = {team_id: emoji_id for team_id, _, emoji_id in teams}

        self.add_item(_MainMenuTeamSelect(self, teams, side=1, selected=self.team1_id))
        self.add_item(_MainMenuTeamSelect(self, teams, side=2, selected=self.team2_id))

        manage_btn = discord.ui.Button(
            label="⚙ Manage Scratch Teams", style=discord.ButtonStyle.secondary, row=2
        )
        manage_btn.callback = self._manage_callback
        self.add_item(manage_btn)

        run_enabled = await self._run_enabled(db)
        run_btn = discord.ui.Button(
            label="▶ Run Match", style=discord.ButtonStyle.primary,
            disabled=not run_enabled, row=2
        )
        run_btn.callback = self._run_callback
        self.add_item(run_btn)

    async def _run_enabled(self, db):
        if not self.team1_id or not self.team2_id or self.team1_id == self.team2_id:
            return False
        cursor = await db.execute(
            "SELECT COUNT(*) FROM scratch_team_players WHERE scratch_team_id = ?", (self.team1_id,)
        )
        team1_count = (await cursor.fetchone())[0]
        cursor = await db.execute(
            "SELECT COUNT(*) FROM scratch_team_players WHERE scratch_team_id = ?", (self.team2_id,)
        )
        team2_count = (await cursor.fetchone())[0]
        if team1_count != 23 or team2_count != 23:
            return False

        # Live mode's per-event feed lines use each team's emoji as the
        # ONLY identifier of who an event belongs to (see LiveMatchState) -
        # without one, every goal/behind/injury line in the feed is
        # ambiguous about which team it's for. Batch mode's result embed
        # always labels each side by name regardless, so this only gates
        # Live.
        if self.mode == "live":
            if not self.emoji_by_id.get(self.team1_id) or not self.emoji_by_id.get(self.team2_id):
                return False

        return True

    def create_embed(self):
        def team_line(side, team_id):
            if not team_id:
                return f"Team {side}: *none selected*"
            name = self.teams_by_id.get(team_id, "Unknown")
            emoji_str = get_team_emoji_str(self.cog.bot, self.emoji_by_id.get(team_id))
            return f"Team {side}: {emoji_str}**{name}**"

        lines = [team_line(1, self.team1_id), team_line(2, self.team2_id)]
        if self.team1_id and self.team1_id == self.team2_id:
            lines.append("\n❌ Pick two different scratch teams.")

        if self.mode == "live":
            missing_emoji = [
                self.teams_by_id[tid] for tid in (self.team1_id, self.team2_id)
                if tid and not self.emoji_by_id.get(tid)
            ]
            if missing_emoji:
                names = " and ".join(f"**{n}**" for n in missing_emoji)
                lines.append(
                    f"\n:exclamation: Live mode requires every team to have an emoji set. "
                    f"Set one for {names} via Manage Scratch Teams first."
                )

        return discord.Embed(
            title="Custom Match" + (" (Live)" if self.mode == "live" else ""),
            description=(
                "Pick a scratch team for each side, then Run Match once both are ready.\n"
                "Use Manage Scratch Teams to create teams or set their lineups.\n\n"
                + "\n".join(lines)
            ),
            color=discord.Color.blurple()
        )

    async def _manage_callback(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            manage_view = ScratchTeamManageView(self.cog, self)
            await manage_view.refresh(db)
            embed = await manage_view.create_embed(db)
        await interaction.response.edit_message(embed=embed, view=manage_view)

    async def _run_callback(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            team1_name = self.teams_by_id.get(self.team1_id, "Team 1")
            team2_name = self.teams_by_id.get(self.team2_id, "Team 2")
            team1_emoji_id = self.emoji_by_id.get(self.team1_id)
            team2_emoji_id = self.emoji_by_id.get(self.team2_id)
        await self.on_run(
            interaction, self.team1_id, team1_name, self.team2_id, team2_name,
            team1_emoji_id, team2_emoji_id
        )


class _MainMenuTeamSelect(discord.ui.Select):
    def __init__(self, parent_view, teams, side, selected):
        self.parent_view = parent_view
        self.side = side
        options = [
            discord.SelectOption(
                label=name[:100], value=str(team_id), default=(team_id == selected),
                emoji=get_team_emoji(parent_view.cog.bot, emoji_id)
            )
            for team_id, name, emoji_id in teams
        ][:DISCORD_SELECT_MAX_OPTIONS]
        if not options:
            options = [discord.SelectOption(label="No saved scratch teams yet - use Manage Scratch Teams", value="none")]
        super().__init__(placeholder=f"Team {side}...", options=options, row=side - 1)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.defer()
            return
        team_id = int(self.values[0])
        if self.side == 1:
            self.parent_view.team1_id = team_id
        else:
            self.parent_view.team2_id = team_id
        async with aiosqlite.connect(DB_PATH) as db:
            await self.parent_view.refresh(db)
        await interaction.response.edit_message(embed=self.parent_view.create_embed(), view=self.parent_view)


class ScratchTeamManageView(discord.ui.View):
    """Create, edit the lineup of, or delete a saved scratch team.
    main_menu is the ScratchMatchMainMenuView to return to (and whose team
    dropdowns get refreshed - a rename/delete here can affect what it
    shows)."""
    def __init__(self, cog, main_menu, selected_team_id=None):
        super().__init__(timeout=600)
        self.cog = cog
        self.main_menu = main_menu
        self.selected_team_id = selected_team_id
        self.message = None

    async def refresh(self, db):
        self.clear_items()
        teams = await fetch_scratch_teams(db)
        self.teams_by_id = {team_id: name for team_id, name, _ in teams}
        self.emoji_by_id = {team_id: emoji_id for team_id, _, emoji_id in teams}

        self.add_item(_ManageTeamSelect(self, teams))

        new_btn = discord.ui.Button(label="+ New Scratch Team", style=discord.ButtonStyle.success, row=1)
        new_btn.callback = self._new_team_callback
        self.add_item(new_btn)

        if self.selected_team_id and self.selected_team_id in self.teams_by_id:
            edit_btn = discord.ui.Button(label="📝 Edit Lineup", style=discord.ButtonStyle.primary, row=2)
            edit_btn.callback = self._edit_lineup_callback
            self.add_item(edit_btn)

            rename_btn = discord.ui.Button(label="✏ Rename Team", style=discord.ButtonStyle.secondary, row=2)
            rename_btn.callback = self._rename_callback
            self.add_item(rename_btn)

            emoji_btn = discord.ui.Button(label="😀 Set Emoji", style=discord.ButtonStyle.secondary, row=2)
            emoji_btn.callback = self._set_emoji_callback
            self.add_item(emoji_btn)

            delete_btn = discord.ui.Button(label="🗑 Delete Team", style=discord.ButtonStyle.danger, row=2)
            delete_btn.callback = self._delete_callback
            self.add_item(delete_btn)

        back_btn = discord.ui.Button(label="← Back to Main Menu", style=discord.ButtonStyle.secondary, row=3)
        back_btn.callback = self._back_callback
        self.add_item(back_btn)

    async def create_embed(self, db):
        if not self.selected_team_id or self.selected_team_id not in self.teams_by_id:
            return discord.Embed(
                title="Manage Scratch Teams",
                description="Pick a scratch team to edit, rename, or delete, or create a new one.",
                color=discord.Color.blurple()
            )
        lineup = await fetch_scratch_lineup(db, self.selected_team_id)
        name = self.teams_by_id[self.selected_team_id]
        emoji_str = get_team_emoji_str(self.cog.bot, self.emoji_by_id.get(self.selected_team_id))

        embed = discord.Embed(
            title="Manage Scratch Teams",
            description=f"Selected: {emoji_str}**{name}** ({len(lineup)}/23 positions filled)",
            color=discord.Color.blurple()
        )
        # Same field-grid layout as the lineup editor itself
        # (build_lineup_field_text) - shows the selected team's lineup
        # right here so managing a team doesn't require opening the editor
        # just to see who's in it.
        embed.add_field(name="​", value=build_lineup_field_text(lineup), inline=False)
        if not emoji_str:
            embed.add_field(
                name="​",
                value="⚠ No emoji set - required before this team can be used in a Live scratch match.",
                inline=False
            )
        return embed

    async def _new_team_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(_NewScratchTeamModal(self))

    async def _rename_callback(self, interaction: discord.Interaction):
        current_name = self.teams_by_id[self.selected_team_id]
        await interaction.response.send_modal(_RenameScratchTeamModal(self, self.selected_team_id, current_name))

    async def _set_emoji_callback(self, interaction: discord.Interaction):
        current_emoji_id = self.emoji_by_id.get(self.selected_team_id)
        current_emoji_str = get_team_emoji_str(self.cog.bot, current_emoji_id, trailing_space=False)
        await interaction.response.send_modal(
            _SetScratchTeamEmojiModal(self, self.selected_team_id, current_emoji_str)
        )

    async def _edit_lineup_callback(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            lineup = await fetch_scratch_lineup(db, self.selected_team_id)
            team_name = self.teams_by_id[self.selected_team_id]
            editor = ScratchLineupEditorView(self.cog, self.selected_team_id, team_name, lineup, manage_view=self)
            await editor.refresh_components(db)
        await interaction.response.edit_message(embed=editor.create_embed(), view=editor)

    async def _delete_callback(self, interaction: discord.Interaction):
        name = self.teams_by_id[self.selected_team_id]
        confirm_view = _ConfirmDeleteView(self, self.selected_team_id, name)
        await interaction.response.send_message(
            f"❌ Delete scratch team **{name}** and its lineup? This can't be undone.",
            view=confirm_view, ephemeral=True
        )

    async def _back_callback(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            await self.main_menu.refresh(db)
        await interaction.response.edit_message(embed=self.main_menu.create_embed(), view=self.main_menu)


class _ManageTeamSelect(discord.ui.Select):
    def __init__(self, parent_view, teams):
        self.parent_view = parent_view
        options = [
            discord.SelectOption(
                label=name[:100], value=str(team_id),
                default=(team_id == parent_view.selected_team_id),
                emoji=get_team_emoji(parent_view.cog.bot, emoji_id)
            )
            for team_id, name, emoji_id in teams
        ][:DISCORD_SELECT_MAX_OPTIONS]
        if not options:
            options = [discord.SelectOption(label="No saved scratch teams yet", value="none")]
        super().__init__(placeholder="Select a scratch team...", options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.defer()
            return
        self.parent_view.selected_team_id = int(self.values[0])
        async with aiosqlite.connect(DB_PATH) as db:
            await self.parent_view.refresh(db)
            embed = await self.parent_view.create_embed(db)
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class _NewScratchTeamModal(discord.ui.Modal, title="New Scratch Team"):
    team_name = discord.ui.TextInput(
        label="Team name", max_length=SCRATCH_TEAM_NAME_MAX_LENGTH,
        placeholder="e.g. Draft Prospects A"
    )

    def __init__(self, manage_view):
        super().__init__()
        self.manage_view = manage_view

    async def on_submit(self, interaction: discord.Interaction):
        name = self.team_name.value.strip()
        if not name:
            await interaction.response.send_message("❌ Name can't be empty.", ephemeral=True)
            return

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT scratch_team_id FROM scratch_teams WHERE team_name = ?", (name,))
            if await cursor.fetchone():
                await interaction.response.send_message(
                    f"❌ A scratch team named '{name}' already exists - pick it from the list instead.",
                    ephemeral=True
                )
                return
            cursor = await db.execute("INSERT INTO scratch_teams (team_name) VALUES (?)", (name,))
            await db.commit()
            scratch_team_id = cursor.lastrowid

            self.manage_view.selected_team_id = scratch_team_id
            await self.manage_view.refresh(db)
            embed = await self.manage_view.create_embed(db)
        await interaction.response.edit_message(embed=embed, view=self.manage_view)


class _RenameScratchTeamModal(discord.ui.Modal, title="Rename Scratch Team"):
    team_name = discord.ui.TextInput(
        label="New team name", max_length=SCRATCH_TEAM_NAME_MAX_LENGTH,
        placeholder="e.g. Draft Prospects A"
    )

    def __init__(self, manage_view, scratch_team_id, current_name):
        super().__init__()
        self.manage_view = manage_view
        self.scratch_team_id = scratch_team_id
        self.team_name.default = current_name

    async def on_submit(self, interaction: discord.Interaction):
        name = self.team_name.value.strip()
        if not name:
            await interaction.response.send_message("❌ Name can't be empty.", ephemeral=True)
            return

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT scratch_team_id FROM scratch_teams WHERE team_name = ? AND scratch_team_id != ?",
                (name, self.scratch_team_id)
            )
            if await cursor.fetchone():
                await interaction.response.send_message(
                    f"❌ A scratch team named '{name}' already exists - pick a different name.",
                    ephemeral=True
                )
                return
            await db.execute(
                "UPDATE scratch_teams SET team_name = ? WHERE scratch_team_id = ?",
                (name, self.scratch_team_id)
            )
            await db.commit()

            await self.manage_view.refresh(db)
            embed = await self.manage_view.create_embed(db)
        await interaction.response.edit_message(embed=embed, view=self.manage_view)


class _SetScratchTeamEmojiModal(discord.ui.Modal, title="Set Scratch Team Emoji"):
    """Same input convention as /updateteam's emoji parameter - paste a
    custom emoji (Discord renders it as <:name:id> text) or type the raw
    numeric ID directly. Only a custom emoji (one the bot can resolve via
    bot.get_emoji, i.e. from a server the bot is in) works here, same
    restriction as a real team's emoji - a standard unicode emoji has no
    ID to store and won't resolve via get_team_emoji_str."""
    emoji_input = discord.ui.TextInput(
        label="Emoji", max_length=100,
        placeholder="Paste a custom emoji, e.g. <:hawks:123456789012345678>"
    )

    def __init__(self, manage_view, scratch_team_id, current_emoji_str):
        super().__init__()
        self.manage_view = manage_view
        self.scratch_team_id = scratch_team_id
        if current_emoji_str:
            self.emoji_input.default = current_emoji_str

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.emoji_input.value.strip()
        if not raw:
            await interaction.response.send_message("❌ Emoji can't be empty.", ephemeral=True)
            return

        import re
        match = re.match(r'<a?:(\w+):(\d+)>', raw)
        emoji_id = match.group(2) if match else raw

        if not emoji_id.isdigit():
            await interaction.response.send_message(
                "❌ That doesn't look like a custom emoji - paste the emoji itself "
                "(Discord will show it as `<:name:id>`), or its numeric ID.",
                ephemeral=True
            )
            return

        resolved = get_team_emoji(self.manage_view.cog.bot, emoji_id)
        if resolved is None:
            await interaction.response.send_message(
                "❌ Couldn't find that emoji - it must be a custom emoji from a server this bot is in.",
                ephemeral=True
            )
            return

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE scratch_teams SET emoji_id = ? WHERE scratch_team_id = ?",
                (emoji_id, self.scratch_team_id)
            )
            await db.commit()

            await self.manage_view.refresh(db)
            embed = await self.manage_view.create_embed(db)
        await interaction.response.edit_message(embed=embed, view=self.manage_view)


class _ConfirmDeleteView(discord.ui.View):
    def __init__(self, manage_view, scratch_team_id, team_name):
        super().__init__(timeout=60)
        self.manage_view = manage_view
        self.scratch_team_id = scratch_team_id
        self.team_name = team_name

    @discord.ui.button(label="✅ Confirm Delete", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM scratch_team_players WHERE scratch_team_id = ?", (self.scratch_team_id,))
            await db.execute("DELETE FROM scratch_teams WHERE scratch_team_id = ?", (self.scratch_team_id,))
            await db.commit()

            if self.manage_view.selected_team_id == self.scratch_team_id:
                self.manage_view.selected_team_id = None
            if self.manage_view.main_menu.team1_id == self.scratch_team_id:
                self.manage_view.main_menu.team1_id = None
            if self.manage_view.main_menu.team2_id == self.scratch_team_id:
                self.manage_view.main_menu.team2_id = None

            await self.manage_view.refresh(db)
            manage_embed = await self.manage_view.create_embed(db)

        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content=f"✅ Deleted **{self.team_name}**.", view=self)
        if self.manage_view.message:
            await self.manage_view.message.edit(embed=manage_embed, view=self.manage_view)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="❌ Cancelled.", view=self)


class ScratchLineupEditorView(discord.ui.View):
    """Edits ONE scratch team's 23-slot lineup - opened from
    ScratchTeamManageView's Edit Lineup button. "← Back" returns to that
    manage view. Picking a player for a position automatically advances
    the position dropdown to the next empty slot (AFL_POSITIONS order),
    so filling a lineup top-to-bottom needs no extra clicks per slot."""
    def __init__(self, cog, scratch_team_id, team_name, lineup, manage_view):
        super().__init__(timeout=600)
        self.cog = cog
        self.scratch_team_id = scratch_team_id
        self.team_name = team_name
        self.lineup = lineup  # {position_name: {player_id, name, pos, rating}}
        self.manage_view = manage_view
        self.selected_position = None
        self.team_filter_id = None
        self.filtered_players = []
        self.player_page = 0
        self.message = None

    async def refresh_components(self, db):
        self.clear_items()
        self.add_item(_ScratchPositionSelect(self))

        teams = await fetch_teams_for_dropdown(db, include_draft_pool=True)
        self.add_item(_ScratchTeamFilterSelect(self, teams))

        # Rows 0-2 are always the three Selects above (position, team
        # filter, and - once both are chosen - player), each consuming a
        # full width-5 row regardless of option count. Only rows 3-4 are
        # free for buttons - unlike the real lineup editor's LineupView,
        # which has just two Selects (position, player) and so can use row
        # 2 for its Prev/Next buttons.
        if self.selected_position and self.team_filter_id is not None:
            await self._load_filtered_players(db)
            self.add_item(_ScratchPlayerSelect(self))
            total = len(self.filtered_players)
            if total > 25:
                total_pages = (total + 24) // 25
                if self.player_page > 0:
                    self.add_item(_ScratchPrevPageButton(self, total_pages))
                if (self.player_page + 1) * 25 < total:
                    self.add_item(_ScratchNextPageButton(self, total_pages))

        if self.selected_position and self.selected_position in self.lineup:
            self.add_item(_ScratchClearPositionButton(self))

        back_btn = discord.ui.Button(label="← Back to Manage Scratch Teams", style=discord.ButtonStyle.secondary, row=4)
        back_btn.callback = self._back_callback
        self.add_item(back_btn)

    async def _back_callback(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            await self.manage_view.refresh(db)
            embed = await self.manage_view.create_embed(db)
        await interaction.response.edit_message(embed=embed, view=self.manage_view)

    async def _load_filtered_players(self, db):
        # team_filter_id is always a real teams.team_id here (including the
        # real "Draft Pool" team row - see fetch_teams_for_dropdown's
        # include_draft_pool=True below). Draft Pool is a genuine roster,
        # not a stand-in for delisted players: those have team_id IS NULL
        # and are deliberately NOT reachable from this picker, matching the
        # user's explicit "draft pool should only be players on the draft
        # pool team" correction.
        cursor = await db.execute(
            """SELECT player_id, name, position, overall_rating, age
               FROM players WHERE team_id = ? ORDER BY overall_rating DESC""",
            (self.team_filter_id,)
        )
        self.filtered_players = await cursor.fetchall()

    def get_sorted_players(self):
        """Filtered players sorted by fit for the selected slot, same
        priority rule as the real lineup editor (fits_without_penalty
        first, then rating descending)."""
        if not self.selected_position or self.selected_position in INTERCHANGE_SLOTS:
            return self.filtered_players

        def sort_key(player):
            pos = player[2]
            rating = player[3]
            fits = fits_without_penalty(pos, self.selected_position)
            return (0 if fits else 1, -rating)

        return sorted(self.filtered_players, key=sort_key)

    def create_embed(self):
        """Same field-grid layout as the real team lineup editor
        (LineupView.create_embed) via the shared build_lineup_field_text."""
        embed = discord.Embed(
            title=f"{self.team_name} - Lineup Editor",
            description="Select a position from the dropdown, then choose a player to fill it.",
            color=discord.Color.green()
        )

        field_text = build_lineup_field_text(self.lineup, self.selected_position)
        embed.add_field(name="​", value=field_text, inline=False)

        # Same key-position-line-overload warning as the real lineup editor
        # (get_key_position_overload) - mirrors match_sim.py's own in-sim
        # penalty, so a scouting scratch match's lineup gets the same
        # heads-up about a crowded line as a real team's does.
        warnings = [
            f"❗ **Too many key position players in {group_label}:** {count} ({', '.join(names)})"
            for group_label, count, names in get_key_position_overload(self.lineup)
        ]
        if warnings:
            embed.add_field(name="​", value="\n".join(warnings), inline=False)

        embed.set_footer(text=f"{len(self.lineup)}/23 positions filled")
        return embed


class _ScratchPositionSelect(discord.ui.Select):
    def __init__(self, parent_view):
        # No roster-wide age/injury/suspension data for a scratch team, so
        # build_position_options's optional badging args are left at their
        # defaults (empty) - same helper PositionSelect uses for real teams.
        options = build_position_options(parent_view.lineup, parent_view.selected_position)
        super().__init__(placeholder="Select a position to edit...", options=options)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.selected_position = self.values[0]
        self.parent_view.player_page = 0
        async with aiosqlite.connect(DB_PATH) as db:
            await self.parent_view.refresh_components(db)
        await interaction.response.edit_message(
            embed=self.parent_view.create_embed(), view=self.parent_view
        )


class _ScratchTeamFilterSelect(discord.ui.Select):
    def __init__(self, parent_view, teams):
        # include_draft_pool=True: Draft Pool is a real teams row (undrafted
        # players actually sit on it, players.team_id = that row's
        # team_id) - it belongs in this list like any other team, not as a
        # separate synthetic option. Delisted players (players.team_id IS
        # NULL) are a different, unrelated group and are NOT reachable from
        # this filter at all, per the user's explicit correction.
        options = build_team_options(
            parent_view.cog.bot, teams,
            selected=parent_view.team_filter_id,
            include_draft_pool=True,
        )
        super().__init__(placeholder="Filter players by team...", options=options)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.team_filter_id = int(self.values[0])
        self.parent_view.player_page = 0
        async with aiosqlite.connect(DB_PATH) as db:
            await self.parent_view.refresh_components(db)
        await interaction.response.edit_message(
            embed=self.parent_view.create_embed(), view=self.parent_view
        )


class _ScratchPlayerSelect(discord.ui.Select):
    def __init__(self, parent_view):
        self.parent_view = parent_view
        sorted_players = parent_view.get_sorted_players()

        # Same option-building rules as the real lineup editor's
        # PlayerSelect (name/currently-in-slot label, pos/age/rating
        # description) - no injured_ids/suspended_ids to pass since scratch
        # teams don't track that.
        options = build_player_options(sorted_players, parent_view.lineup, parent_view.player_page)

        total_pages = max(1, (len(sorted_players) + 24) // 25)
        placeholder = f"Select player for {parent_view.selected_position} (Page {parent_view.player_page + 1}/{total_pages})"
        super().__init__(placeholder=placeholder[:150], options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.defer()
            return
        player_id = int(self.values[0])
        position_name = self.parent_view.selected_position
        slot_number = AFL_POSITIONS.index(position_name) + 1

        async with aiosqlite.connect(DB_PATH) as db:
            # A player can only occupy one slot on this scratch team -
            # drop any existing slot of theirs before assigning the new one.
            await db.execute(
                "DELETE FROM scratch_team_players WHERE scratch_team_id = ? AND player_id = ?",
                (self.parent_view.scratch_team_id, player_id)
            )
            await db.execute(
                """INSERT OR REPLACE INTO scratch_team_players
                   (scratch_team_id, player_id, slot_number, position_name)
                   VALUES (?, ?, ?, ?)""",
                (self.parent_view.scratch_team_id, player_id, slot_number, position_name)
            )
            await db.commit()

            cursor = await db.execute(
                "SELECT name, position, overall_rating FROM players WHERE player_id = ?", (player_id,)
            )
            name, pos, rating = await cursor.fetchone()

            for pos_name, info in list(self.parent_view.lineup.items()):
                if info.get('player_id') == player_id and pos_name != position_name:
                    del self.parent_view.lineup[pos_name]

            self.parent_view.lineup[position_name] = {
                'player_id': player_id, 'name': name, 'pos': pos, 'rating': rating
            }

            # Auto-advance to the next empty slot (AFL_POSITIONS order) so
            # filling a lineup top-to-bottom needs one click per player,
            # not two - deselects (None) once every slot is filled.
            self.parent_view.selected_position = _next_empty_position(self.parent_view.lineup, position_name)
            self.parent_view.player_page = 0
            await self.parent_view.refresh_components(db)

        await interaction.response.edit_message(
            embed=self.parent_view.create_embed(), view=self.parent_view
        )


class _ScratchClearPositionButton(discord.ui.Button):
    def __init__(self, parent_view):
        super().__init__(label="✗ Clear Slot", style=discord.ButtonStyle.danger, row=3)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        position_name = self.parent_view.selected_position
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM scratch_team_players WHERE scratch_team_id = ? AND position_name = ?",
                (self.parent_view.scratch_team_id, position_name)
            )
            await db.commit()
            if position_name in self.parent_view.lineup:
                del self.parent_view.lineup[position_name]
            await self.parent_view.refresh_components(db)
        await interaction.response.edit_message(
            embed=self.parent_view.create_embed(), view=self.parent_view
        )


class _ScratchPrevPageButton(discord.ui.Button):
    def __init__(self, parent_view, total_pages):
        # Row 3, not 2 - rows 0-2 are all Selects here (position, team
        # filter, player), unlike the real lineup editor's two-Select
        # layout where row 2 is free for these buttons.
        super().__init__(label=f"◀ Page {parent_view.player_page}/{total_pages}",
                          style=discord.ButtonStyle.secondary, row=3)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.player_page -= 1
        async with aiosqlite.connect(DB_PATH) as db:
            await self.parent_view.refresh_components(db)
        await interaction.response.edit_message(
            embed=self.parent_view.create_embed(), view=self.parent_view
        )


class _ScratchNextPageButton(discord.ui.Button):
    def __init__(self, parent_view, total_pages):
        super().__init__(label=f"Page {parent_view.player_page + 2}/{total_pages} ▶",
                          style=discord.ButtonStyle.secondary, row=3)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.player_page += 1
        async with aiosqlite.connect(DB_PATH) as db:
            await self.parent_view.refresh_components(db)
        await interaction.response.edit_message(
            embed=self.parent_view.create_embed(), view=self.parent_view
        )
