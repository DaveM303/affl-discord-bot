import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
import json
from config import DB_PATH
from utils import (is_admin_user, assign_drafted_player, get_team_emoji, pick_origin_label,
                   get_team_emoji_str, build_team_options, fetch_teams_for_dropdown, add_page_buttons)


def replace_page_buttons(view, page, total_pages, on_page, row):
    """For views that keep their other components and only redraw the page
    controls (decorated selects/buttons can't be rebuilt with clear_items):
    removes the previous page buttons, then adds fresh standard ones."""
    for item in [item for item in view.children if getattr(item, "_is_page_button", False)]:
        view.remove_item(item)
    before = set(view.children)
    add_page_buttons(view, page, total_pages, on_page, row=row)
    for item in view.children:
        if item not in before:
            item._is_page_button = True

class DraftCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        """Re-attaches the buttons of every live draft's control panel
        after a restart (they're persistent - see DraftControlView)."""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    """SELECT draft_id, status, control_message_id FROM drafts
                       WHERE control_message_id IS NOT NULL
                         AND status IN ('current', 'in_progress', 'completed')"""
                )
                for draft_id, status, message_id in await cursor.fetchall():
                    self.bot.add_view(DraftControlView(self.bot, draft_id, status), message_id=int(message_id))
        except Exception as e:
            print(f"Couldn't re-register draft control panels: {e}")

    async def draft_name_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for draft names - shows current and in-progress drafts"""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    """SELECT draft_name FROM drafts
                       WHERE status IN ('current', 'in_progress')
                       ORDER BY draft_id DESC"""
                )
                drafts = await cursor.fetchall()

            choices = []
            for (draft_name,) in drafts:
                if current.lower() in draft_name.lower():
                    choices.append(app_commands.Choice(name=draft_name, value=draft_name))

            return choices[:25]
        except Exception:
            return []

    async def draft_pool_position_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Positions that actually appear in the draft pool right now, with
        a count each - suggesting a position the pool has nobody for would
        only ever return an empty list."""
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                """SELECT p.position, COUNT(*)
                   FROM players p
                   JOIN teams t ON p.team_id = t.team_id
                   WHERE t.team_name = 'Draft Pool'
                   GROUP BY p.position
                   ORDER BY p.position"""
            )
            rows = await cursor.fetchall()

        return [
            app_commands.Choice(name=f"{position} ({count})", value=position)
            for position, count in rows
            if current.lower() in position.lower()
        ][:25]

    async def team_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for team names"""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "SELECT team_name FROM teams ORDER BY team_name"
                )
                teams = await cursor.fetchall()

            choices = []
            for (team_name,) in teams:
                if current.lower() in team_name.lower():
                    choices.append(app_commands.Choice(name=team_name, value=team_name))

            return choices[:25]
        except Exception:
            return []

    @app_commands.command(name="createcustomdraft", description="[ADMIN] Create a custom draft, ordered by the active season's current ladder")
    @app_commands.describe(
        draft_name="Custom draft name",
        rounds="Number of rounds (default: 4)",
        rookie_contract_years="Rookie contract length in years (default: 3)"
    )
    async def create_custom_draft(self, interaction: discord.Interaction, draft_name: str, rounds: int = 4, rookie_contract_years: int = 3):
        await interaction.response.defer(ephemeral=True)

        # Check if user has admin role
        if not await is_admin_user(interaction):
            await interaction.followup.send("❌ You don't have permission to use this command.", ephemeral=True)
            return

        if rounds < 1 or rounds > 10:
            await interaction.followup.send("❌ Number of rounds must be between 1 and 10!", ephemeral=True)
            return

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Check if draft already exists
                cursor = await db.execute("SELECT draft_id FROM drafts WHERE draft_name = ?", (draft_name,))
                if await cursor.fetchone():
                    await interaction.followup.send(
                        f"❌ A draft named '{draft_name}' already exists!",
                        ephemeral=True
                    )
                    return

                # Custom drafts are never season-linked - always season_number = 0,
                # same convention the old manual-draft path used. Ordered
                # immediately from the ACTIVE season's current ladder
                # (ladder_positions) rather than an admin pasting one in -
                # a one-time snapshot at creation, not kept in sync
                # afterward (unlike a season-linked draft's own indicative
                # order - see update_indicative_draft_order in
                # season_commands.py, which only ever touches a draft with
                # a real season_number).
                cursor = await db.execute(
                    "SELECT season_id FROM seasons WHERE status = 'active' LIMIT 1"
                )
                active_season = await cursor.fetchone()
                if not active_season:
                    await interaction.followup.send(
                        "❌ No active season! A custom draft's order is based on the active season's current ladder.",
                        ephemeral=True
                    )
                    return
                season_id = active_season[0]

                cursor = await db.execute(
                    "SELECT team_id, team_name FROM teams WHERE team_name != 'Draft Pool' ORDER BY team_name"
                )
                teams = await cursor.fetchall()
                if not teams:
                    await interaction.followup.send("❌ No teams found!", ephemeral=True)
                    return
                team_name_by_id = dict(teams)

                cursor = await db.execute(
                    "SELECT team_id FROM ladder_positions WHERE season_id = ? ORDER BY position", (season_id,)
                )
                ranked_team_ids = [row[0] for row in await cursor.fetchall()]
                if not ranked_team_ids:
                    await interaction.followup.send(
                        "❌ The active season has no ladder yet - simulate at least one round first.",
                        ephemeral=True
                    )
                    return

                # Worst team first (last place picks first), matching every
                # other draft-order convention in this codebase.
                draft_order = list(reversed(ranked_team_ids))

                cursor = await db.execute(
                    """INSERT INTO drafts (draft_name, season_number, status, rounds, rookie_contract_years, ladder_set_at)
                       VALUES (?, 0, 'current', ?, ?, CURRENT_TIMESTAMP)""",
                    (draft_name, rounds, rookie_contract_years)
                )
                draft_id = cursor.lastrowid

                pick_counter = 1
                for round_num in range(1, rounds + 1):
                    for team_id in draft_order:
                        await db.execute(
                            """INSERT INTO draft_picks (draft_id, draft_name, season_number, round_number, pick_number,
                                                        original_team_id, current_team_id, pick_type)
                               VALUES (?, ?, 0, ?, ?, ?, ?, 'ladder')""",
                            (draft_id, draft_name, round_num, pick_counter, team_id, team_id)
                        )
                        pick_counter += 1

                await db.commit()

                first_place_team = team_name_by_id[ranked_team_ids[0]]
                last_place_team = team_name_by_id[ranked_team_ids[-1]]

                message = f"✅ **Custom Draft Created: {draft_name}**\n\n"
                message += f"**Rounds:** {rounds}\n"
                message += f"**Rookie Contract:** {rookie_contract_years} years\n"
                message += f"**Order set from:** the active season's current ladder\n"
                message += f"  1st: {first_place_team}\n  ...\n  {len(ranked_team_ids)}th: {last_place_team}\n\n"
                message += f"**First pick:** {last_place_team} (last place)\n"
                message += f"\nUse `/draftorder \"{draft_name}\"` to view the full draft order."

                await interaction.followup.send(message, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @app_commands.command(name="draftorder", description="View the draft order")
    @app_commands.describe(draft_name="Optional: Name of the draft to view (defaults to latest)")
    @app_commands.autocomplete(draft_name=draft_name_autocomplete)
    async def draft_order(self, interaction: discord.Interaction, draft_name: str = None):
        await interaction.response.defer(ephemeral=True)

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # If no draft name provided, get the most recent current or in-progress draft
                if draft_name is None:
                    cursor = await db.execute(
                        """SELECT draft_name
                           FROM drafts
                           WHERE status IN ('current', 'in_progress')
                           ORDER BY draft_id DESC
                           LIMIT 1"""
                    )
                    draft_result = await cursor.fetchone()
                    if not draft_result:
                        await interaction.followup.send(
                            "❌ No current or in-progress drafts found!\n"
                            "A season-linked draft becomes viewable automatically once its season starts."
                        )
                        return
                    draft_name = draft_result[0]

                # Verify this draft is current or in progress (has ladder set)
                cursor = await db.execute(
                    "SELECT status FROM drafts WHERE draft_name = ?",
                    (draft_name,)
                )
                draft_status = await cursor.fetchone()
                if not draft_status or draft_status[0] not in ('current', 'in_progress'):
                    await interaction.followup.send(
                        f"❌ Draft '{draft_name}' is not viewable (status: {draft_status[0] if draft_status else 'unknown'})!\n"
                        f"Only current or in-progress drafts with ladder order set can be viewed."
                    )
                    return

                # Get draft picks with team emojis
                cursor = await db.execute(
                    """SELECT dp.pick_number, dp.round_number,
                              dp.pick_origin,
                              ct.team_name as current_team, ct.emoji_id as current_emoji,
                              p.name as player_selected,
                              dp.pick_type, ot.team_name, ot.emoji_id
                       FROM draft_picks dp
                       JOIN teams ct ON dp.current_team_id = ct.team_id
                       LEFT JOIN teams ot ON dp.original_team_id = ot.team_id
                       LEFT JOIN players p ON dp.player_selected_id = p.player_id
                       WHERE dp.draft_name = ? AND dp.pick_number IS NOT NULL
                       ORDER BY dp.pick_number""",
                    (draft_name,)
                )
                # The origin column becomes the label to show ("R1 tied to
                # <emoji>" for a ladder pick - see utils.pick_origin_label).
                picks = [
                    (pick_number, round_number,
                     pick_origin_label(pick_type, pick_origin, round_number,
                                       get_team_emoji_str(self.bot, original_emoji, trailing_space=False)
                                       or original_name),
                     current_team, current_emoji, player_selected)
                    for (pick_number, round_number, pick_origin, current_team, current_emoji, player_selected,
                         pick_type, original_name, original_emoji) in await cursor.fetchall()
                ]

                if not picks:
                    await interaction.followup.send(
                        f"❌ No draft picks found for '{draft_name}'!\n"
                        f"This draft may not have a ladder order set yet."
                    )
                    return

                # Create paginated view
                view = DraftOrderView(picks, draft_name, interaction.guild)
                embed = view.create_embed()
                await interaction.followup.send(embed=embed, view=view)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}")

    async def all_drafts_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for all draft names (current and future)"""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    """SELECT draft_name FROM drafts
                       ORDER BY draft_id DESC"""
                )
                drafts = await cursor.fetchall()

            choices = []
            for (draft_name,) in drafts:
                if current.lower() in draft_name.lower():
                    choices.append(app_commands.Choice(name=draft_name, value=draft_name))

            return choices[:25]
        except Exception:
            return []

    @app_commands.command(name="viewdraftpool", description="View the players available in the draft pool")
    @app_commands.describe(position="Optional: only show players of this position")
    @app_commands.autocomplete(position=draft_pool_position_autocomplete)
    async def view_draft_pool(self, interaction: discord.Interaction, position: str = None):
        """Browsable list of everyone currently in the Draft Pool.

        Open to everyone, not admin-only - coaches need to see who's
        available before a draft the same way /draftorder lets them see the
        picks. OVR is deliberately NOT shown, matching how the Draft Pool is
        treated everywhere else (the live draft's own player dropdown,
        /player, /roster, /filterplayers) - pool players' ratings are hidden
        until they're actually drafted onto a list.
        """
        await interaction.response.defer(ephemeral=True)

        normalized_position = None
        if position is not None:
            from positions import validate_position
            is_valid, normalized_position = validate_position(position)
            if not is_valid:
                await interaction.followup.send(
                    f"❌ Invalid position '{position}'. Pick one from the autocomplete suggestions.",
                    ephemeral=True
                )
                return

        async with aiosqlite.connect(DB_PATH) as db:
            query = """SELECT p.name, p.position, p.age, t_fs.team_name
                       FROM players p
                       JOIN teams t ON p.team_id = t.team_id
                       LEFT JOIN teams t_fs ON p.father_son_club_id = t_fs.team_id
                       WHERE t.team_name = 'Draft Pool'"""
            params = []
            if normalized_position is not None:
                query += " AND p.position = ?"
                params.append(normalized_position)

            # Ordered by position (POSITION_DISPLAY_ORDER, same as /roster)
            # then name, so the list reads as a scouting board rather than a
            # flat alphabetical dump.
            from positions import POSITION_DISPLAY_ORDER
            case_parts = " ".join(
                f"WHEN p.position = '{pos}' THEN {idx}"
                for idx, pos in enumerate(POSITION_DISPLAY_ORDER)
            )
            query += f" ORDER BY CASE {case_parts} ELSE 999 END, p.name"

            cursor = await db.execute(query, params)
            players = await cursor.fetchall()

        if not players:
            if normalized_position is not None:
                await interaction.followup.send(
                    f"No {normalized_position} players in the draft pool.", ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "The draft pool is empty! Use `/updateplayer` to assign players to the 'Draft Pool' team.",
                    ephemeral=True
                )
            return

        view = DraftPoolView(players, normalized_position)
        await interaction.followup.send(embed=view.create_embed(), view=view, ephemeral=True)

    @app_commands.command(name="editdraft", description="[ADMIN] Edit a draft - name, rounds, and its picks")
    @app_commands.describe(draft_name="Draft to edit")
    @app_commands.autocomplete(draft_name=all_drafts_autocomplete)
    async def edit_draft(self, interaction: discord.Interaction, draft_name: str):
        """Menu-driven editor for a draft, replacing the old one-shot
        /addpick, /removepick and /transferpick commands - those each took
        the draft and pick as typed parameters and gave no view of what they
        were changing, so an admin had to run /draftorder alongside them to
        see the effect. Here the pick list is shown and re-rendered after
        every edit."""
        await interaction.response.defer(ephemeral=True)

        if not await is_admin_user(interaction):
            await interaction.followup.send("❌ You don't have permission to use this command.", ephemeral=True)
            return

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                """SELECT draft_id, draft_name, season_number, status, rounds, rookie_contract_years
                   FROM drafts WHERE draft_name = ?""",
                (draft_name,)
            )
            draft = await cursor.fetchone()
            if not draft:
                await interaction.followup.send(
                    f"❌ Draft '{draft_name}' not found!", ephemeral=True
                )
                return

            teams = await fetch_teams_for_dropdown(db)

            view = EditDraftView(self.bot, draft, teams)
            await view.refresh(db)

        await interaction.followup.send(embed=view.create_embed(), view=view, ephemeral=True)

    @app_commands.command(name="drafthand", description="View a team's draft picks")
    @app_commands.describe(team="Team to view (defaults to your team)")
    @app_commands.autocomplete(team=team_autocomplete)
    async def draft_hand(self, interaction: discord.Interaction, team: str = None):
        await interaction.response.defer(ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as db:
            # Get team ID - default to user's team if not specified
            if team is None:
                # Get user's team from their role
                cursor = await db.execute(
                    "SELECT team_id, team_name FROM teams ORDER BY team_name"
                )
                teams = await cursor.fetchall()

                user_team_id = None
                user_team_name = None
                for team_id, team_name in teams:
                    cursor = await db.execute(
                        "SELECT role_id FROM teams WHERE team_id = ?",
                        (team_id,)
                    )
                    role_result = await cursor.fetchone()
                    if role_result and role_result[0]:
                        role = interaction.guild.get_role(int(role_result[0]))
                        if role and role in interaction.user.roles:
                            user_team_id = team_id
                            user_team_name = team_name
                            break

                if not user_team_id:
                    await interaction.followup.send(
                        "❌ You don't have a team role! Please specify a team.",
                        ephemeral=True
                    )
                    return

                target_team_id = user_team_id
                target_team_name = user_team_name
            else:
                # Look up specified team
                cursor = await db.execute(
                    "SELECT team_id, team_name FROM teams WHERE LOWER(team_name) = LOWER(?)",
                    (team,)
                )
                team_result = await cursor.fetchone()
                if not team_result:
                    await interaction.followup.send(f"❌ Team '{team}' not found!", ephemeral=True)
                    return

                target_team_id, target_team_name = team_result

            # Get team emoji
            cursor = await db.execute(
                "SELECT emoji_id FROM teams WHERE team_id = ?",
                (target_team_id,)
            )
            emoji_result = await cursor.fetchone()
            team_emoji = None
            if emoji_result and emoji_result[0]:
                team_emoji = get_team_emoji(self.bot, emoji_result[0])

            # Get current active season
            cursor = await db.execute(
                "SELECT season_number FROM seasons WHERE status = 'active' LIMIT 1"
            )
            active_season_result = await cursor.fetchone()
            current_season = active_season_result[0] if active_season_result else 999

            # Get all picks for this team, grouped by season
            cursor = await db.execute(
                """SELECT dp.season_number, dp.pick_number, dp.round_number,
                          dp.pick_origin, t.emoji_id, d.draft_name
                   FROM draft_picks dp
                   JOIN teams t ON dp.original_team_id = t.team_id
                   LEFT JOIN drafts d ON dp.draft_id = d.draft_id
                   WHERE dp.current_team_id = ?
                     AND dp.player_selected_id IS NULL
                     AND COALESCE(dp.passed, 0) = 0
                     AND dp.draft_id NOT IN (SELECT draft_id FROM drafts WHERE status = 'completed')
                   ORDER BY dp.season_number ASC NULLS FIRST,
                            dp.pick_number ASC NULLS LAST,
                            dp.round_number ASC""",
                (target_team_id,)
            )
            all_picks = await cursor.fetchall()

            if not all_picks:
                await interaction.followup.send(
                    f"❌ {target_team_name} has no draft picks!",
                    ephemeral=True
                )
                return

            # Group picks by season
            picks_by_season = {}
            for season_num, pick_num, round_num, pick_origin, orig_emoji_id, draft_name in all_picks:
                if season_num not in picks_by_season:
                    picks_by_season[season_num] = []
                picks_by_season[season_num].append((pick_num, round_num, pick_origin, orig_emoji_id, draft_name))

            # Build embed
            team_emoji_str = f"{team_emoji} " if team_emoji else ""
            embed = discord.Embed(
                title=f"{team_emoji_str}{target_team_name} Draft Hand",
                color=discord.Color.blue()
            )

            # Collect all picks in one list
            all_pick_lines = []
            for season_num in sorted(picks_by_season.keys(), key=lambda x: (x is None, x)):
                picks = picks_by_season[season_num]

                for pick_num, round_num, pick_origin, orig_emoji_id, draft_name in picks:
                    if pick_num is not None:
                        # Current pick with number
                        all_pick_lines.append(f"Pick #{pick_num}")
                    else:
                        # Future pick - format as "Future 1st ([emoji] S10)"
                        round_suffix = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}.get(round_num, f"{round_num}th")
                        orig_emoji = None
                        if orig_emoji_id:
                            orig_emoji = get_team_emoji(self.bot, orig_emoji_id)
                        emoji_str = f"{orig_emoji} " if orig_emoji else ""
                        # Use season_num - 1 for display (draft naming convention)
                        all_pick_lines.append(f"Future {round_suffix} ({emoji_str}S{season_num - 1})")

            # Display all picks in embed description
            embed.description = "\n".join(all_pick_lines) if all_pick_lines else "*No picks*"

            await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="draftpoints", description="View the points value for all draft pick numbers")
    async def draft_points(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as db:
            # Get all draft point values
            cursor = await db.execute(
                """SELECT pick_number, points_value FROM draft_value_index
                   WHERE points_value > 0
                   ORDER BY pick_number ASC"""
            )
            all_picks = await cursor.fetchall()

            if not all_picks:
                await interaction.followup.send("❌ No draft point values found!", ephemeral=True)
                return

            # Format into multiple columns
            embed = discord.Embed(
                title="Draft Value Index",
                color=discord.Color.blue()
            )
            embed.set_footer(text="*Reminder that F/S bids can be matched with a 20% discount!*")

            # Split into 3 columns of ~30 picks each
            picks_per_column = 30
            columns = []

            for i in range(0, len(all_picks), picks_per_column):
                column_picks = all_picks[i:i + picks_per_column]
                column_text = "\n".join([f"**#{pick}:** {points}" for pick, points in column_picks])
                columns.append(column_text)

            # Add columns to embed (max 3 inline fields per row)
            for idx, column in enumerate(columns):
                embed.add_field(
                    name="\u200b",  # Zero-width space for empty header
                    value=column,
                    inline=True
                )

            await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="draftpointscalculator", description="Calculate the highest bid your draft picks can match")
    async def draft_points_calculator(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as db:
            # Check if there's a current or in-progress draft
            cursor = await db.execute(
                """SELECT draft_id, draft_name FROM drafts
                   WHERE status IN ('current', 'in_progress')
                   ORDER BY draft_id DESC
                   LIMIT 1"""
            )
            draft_result = await cursor.fetchone()

            # Get all pick numbers with point values from draft_value_index
            cursor = await db.execute(
                """SELECT pick_number, points_value FROM draft_value_index
                   WHERE points_value > 0
                   ORDER BY pick_number ASC"""
            )
            index_picks = await cursor.fetchall()

            if not index_picks:
                await interaction.followup.send(
                    "❌ No draft point values found in the draft value index!",
                    ephemeral=True
                )
                return

            # Convert to format expected by view: (pick_id, pick_number, emoji_id, team_name, points_value)
            all_picks = []

            if draft_result:
                # If there's an active draft, get team info for each pick
                draft_id, draft_name = draft_result
                for pick_number, points_value in index_picks:
                    # Try to get team emoji for this pick from the draft
                    cursor = await db.execute(
                        """SELECT t.emoji_id, t.team_name
                           FROM draft_picks dp
                           JOIN teams t ON dp.current_team_id = t.team_id
                           WHERE dp.draft_id = ? AND dp.pick_number = ?""",
                        (draft_id, pick_number)
                    )
                    team_result = await cursor.fetchone()

                    if team_result:
                        emoji_id, team_name = team_result
                        all_picks.append((pick_number, pick_number, emoji_id, team_name, points_value))
                    else:
                        # Pick exists in index but not in draft yet
                        all_picks.append((pick_number, pick_number, None, None, points_value))
            else:
                # No active draft - just use pick numbers
                draft_name = None
                for pick_number, points_value in index_picks:
                    all_picks.append((pick_number, pick_number, None, None, points_value))

        # Create the calculator view
        view = DraftPointsCalculatorView(self.bot, draft_name, all_picks, interaction.guild)
        view.update_dropdown()  # Initialize the dropdown with first page
        embed = view.create_embed()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="livedraft", description="[ADMIN] Start a live draft (or re-post its control panel)")
    @app_commands.describe(draft_name="Name of the draft")
    @app_commands.autocomplete(draft_name=draft_name_autocomplete)
    async def live_draft(self, interaction: discord.Interaction, draft_name: str):
        """Posts a draft's control panel in the admin controls channel - the
        draft itself starts from the panel's Start Draft button (see
        start_draft). Run again, it re-posts the panel, e.g. if the panel
        message was deleted."""
        await interaction.response.defer(ephemeral=True)

        if not await is_admin_user(interaction):
            await interaction.followup.send("❌ You don't have permission to use this command.", ephemeral=True)
            return

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "SELECT draft_id, status FROM drafts WHERE draft_name = ?", (draft_name,)
                )
                draft_info = await cursor.fetchone()
                if not draft_info:
                    await interaction.followup.send(f"❌ Draft '{draft_name}' not found!", ephemeral=True)
                    return
                draft_id, status = draft_info

                if status not in ('current', 'in_progress', 'completed'):
                    await interaction.followup.send(
                        f"❌ Draft '{draft_name}' is not ready yet (status: {status})!\n"
                        f"A season-linked draft's order is set automatically once its season starts; "
                        f"a custom draft's order is set at creation via `/createcustomdraft`.",
                        ephemeral=True
                    )
                    return

                panel = await post_draft_panel(self.bot, db, draft_id)
                if panel is None:
                    await interaction.followup.send(
                        "❌ No admin controls channel is set to post the control panel in - set one with "
                        "`/config`, then run `/livedraft` again.",
                        ephemeral=True
                    )
                    return
                follow_up = ("Press **Start Draft** on it when you're ready." if status == 'current'
                             else "The draft is already underway - this replaces its old panel.")
                await interaction.followup.send(
                    f"✅ Control panel for **{draft_name}** posted in {panel.channel.mention}. {follow_up}",
                    ephemeral=True
                )

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)
            import traceback
            traceback.print_exc()

    async def start_draft(self, db, draft_id):
        """Starts a draft (the control panel's Start Draft button): checks
        the draft channel and Draft Pool, sets it running, posts its title
        in the draft channel and puts pick 1 on the clock (which also takes
        the pick 1 snapshot - the draft before it began). Returns None, or
        why it couldn't start."""
        cursor = await db.execute(
            "SELECT draft_name, status, started_at FROM drafts WHERE draft_id = ?", (draft_id,)
        )
        draft_name, status, started_at = await cursor.fetchone()
        if status != 'current' or started_at:
            return "❌ This draft has already been started."

        draft_channel = await _draft_channel(self.bot, db)
        if not draft_channel:
            return "❌ No draft channel configured (or it can't be found)! Use `/config` to set it."

        cursor = await db.execute(
            """SELECT COUNT(*) FROM players p
               JOIN teams t ON p.team_id = t.team_id
               WHERE t.team_name = 'Draft Pool'"""
        )
        if (await cursor.fetchone())[0] == 0:
            return "❌ No players in the Draft Pool! Use `/importplayers` or `/addplayer` to add prospects."

        await db.execute(
            """UPDATE drafts
               SET status = 'in_progress', started_at = CURRENT_TIMESTAMP, current_pick_number = 1
               WHERE draft_id = ?""",
            (draft_id,)
        )
        await db.commit()
        await draft_channel.send(f"# {draft_name}")
        await self.send_pick_notification(db, draft_id, draft_name, 1)
        return None

    async def send_pick_notification(self, db, draft_id, draft_name, pick_number, round_header=True):
        """Send draft pick notification to team's channel. round_header
        False skips the "-- ROUND N --" line even on a round's first pick -
        for re-notifying a pick already announced (e.g. after it's traded)."""
        try:
            # Check if there are any players left in the draft pool
            cursor = await db.execute(
                """SELECT COUNT(*) FROM players p
                   JOIN teams t ON p.team_id = t.team_id
                   WHERE t.team_name = 'Draft Pool'"""
            )
            players_in_pool = (await cursor.fetchone())[0]

            if players_in_pool == 0:
                # No players left - auto-end draft
                cursor = await db.execute(
                    "SELECT setting_value FROM settings WHERE setting_key = 'draft_channel_id'"
                )
                result = await cursor.fetchone()
                draft_channel_id = int(result[0]) if result and result[0] else None
                draft_channel = self.bot.get_channel(draft_channel_id) if draft_channel_id else None

                if draft_channel:
                    await draft_channel.send("**No players left in draft pool**")

                await self.complete_draft(db, draft_id, draft_name)
                return

            # Get the pick info
            cursor = await db.execute(
                """SELECT dp.current_team_id, dp.round_number, dp.pick_number, t.team_name, t.channel_id
                   FROM draft_picks dp
                   JOIN teams t ON dp.current_team_id = t.team_id
                   WHERE dp.draft_id = ? AND dp.pick_number = ? AND dp.player_selected_id IS NULL""",
                (draft_id, pick_number)
            )
            pick_info = await cursor.fetchone()

            if not pick_info:
                # Draft is complete
                await self.complete_draft(db, draft_id, draft_name)
                return

            team_id, round_number, pick_num, team_name, channel_id = pick_info

            # Record the draft as it stands with this pick on the clock, so
            # the control panel can rewind to it (see restore_draft_snapshot).
            await snapshot_draft(db, draft_id, pick_number)
            await db.commit()

            if not channel_id:
                print(f"No channel configured for team {team_name}")
                return

            team_channel = self.bot.get_channel(int(channel_id))
            if not team_channel:
                print(f"Channel not found for team {team_name}")
                return

            # Get team emoji
            cursor = await db.execute("SELECT emoji_id FROM teams WHERE team_id = ?", (team_id,))
            emoji_result = await cursor.fetchone()
            team_emoji = ""
            if emoji_result and emoji_result[0]:
                team_emoji = get_team_emoji_str(self.bot, emoji_result[0])

            # Get draft channel
            cursor = await db.execute(
                "SELECT setting_value FROM settings WHERE setting_key = 'draft_channel_id'"
            )
            result = await cursor.fetchone()
            draft_channel_id = int(result[0]) if result and result[0] else None
            draft_channel = self.bot.get_channel(draft_channel_id) if draft_channel_id else None

            # Post round header if this is the first pick of a new round
            if draft_channel:
                # Get the number of picks in each round
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM draft_picks WHERE draft_id = ? AND round_number = 1",
                    (draft_id,)
                )
                picks_per_round = (await cursor.fetchone())[0]

                # Check if this pick number is the start of a new round
                if round_header and (pick_num - 1) % picks_per_round == 0:
                    await draft_channel.send(f"**-- ROUND {round_number} --**")

                # Post "on the clock" message
                await draft_channel.send(f"{team_emoji}are on the clock...")

            # Send interactive notification to team channel
            view = DraftPickView(self.bot, draft_id, draft_name, team_id, pick_number)
            embed = await view.create_embed(db)
            await team_channel.send(embed=embed, view=view)

        except Exception as e:
            print(f"Error sending pick notification: {e}")
            import traceback
            traceback.print_exc()
        await refresh_draft_panel(self.bot, db, draft_id)

    async def announce_traded_live_picks(self, pick_ids):
        """Called after a trade is approved: if any of its picks is on the
        clock in a running draft, says so in the draft channel and sends a
        fresh "on the clock" notification to the pick's new owner (the old
        owner's notification stops working - see draft_pick_problem)."""
        pick_ids = set(pick_ids)
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                """SELECT d.draft_id, d.draft_name, d.current_pick_number, dp.pick_id, t.team_name, t.emoji_id
                   FROM drafts d
                   JOIN draft_picks dp ON dp.draft_id = d.draft_id AND dp.pick_number = d.current_pick_number
                   JOIN teams t ON dp.current_team_id = t.team_id
                   WHERE d.status = 'in_progress' AND dp.player_selected_id IS NULL AND dp.passed = 0"""
            )
            on_clock = [row for row in await cursor.fetchall() if row[3] in pick_ids]
            for draft_id, draft_name, pick_number, _, team_name, emoji_id in on_clock:
                channel = await _draft_channel(self.bot, db)
                if channel:
                    await channel.send(
                        f"🔁 **Pick {pick_number}** has been traded to "
                        f"{get_team_emoji_str(self.bot, emoji_id)}**{team_name}**!"
                    )
                await self.send_pick_notification(db, draft_id, draft_name, pick_number, round_header=False)

    async def complete_draft(self, db, draft_id, draft_name):
        """Mark draft as completed and post completion message"""
        try:
            # Update draft status
            await db.execute(
                """UPDATE drafts
                   SET status = 'completed', completed_at = CURRENT_TIMESTAMP
                   WHERE draft_id = ?""",
                (draft_id,)
            )
            await db.commit()

            # Delist all remaining Draft Pool players
            cursor = await db.execute(
                "SELECT team_id FROM teams WHERE team_name = 'Draft Pool'"
            )
            draft_pool_result = await cursor.fetchone()
            if draft_pool_result:
                draft_pool_id = draft_pool_result[0]
                await db.execute(
                    "UPDATE players SET team_id = NULL WHERE team_id = ?",
                    (draft_pool_id,)
                )
                await db.commit()
                print(f"Delisted all remaining Draft Pool players")

            # Get draft channel
            cursor = await db.execute(
                "SELECT setting_value FROM settings WHERE setting_key = 'draft_channel_id'"
            )
            result = await cursor.fetchone()
            draft_channel_id = int(result[0]) if result and result[0] else None
            draft_channel = self.bot.get_channel(draft_channel_id) if draft_channel_id else None

            if draft_channel:
                await draft_channel.send(f"# End of Draft")

            await self.post_team_draft_summaries(db, draft_id, draft_name)

            print(f"Draft '{draft_name}' completed!")
            await refresh_draft_panel(self.bot, db, draft_id)

        except Exception as e:
            print(f"Error completing draft: {e}")
            import traceback
            traceback.print_exc()

    async def post_team_draft_summaries(self, db, draft_id, draft_name):
        """Sends each club that made at least one selection in this draft a
        summary in its team channel: every player it took (in pick order)
        and its list size now the draft's over."""
        cursor = await db.execute(
            """SELECT dp.current_team_id, dp.pick_number, dp.pick_origin,
                      p.name, p.position, p.age, p.overall_rating
               FROM draft_picks dp
               JOIN players p ON p.player_id = dp.player_selected_id
               WHERE dp.draft_id = ?
               ORDER BY dp.pick_number""",
            (draft_id,)
        )
        by_team = {}
        for team_id, pick_num, origin, name, pos, age, ovr in await cursor.fetchall():
            line = f"**Pick {pick_num}:** **{name}** ({pos}, {age} yo, {ovr} OVR)"
            if origin and origin.endswith("F/S Match"):
                line += " - *F/S match*"
            by_team.setdefault(team_id, []).append(line)

        for team_id, lines in by_team.items():
            try:
                cursor = await db.execute(
                    "SELECT team_name, emoji_id, channel_id FROM teams WHERE team_id = ?", (team_id,)
                )
                team = await cursor.fetchone()
                if not team or not team[2]:
                    continue
                team_name, emoji_id, channel_id = team
                channel = self.bot.get_channel(int(channel_id))
                if not channel:
                    print(f"Channel not found for team {team_name}")
                    continue
                cursor = await db.execute("SELECT COUNT(*) FROM players WHERE team_id = ?", (team_id,))
                list_size = (await cursor.fetchone())[0]

                description = "\n".join(lines)
                if len(description) > 4000:
                    description = description[:4000].rsplit("\n", 1)[0] + "\n..."
                embed = discord.Embed(
                    title=f"{get_team_emoji_str(self.bot, emoji_id) if emoji_id else ''}{draft_name} Summary",
                    description=description,
                    color=discord.Color.blue()
                )
                embed.set_footer(text=f"List Size: {list_size}/44")
                await channel.send(embed=embed)
            except Exception as e:
                print(f"Error sending draft summary to team {team_id}: {e}")


async def void_trades_for_picks(bot, pick_ids):
    """Voids any open trade offer that includes these picks now they've
    been used, passed, or spent on a father-son match in the draft (see
    TradeCommands.void_stale_trades). Call after the change is committed."""
    trade_cog = bot.get_cog('TradeCommands')
    if trade_cog and pick_ids:
        await trade_cog.void_stale_trades(None, [], list(pick_ids))


async def draft_pick_problem(db, draft_id, pick_number, team_id=None):
    """Why a pick (or a father-son decision on it) can't be acted on now,
    or None: its draft must still be running, with pick_number the pick
    on the clock and not yet made - and, given team_id, still owned by that
    team. Guards every draft button - an old notification (left over after
    a rewind or abandon, a re-send, or the pick being traded away) would
    otherwise make a pick out of turn, for the wrong club, or in a draft
    that's been reset."""
    cursor = await db.execute("SELECT status, current_pick_number FROM drafts WHERE draft_id = ?", (draft_id,))
    draft = await cursor.fetchone()
    if not draft or draft[0] != 'in_progress':
        return "❌ This draft isn't running any more, so this pick can't be made."
    if draft[1] != pick_number:
        return "❌ This pick isn't on the clock any more."
    cursor = await db.execute(
        "SELECT player_selected_id, passed, current_team_id FROM draft_picks WHERE draft_id = ? AND pick_number = ?",
        (draft_id, pick_number)
    )
    pick = await cursor.fetchone()
    if not pick or pick[0] is not None or pick[1]:
        return "❌ This pick has already been made."
    if team_id is not None and pick[2] != team_id:
        return "❌ This pick has been traded to another club."
    return None


async def snapshot_draft(db, draft_id, pick_number):
    """Records a draft as it stands with pick_number on the clock: every
    pick row (all columns, so father-son picks added later, and their
    renumbering, can be undone exactly) and every Draft Pool player with
    their contract_expiry. Pick 1's snapshot is the draft before it began.
    restore_draft_snapshot puts one back. Replaces an earlier snapshot of
    the same pick (e.g. a re-sent notification)."""
    cursor = await db.execute("SELECT * FROM draft_picks WHERE draft_id = ?", (draft_id,))
    columns = [d[0] for d in cursor.description]
    picks = [dict(zip(columns, row)) for row in await cursor.fetchall()]
    cursor = await db.execute(
        """SELECT p.player_id, p.contract_expiry FROM players p
           JOIN teams t ON p.team_id = t.team_id WHERE t.team_name = 'Draft Pool'"""
    )
    pool = [list(row) for row in await cursor.fetchall()]
    await db.execute(
        """INSERT OR REPLACE INTO draft_snapshots (draft_id, pick_number, picks, pool_players)
           VALUES (?, ?, ?, ?)""",
        (draft_id, pick_number, json.dumps(picks), json.dumps(pool))
    )


async def restore_draft_snapshot(db, draft_id, pick_number, abandon=False):
    """Puts a draft back exactly as it was when pick_number came on the
    clock (see snapshot_draft). Every pick row is replaced by its snapshot
    (same pick ids, so trade references stay valid; picks added since, like
    father-son picks, disappear). Every player who was in the Draft Pool
    then goes back there with their contract from then, and out of any
    lineup they've been put in since.

    Rewinding (abandon=False) leaves the draft running with pick_number on
    the clock - the caller re-sends its notification. Abandoning restores
    pick 1 and returns the draft to unstarted ('current'), ready for
    /livedraft again; every snapshot of it is then deleted. Doesn't commit.
    Returns how many players went back to the Draft Pool."""
    from commands.lineup_commands import clear_departed_players_from_lineups

    cursor = await db.execute(
        "SELECT picks, pool_players FROM draft_snapshots WHERE draft_id = ? AND pick_number = ?",
        (draft_id, pick_number)
    )
    picks_json, pool_json = await cursor.fetchone()
    picks, pool = json.loads(picks_json), json.loads(pool_json)

    cursor = await db.execute("PRAGMA table_info(draft_picks)")
    current_columns = {row[1] for row in await cursor.fetchall()}
    await db.execute("DELETE FROM draft_picks WHERE draft_id = ?", (draft_id,))
    for pick in picks:
        columns = [c for c in pick if c in current_columns]
        await db.execute(
            f"INSERT INTO draft_picks ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
            [pick[c] for c in columns]
        )

    cursor = await db.execute("SELECT team_id FROM teams WHERE team_name = 'Draft Pool'")
    pool_team_id = (await cursor.fetchone())[0]
    returned = 0
    for player_id, contract_expiry in pool:
        cursor = await db.execute("SELECT team_id FROM players WHERE player_id = ?", (player_id,))
        row = await cursor.fetchone()
        if row is None:
            continue  # removed from the league since - nothing to put back
        if row[0] != pool_team_id:
            returned += 1
            if row[0] is not None:
                await clear_departed_players_from_lineups(db, [player_id], row[0])
        await db.execute(
            "UPDATE players SET team_id = ?, contract_expiry = ? WHERE player_id = ?",
            (pool_team_id, contract_expiry, player_id)
        )

    if abandon:
        await db.execute(
            """UPDATE drafts SET status = 'current', started_at = NULL, completed_at = NULL,
                                 current_pick_number = 0
               WHERE draft_id = ?""",
            (draft_id,)
        )
        await db.execute("DELETE FROM draft_snapshots WHERE draft_id = ?", (draft_id,))
    else:
        await db.execute(
            """UPDATE drafts SET status = 'in_progress', completed_at = NULL, current_pick_number = ?
               WHERE draft_id = ?""",
            (pick_number, draft_id)
        )
        await db.execute(
            "DELETE FROM draft_snapshots WHERE draft_id = ? AND pick_number > ?", (draft_id, pick_number)
        )
    return returned


async def _draft_channel(bot, db):
    cursor = await db.execute("SELECT setting_value FROM settings WHERE setting_key = 'draft_channel_id'")
    row = await cursor.fetchone()
    return bot.get_channel(int(row[0])) if row and row[0] else None


async def build_draft_panel_embed(bot, db, draft_id):
    """The control panel's embed: what's happening, then the current
    round's picks (the round on the clock, or the last round picked in once
    the draft is over), with the pick on the clock marked."""
    cursor = await db.execute(
        "SELECT draft_name, status, current_pick_number FROM drafts WHERE draft_id = ?", (draft_id,)
    )
    draft_name, status, current_pick = await cursor.fetchone()

    cursor = await db.execute(
        """SELECT dp.pick_number, dp.round_number, dp.pick_origin, dp.pick_type, ct.team_name, ct.emoji_id,
                  ot.team_name, ot.emoji_id, p.name, dp.passed
           FROM draft_picks dp
           LEFT JOIN teams ct ON dp.current_team_id = ct.team_id
           LEFT JOIN teams ot ON dp.original_team_id = ot.team_id
           LEFT JOIN players p ON dp.player_selected_id = p.player_id
           WHERE dp.draft_id = ? AND dp.pick_number IS NOT NULL
           ORDER BY dp.pick_number""",
        (draft_id,)
    )
    picks = await cursor.fetchall()
    made = sum(1 for row in picks if row[8] or row[9])

    on_clock = next((row for row in picks if row[0] == current_pick), None) if status == 'in_progress' else None
    if on_clock:
        shown_round = on_clock[1]
    else:
        used_rounds = [row[1] for row in picks if row[8] or row[9]]
        shown_round = max(used_rounds) if used_rounds else (picks[0][1] if picks else 1)

    if status == 'in_progress' and on_clock:
        headline = (f"⏰ **Pick {current_pick}** (Round {on_clock[1]}) is on the clock: "
                    f"{get_team_emoji_str(bot, on_clock[5])}**{on_clock[4]}**")
        color = discord.Color.green()
    elif status == 'in_progress':
        headline = "⏰ Waiting on the next pick."
        color = discord.Color.green()
    elif status == 'completed':
        headline = "✅ **Draft complete.** Rewind or Abandon can still undo it."
        color = discord.Color.dark_grey()
    else:
        cursor = await db.execute(
            """SELECT COUNT(*) FROM players p JOIN teams t ON p.team_id = t.team_id
               WHERE t.team_name = 'Draft Pool'"""
        )
        pool_size = (await cursor.fetchone())[0]
        headline = (f"📋 **Ready to start** - {pool_size} prospects in the Draft Pool. "
                    "Press **Start Draft** to put pick 1 on the clock.")
        color = discord.Color.blurple()

    lines = []
    for (number, round_number, origin, pick_type, team_name, emoji_id, original_name, original_emoji,
         player_name, passed) in picks:
        if round_number != shown_round:
            continue
        label = pick_origin_label(pick_type, origin, round_number,
                                  get_team_emoji_str(bot, original_emoji, trailing_space=False) or original_name)
        line = format_draft_pick_line(bot, number, label, emoji_id, player_name)
        if passed:
            line += " → *passed*"
        if on_clock and number == current_pick:
            line = f"⏰ {line}"
        lines.append(line)

    embed = discord.Embed(title=f"🎛️ Live Draft Control — {draft_name}", color=color)
    body = headline + f"\n\n**Round {shown_round}**\n" + ("\n".join(lines) if lines else "*No picks*")
    if len(body) > 4000:
        body = body[:3990] + "\n…"
    embed.description = body
    embed.set_footer(text=f"{made} of {len(picks)} picks made")
    return embed


async def post_draft_panel(bot, db, draft_id):
    """Posts a draft's control panel in the admin controls channel (the
    live match control panel's channel) and records where, replacing any
    earlier panel for it. Returns the message, or None if no channel is set."""
    cursor = await db.execute(
        "SELECT setting_value FROM settings WHERE setting_key = 'live_match_control_channel_id'"
    )
    row = await cursor.fetchone()
    channel = bot.get_channel(int(row[0])) if row and row[0] else None
    if channel is None:
        return None

    cursor = await db.execute(
        "SELECT status, control_channel_id, control_message_id FROM drafts WHERE draft_id = ?", (draft_id,)
    )
    status, old_channel_id, old_message_id = await cursor.fetchone()
    if old_message_id:
        old_channel = bot.get_channel(int(old_channel_id)) if old_channel_id else None
        if old_channel:
            try:
                await old_channel.get_partial_message(int(old_message_id)).delete()
            except Exception:
                pass  # already deleted

    message = await channel.send(embed=await build_draft_panel_embed(bot, db, draft_id),
                                 view=DraftControlView(bot, draft_id, status))
    await db.execute(
        "UPDATE drafts SET control_channel_id = ?, control_message_id = ? WHERE draft_id = ?",
        (str(channel.id), str(message.id), draft_id)
    )
    await db.commit()
    return message


async def refresh_draft_panel(bot, db, draft_id):
    """Redraws a draft's control panel, if it has one - called whenever a
    pick comes on the clock, and after ending, rewinding or abandoning."""
    try:
        cursor = await db.execute(
            "SELECT status, control_channel_id, control_message_id FROM drafts WHERE draft_id = ?", (draft_id,)
        )
        row = await cursor.fetchone()
        if not row or not row[2]:
            return
        status, channel_id, message_id = row
        channel = bot.get_channel(int(channel_id)) if channel_id else None
        if channel is None:
            return
        await channel.get_partial_message(int(message_id)).edit(
            embed=await build_draft_panel_embed(bot, db, draft_id),
            view=DraftControlView(bot, draft_id, status)
        )
    except Exception as e:
        print(f"Couldn't update the control panel for draft {draft_id}: {e}")


class DraftControlView(discord.ui.View):
    """A live draft's control panel buttons (admins only). Persistent: the
    custom ids carry the draft id, and DraftCommands.cog_load re-registers
    every panel after a restart. Each button re-checks the draft's status
    when pressed, so a stale panel can't act on the wrong state."""

    def __init__(self, bot, draft_id, status):
        super().__init__(timeout=None)
        self.bot = bot
        self.draft_id = draft_id
        running = status == 'in_progress'
        started = status in ('in_progress', 'completed')
        for label, style, action, enabled in (
            ("▶ Start Draft", discord.ButtonStyle.success, "start", status == 'current'),
            ("🎲 Auto Pick", discord.ButtonStyle.primary, "autopick", running),
            ("🔁 Re-send Pick Notification", discord.ButtonStyle.secondary, "resend", running),
            ("⏪ Rewind...", discord.ButtonStyle.secondary, "rewind", started),
            ("⏹ End Draft", discord.ButtonStyle.danger, "end", running),
            ("🗑 Abandon Draft", discord.ButtonStyle.danger, "abandon", started),
        ):
            button = discord.ui.Button(label=label, style=style, disabled=not enabled,
                                       custom_id=f"draftpanel:{action}:{draft_id}")
            button.callback = getattr(self, f"_{action}")
            self.add_item(button)

    async def interaction_check(self, interaction):
        if await is_admin_user(interaction):
            return True
        await interaction.response.send_message("❌ Only admins can use the draft control panel.", ephemeral=True)
        return False

    async def _draft(self, db):
        cursor = await db.execute(
            "SELECT draft_name, status, current_pick_number FROM drafts WHERE draft_id = ?", (self.draft_id,)
        )
        return await cursor.fetchone()

    async def _start(self, interaction):
        await interaction.response.defer(ephemeral=True)
        async with aiosqlite.connect(DB_PATH) as db:
            problem = await self.bot.get_cog('DraftCommands').start_draft(db, self.draft_id)
        await interaction.followup.send(problem or "✅ Draft started - pick 1 is on the clock.", ephemeral=True)

    async def _autopick(self, interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            draft_name, status, current_pick = await self._draft(db)
            if status != 'in_progress':
                await interaction.response.send_message("❌ This draft isn't running.", ephemeral=True)
                return
            cursor = await db.execute(
                """SELECT dp.round_number, t.team_name, t.emoji_id FROM draft_picks dp
                   JOIN teams t ON dp.current_team_id = t.team_id
                   WHERE dp.draft_id = ? AND dp.pick_number = ?""",
                (self.draft_id, current_pick)
            )
            row = await cursor.fetchone()
        if not row:
            await interaction.response.send_message("❌ There's no pick on the clock.", ephemeral=True)
            return
        round_number, team_name, emoji_id = row
        await interaction.response.send_message(
            f"Auto pick for **Pick {current_pick}** (Round {round_number}) - "
            f"{get_team_emoji_str(self.bot, emoji_id)}**{team_name}**? A random player from the Draft Pool "
            "will be drafted for them.",
            view=_DraftActionConfirmView(self.bot, interaction.user.id, self.draft_id, "autopick", current_pick),
            ephemeral=True
        )

    async def _resend(self, interaction):
        await interaction.response.defer(ephemeral=True)
        async with aiosqlite.connect(DB_PATH) as db:
            draft_name, status, current_pick = await self._draft(db)
            if status != 'in_progress':
                await interaction.followup.send("❌ This draft isn't running.", ephemeral=True)
                return
            await self.bot.get_cog('DraftCommands').send_pick_notification(db, self.draft_id, draft_name, current_pick)
        await interaction.followup.send(f"✅ Re-sent the notification for pick {current_pick}.", ephemeral=True)

    async def _rewind(self, interaction):
        await interaction.response.send_modal(_RewindDraftModal(self.bot, self.draft_id))

    async def _end(self, interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            draft_name, status, _ = await self._draft(db)
        if status != 'in_progress':
            await interaction.response.send_message("❌ This draft isn't running.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"End **{draft_name}** now? It's marked complete and every player left in the Draft Pool is "
            "delisted. (Rewind or Abandon can still undo it afterwards.)",
            view=_DraftActionConfirmView(self.bot, interaction.user.id, self.draft_id, "end"),
            ephemeral=True
        )

    async def _abandon(self, interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            draft_name, status, _ = await self._draft(db)
            cursor = await db.execute(
                """SELECT COUNT(*) FROM draft_picks
                   WHERE draft_id = ? AND (player_selected_id IS NOT NULL OR passed = 1)""",
                (self.draft_id,)
            )
            made = (await cursor.fetchone())[0]
        if status not in ('in_progress', 'completed'):
            await interaction.response.send_message("❌ This draft isn't running.", ephemeral=True)
            return
        warning = ("\n\n:warning: This draft has **finished** - any of its players who've since been traded, "
                   "delisted or played will still be moved back to the Draft Pool." if status == 'completed' else "")
        await interaction.response.send_message(
            f"Abandon **{draft_name}**? All {made} pick(s) made or passed are undone: every drafted player goes "
            "back to the Draft Pool with their original contract, every pick (including any father-son picks) "
            "goes back to how it was before the draft began, and it can be started again with **Start Draft**."
            + warning,
            view=_DraftActionConfirmView(self.bot, interaction.user.id, self.draft_id, "abandon"),
            ephemeral=True
        )


class _RewindDraftModal(discord.ui.Modal, title="Rewind Draft"):
    pick = discord.ui.TextInput(label="Rewind to pick number", placeholder="e.g. 14", max_length=4)

    def __init__(self, bot, draft_id):
        super().__init__()
        self.bot = bot
        self.draft_id = draft_id

    async def on_submit(self, interaction: discord.Interaction):
        text = self.pick.value.strip().lstrip("#")
        if not text.isdigit():
            await interaction.response.send_message(f"❌ \"{self.pick.value}\" isn't a pick number.", ephemeral=True)
            return
        target = int(text)
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT draft_name, status, current_pick_number FROM drafts WHERE draft_id = ?", (self.draft_id,)
            )
            draft_name, status, current_pick = await cursor.fetchone()
            cursor = await db.execute(
                "SELECT 1 FROM draft_snapshots WHERE draft_id = ? AND pick_number = ?", (self.draft_id, target)
            )
            has_snapshot = await cursor.fetchone() is not None
            cursor = await db.execute(
                """SELECT dp.pick_number, t.team_name, p.name, dp.passed FROM draft_picks dp
                   LEFT JOIN teams t ON dp.current_team_id = t.team_id
                   LEFT JOIN players p ON dp.player_selected_id = p.player_id
                   WHERE dp.draft_id = ? AND dp.pick_number >= ?
                     AND (dp.player_selected_id IS NOT NULL OR dp.passed = 1)
                   ORDER BY dp.pick_number""",
                (self.draft_id, target)
            )
            undone = await cursor.fetchall()

        if status not in ('in_progress', 'completed'):
            await interaction.response.send_message("❌ This draft isn't running.", ephemeral=True)
            return
        if not has_snapshot:
            reason = ("hasn't come up yet" if status == 'in_progress' and target > current_pick
                      else "can't be rewound to - there's no record of the draft at that point")
            await interaction.response.send_message(f"❌ Pick {target} {reason}.", ephemeral=True)
            return
        if not undone:
            await interaction.response.send_message(
                f"Pick {target} is already the pick on the clock - nothing to undo.", ephemeral=True)
            return

        shown = [f"• #{n} {team or '?'} - {('*passed*' if passed else name)}" for n, team, name, passed in undone[:15]]
        if len(undone) > 15:
            shown.append(f"… and {len(undone) - 15} more")
        await interaction.response.send_message(
            f"Rewind **{draft_name}** to pick {target}? These {len(undone)} pick(s) are undone, their players go "
            "back to the Draft Pool, and pick " + str(target) + " goes back on the clock:\n" + "\n".join(shown),
            view=_DraftActionConfirmView(self.bot, interaction.user.id, self.draft_id, "rewind", target),
            ephemeral=True
        )


async def _auto_pick(bot, db, draft_id, draft_name, pick_number):
    """Drafts a random Draft Pool player with pick_number for the team on
    the clock, through the normal pick path (DraftPickView.process_pick -
    announcement, contract, next pick). Players tied to another club as
    father-son selections are skipped, since picking one starts a bid
    rather than a pick. Returns the result message."""
    problem = await draft_pick_problem(db, draft_id, pick_number)
    if problem:
        return problem.replace("this pick can't be made", "the auto pick wasn't made")
    cursor = await db.execute(
        "SELECT current_team_id FROM draft_picks WHERE draft_id = ? AND pick_number = ?", (draft_id, pick_number)
    )
    team_id = (await cursor.fetchone())[0]
    cursor = await db.execute(
        """SELECT p.player_id, p.name, p.position FROM players p JOIN teams t ON p.team_id = t.team_id
           WHERE t.team_name = 'Draft Pool' AND (p.father_son_club_id IS NULL OR p.father_son_club_id = ?)
           ORDER BY RANDOM() LIMIT 1""",
        (team_id,)
    )
    player = await cursor.fetchone()
    if not player:
        return "❌ There's no player left in the Draft Pool this team can simply pick."
    player_id, name, position = player
    view = DraftPickView(bot, draft_id, draft_name, team_id, pick_number)
    await view.process_pick(db, player_id, False)
    return f"✅ Auto pick made: **{name}** ({position}) with pick {pick_number}."


class _DraftActionConfirmView(discord.ui.View):
    """Confirm / cancel for the panel's End, Abandon and Rewind - only the
    admin who asked can press them."""

    def __init__(self, bot, user_id, draft_id, action, pick_number=None):
        super().__init__(timeout=300)
        self.bot = bot
        self.user_id = user_id
        self.draft_id = draft_id
        self.action = action
        self.pick_number = pick_number
        confirm = discord.ui.Button(
            label={"end": "End draft", "abandon": "Abandon draft", "rewind": f"Rewind to pick {pick_number}",
                   "autopick": "Make auto pick"}[action],
            style=discord.ButtonStyle.primary if action == "autopick" else discord.ButtonStyle.danger,
        )
        confirm.callback = self._confirm
        self.add_item(confirm)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel.callback = self._cancel
        self.add_item(cancel)

    async def interaction_check(self, interaction):
        return interaction.user.id == self.user_id

    async def _confirm(self, interaction):
        self.stop()
        await interaction.response.edit_message(content="⏳ Working...", view=None)
        cog = self.bot.get_cog('DraftCommands')
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT draft_name, status FROM drafts WHERE draft_id = ?", (self.draft_id,))
            draft_name, status = await cursor.fetchone()
            allowed = {'end': ('in_progress',), 'abandon': ('in_progress', 'completed'),
                       'rewind': ('in_progress', 'completed'), 'autopick': ('in_progress',)}[self.action]
            if status not in allowed:
                await interaction.edit_original_response(content="❌ The draft has changed since - nothing was done.")
                return
            channel = await _draft_channel(self.bot, db)

            if self.action == "autopick":
                result = await _auto_pick(self.bot, db, self.draft_id, draft_name, self.pick_number)
            elif self.action == "end":
                await cog.complete_draft(db, self.draft_id, draft_name)
                result = f"✅ **{draft_name}** ended."
            elif self.action == "abandon":
                returned = await restore_draft_snapshot(db, self.draft_id, 1, abandon=True)
                await db.commit()
                if channel:
                    await channel.send(f"# {draft_name} abandoned\nThe draft will be restarted.")
                await refresh_draft_panel(self.bot, db, self.draft_id)
                result = (f"✅ **{draft_name}** abandoned - {returned} player(s) went back to the Draft Pool. "
                          "Start it again with **Start Draft**.")
            else:
                cursor = await db.execute(
                    "SELECT 1 FROM draft_snapshots WHERE draft_id = ? AND pick_number = ?",
                    (self.draft_id, self.pick_number)
                )
                if not await cursor.fetchone():
                    await interaction.edit_original_response(content="❌ That pick can't be rewound to any more.")
                    return
                returned = await restore_draft_snapshot(db, self.draft_id, self.pick_number)
                await db.commit()
                if channel:
                    await channel.send(f"⏪ **Draft rewound to pick {self.pick_number}** - picks from here on are being re-done.")
                # Re-posts pick N's notification and redraws the panel.
                await cog.send_pick_notification(db, self.draft_id, draft_name, self.pick_number)
                result = (f"✅ Rewound to pick {self.pick_number} - {returned} player(s) went back to the Draft Pool, "
                          "and the pick is back on the clock.")
        await interaction.edit_original_response(content=result)

    async def _cancel(self, interaction):
        self.stop()
        await interaction.response.edit_message(content="Cancelled - nothing was changed.", view=None)


def format_draft_pick_line(emoji_source, pick_number, pick_origin, team_emoji_id, player_selected):
    """One pick's line, in the format /draftorder uses:

        **12.** <:team:1234>*(Adelaide R1)* → **Player Name**

    Shared by DraftOrderView and /editdraft's own pick list so the two can't
    drift apart. `emoji_source` is whatever get_team_emoji_str resolves
    emojis against (a guild or the bot).
    """
    line = f"**{pick_number}.** {get_team_emoji_str(emoji_source, team_emoji_id)}"
    if pick_origin:
        line += f"*({pick_origin})*"
    if player_selected:
        line += f" → **{player_selected}**"
    return line


class DraftOrderView(discord.ui.View):
    def __init__(self, picks, draft_name, guild):
        super().__init__(timeout=180)
        self.picks = picks
        self.draft_name = draft_name
        self.guild = guild
        self.current_round = 1

        # Group picks by round
        self.picks_by_round = {}
        for pick in picks:
            round_num = pick[1]
            if round_num not in self.picks_by_round:
                self.picks_by_round[round_num] = []
            self.picks_by_round[round_num].append(pick)

        self.max_rounds = max(self.picks_by_round.keys()) if self.picks_by_round else 1
        self.update_buttons()

    def get_emoji(self, emoji_id):
        """Convert emoji_id to Discord emoji or return empty string"""
        return get_team_emoji_str(self.guild, emoji_id)

    def create_embed(self):
        embed = discord.Embed(
            title=f"{self.draft_name} - Round {self.current_round}",
            color=discord.Color.blue()
        )

        round_picks = self.picks_by_round.get(self.current_round, [])

        if not round_picks:
            embed.description = "No picks in this round"
            return embed

        embed.description = "\n".join(
            format_draft_pick_line(self.guild, pick_num, pick_origin, current_emoji, player_selected)
            for pick_num, round_num, pick_origin, current_team, current_emoji, player_selected
            in round_picks
        )
        embed.set_footer(text=f"Round {self.current_round} of {self.max_rounds}")

        return embed

    def update_buttons(self):
        self.clear_items()
        add_page_buttons(self, self.current_round - 1, self.max_rounds, self._go_to_round, count_label="Round")

    async def _go_to_round(self, interaction: discord.Interaction, page):
        self.current_round = page + 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)


class DraftPickView(discord.ui.View):
    """Interactive view for making draft picks"""
    def __init__(self, bot, draft_id, draft_name, team_id, pick_number):
        super().__init__(timeout=None)  # Persistent view
        self.bot = bot
        self.draft_id = draft_id
        self.draft_name = draft_name
        self.team_id = team_id
        self.pick_number = pick_number
        self.selected_player_id = None
        self.current_page = 0
        self.players_per_page = 25

    async def create_embed(self, db):
        """Create the embed for this draft pick"""
        # Get pick info
        cursor = await db.execute(
            """SELECT dp.round_number, dp.pick_number, t.team_name, t.emoji_id
               FROM draft_picks dp
               JOIN teams t ON dp.current_team_id = t.team_id
               WHERE dp.draft_id = ? AND dp.pick_number = ?""",
            (self.draft_id, self.pick_number)
        )
        pick_info = await cursor.fetchone()

        if not pick_info:
            return None

        round_number, pick_num, team_name, emoji_id = pick_info

        # Get emoji
        emoji_str = ""
        if emoji_id:
            emoji_str = get_team_emoji_str(self.bot, emoji_id)

        # Get ALL available players from Draft Pool
        cursor = await db.execute(
            """SELECT p.player_id, p.name, p.position, p.age, p.father_son_club_id, t_fs.team_name
               FROM players p
               JOIN teams t ON p.team_id = t.team_id
               LEFT JOIN teams t_fs ON p.father_son_club_id = t_fs.team_id
               WHERE t.team_name = 'Draft Pool'
               ORDER BY p.name"""
        )
        all_players = await cursor.fetchall()

        # Calculate pagination
        total_players = len(all_players)
        total_pages = (total_players + self.players_per_page - 1) // self.players_per_page if total_players > 0 else 1
        start_idx = self.current_page * self.players_per_page
        end_idx = min(start_idx + self.players_per_page, total_players)
        page_players = all_players[start_idx:end_idx]

        # Populate dropdown with current page
        options = []
        for player_id, name, pos, age, fs_club_id, fs_club_name in page_players:
            # Don't show OVR for draft pool players, add "yo" after age
            label = f"{name} ({pos}, {age} yo)"
            if fs_club_id:
                label += f" (F/S tied to {fs_club_name})"

            options.append(
                discord.SelectOption(
                    label=label,
                    value=str(player_id)
                )
            )

        if not options:
            options.append(discord.SelectOption(label="No players available", value="0", default=True))

        # Update the select menu with options
        for item in self.children:
            if isinstance(item, discord.ui.Select) and item.custom_id == "player_select":
                item.options = options
                item.disabled = len(all_players) == 0
                break

        # Standard page buttons, rebuilt for the current page.
        replace_page_buttons(self, self.current_page, total_pages, self._go_to_page, row=1)

        embed = discord.Embed(
            title=f":rotating_light: {emoji_str}{team_name} - On the Clock :rotating_light:",
            description=f"**{self.draft_name}**\nRound {round_number}, Pick {pick_num}",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="Available Players",
            value=f"{total_players} players in draft pool (Page {self.current_page + 1}/{total_pages})",
            inline=False
        )

        embed.set_footer(text="Select a player from the dropdown, then click Confirm Selection")

        return embed

    @discord.ui.select(placeholder="Select a player to draft...", min_values=0, max_values=1, custom_id="player_select", row=0)
    async def player_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        """Handle player selection"""
        if select.values:
            self.selected_player_id = int(select.values[0])
        else:
            self.selected_player_id = None

        await interaction.response.defer()

    @discord.ui.button(label="Confirm Selection", style=discord.ButtonStyle.primary, row=2)
    async def confirm_pick(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Confirm the draft pick"""
        if not self.selected_player_id:
            await interaction.response.send_message("❌ Please select a player first!", ephemeral=True)
            return
        async with aiosqlite.connect(DB_PATH) as db:
            problem = await draft_pick_problem(db, self.draft_id, self.pick_number, self.team_id)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return

        await interaction.response.defer()

        # Disable all buttons immediately to prevent double-clicks
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Check if player is a father/son player
                cursor = await db.execute(
                    "SELECT father_son_club_id FROM players WHERE player_id = ?",
                    (self.selected_player_id,)
                )
                result = await cursor.fetchone()
                father_son_club_id = result[0] if result and result[0] else None

                # If player is a father/son player and this team is NOT the tied club
                if father_son_club_id and father_son_club_id != self.team_id:
                    # This is a bid on a father/son player
                    await self.process_father_son_bid(db, self.selected_player_id, father_son_club_id)
                    await interaction.followup.send("✅ Bid placed on father/son player!", ephemeral=True)
                    await interaction.message.edit(view=None)  # Remove buttons
                else:
                    # Normal pick
                    await self.process_pick(db, self.selected_player_id, False)
                    await interaction.followup.send("✅ Pick confirmed!", ephemeral=True)
                    await interaction.message.edit(view=None)  # Remove buttons

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @discord.ui.button(label="Pass Pick", style=discord.ButtonStyle.secondary, row=2)
    async def pass_pick(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Pass on this pick"""
        async with aiosqlite.connect(DB_PATH) as db:
            problem = await draft_pick_problem(db, self.draft_id, self.pick_number, self.team_id)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        await interaction.response.defer()

        # Disable all buttons immediately to prevent double-clicks
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Process as a pass
                await self.process_pick(db, None, True)

                # Update the message
                await interaction.followup.send("✅ Pick passed!", ephemeral=True)
                await interaction.message.edit(view=None)  # Remove buttons

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    async def _go_to_page(self, interaction: discord.Interaction, page):
        self.current_page = page
        async with aiosqlite.connect(DB_PATH) as db:
            embed = await self.create_embed(db)
        await interaction.response.edit_message(embed=embed, view=self)

    async def process_pick(self, db, player_id, is_pass):
        """Process the draft pick or pass"""
        # Update the pick in database
        if is_pass:
            await db.execute(
                """UPDATE draft_picks
                   SET passed = 1, picked_at = CURRENT_TIMESTAMP
                   WHERE draft_id = ? AND pick_number = ?""",
                (self.draft_id, self.pick_number)
            )
        else:
            # Get rookie contract years
            cursor = await db.execute(
                "SELECT rookie_contract_years, season_number FROM drafts WHERE draft_id = ?",
                (self.draft_id,)
            )
            rookie_years, season_number = await cursor.fetchone()

            # Update pick
            await db.execute(
                """UPDATE draft_picks
                   SET player_selected_id = ?, picked_at = CURRENT_TIMESTAMP
                   WHERE draft_id = ? AND pick_number = ?""",
                (player_id, self.draft_id, self.pick_number)
            )

            # Assign player to team
            await assign_drafted_player(db, self.team_id, player_id, season_number, rookie_years)

        await db.commit()

        # Trade offers including this pick can't go ahead now it's used.
        cursor = await db.execute(
            "SELECT pick_id FROM draft_picks WHERE draft_id = ? AND pick_number = ?",
            (self.draft_id, self.pick_number)
        )
        used = await cursor.fetchone()
        if used:
            await void_trades_for_picks(self.bot, [used[0]])

        # Post to draft channel
        await self.post_to_draft_channel(db, player_id, is_pass)

        # Advance to next pick
        cursor = await db.execute(
            "SELECT current_pick_number FROM drafts WHERE draft_id = ?",
            (self.draft_id,)
        )
        current_pick = (await cursor.fetchone())[0]
        next_pick = current_pick + 1

        await db.execute(
            "UPDATE drafts SET current_pick_number = ? WHERE draft_id = ?",
            (next_pick, self.draft_id)
        )
        await db.commit()

        # Send next pick notification
        draft_commands = self.bot.get_cog('DraftCommands')
        await draft_commands.send_pick_notification(db, self.draft_id, self.draft_name, next_pick)

    async def process_father_son_bid(self, db, player_id, father_son_club_id):
        """Process a bid on a father/son player"""
        # Get bid pick value (80% discount for matching)
        cursor = await db.execute(
            "SELECT points_value FROM draft_value_index WHERE pick_number = ?",
            (self.pick_number,)
        )
        result = await cursor.fetchone()
        bid_value = result[0] if result else 0
        required_value = int(bid_value * 0.8)  # 20% discount

        # Get player info
        cursor = await db.execute(
            "SELECT name, position, age, overall_rating FROM players WHERE player_id = ?",
            (player_id,)
        )
        player_name, pos, age, ovr = await cursor.fetchone()

        # Get bidding team info
        cursor = await db.execute(
            "SELECT team_name, emoji_id FROM teams WHERE team_id = ?",
            (self.team_id,)
        )
        bidding_team_name, bidding_emoji_id = await cursor.fetchone()

        # Get father/son club info
        cursor = await db.execute(
            "SELECT team_name, emoji_id, channel_id FROM teams WHERE team_id = ?",
            (father_son_club_id,)
        )
        fs_team_name, fs_emoji_id, fs_channel_id = await cursor.fetchone()

        # Post bid to draft channel
        await self.post_father_son_bid_to_draft_channel(
            db, player_id, player_name, pos, age, ovr,
            bidding_team_name, bidding_emoji_id, fs_team_name, fs_emoji_id
        )

        # If bid value is 0, automatically treat as unable to match
        if bid_value == 0:
            await self.auto_pass_father_son_bid(
                db, player_id, player_name, pos, age, ovr,
                father_son_club_id, fs_team_name, fs_emoji_id,
                bidding_team_name, bidding_emoji_id,
                has_picks=False
            )
            return

        # Calculate which picks the father/son club needs to match
        matching_picks = await self.calculate_matching_picks(db, father_son_club_id, required_value)

        # Send match notification to father/son club (always, even if they can't match)
        if fs_channel_id:
            fs_channel = self.bot.get_channel(int(fs_channel_id))
            if fs_channel:
                # Calculate total match value to determine if they can match
                total_match_value = sum(p[3] for p in matching_picks)
                can_match = matching_picks and total_match_value >= required_value

                # Create match notification view
                match_view = FatherSonMatchView(
                    self.bot, self.draft_id, self.draft_name, self.pick_number,
                    player_id, player_name, pos, age, ovr,
                    father_son_club_id, fs_team_name,
                    self.team_id, bidding_team_name,
                    bid_value, required_value, matching_picks,
                    can_match=can_match
                )

                # Create embed
                embed = await match_view.create_embed(db)
                await fs_channel.send(embed=embed, view=match_view)

                # If they can't match, automatically pass after sending notification
                if not can_match:
                    await self.auto_pass_father_son_bid(
                        db, player_id, player_name, pos, age, ovr,
                        father_son_club_id, fs_team_name, fs_emoji_id,
                        bidding_team_name, bidding_emoji_id,
                        has_picks=(len(matching_picks) > 0)
                    )

    async def calculate_matching_picks(self, db, team_id, required_value):
        """Calculate which picks are needed to match the bid (earliest picks, minimum value)"""
        # Get all picks for this team after the current pick
        cursor = await db.execute(
            """SELECT pick_number, round_number, pick_origin
               FROM draft_picks
               WHERE draft_id = ? AND current_team_id = ? AND pick_number > ?
               AND player_selected_id IS NULL AND passed = 0
               ORDER BY pick_number ASC""",
            (self.draft_id, team_id, self.pick_number)
        )
        available_picks = await cursor.fetchall()

        # Get point values for each pick
        picks_with_values = []
        for pick_number, round_number, pick_origin in available_picks:
            cursor = await db.execute(
                "SELECT points_value FROM draft_value_index WHERE pick_number = ?",
                (pick_number,)
            )
            result = await cursor.fetchone()
            points_value = result[0] if result else 0
            picks_with_values.append((pick_number, round_number, pick_origin, points_value))

        # Use earliest picks until we reach required value
        matching_picks = []
        total_value = 0
        for pick_number, round_number, pick_origin, points_value in picks_with_values:
            matching_picks.append((pick_number, round_number, pick_origin, points_value))
            total_value += points_value
            if total_value >= required_value:
                break

        return matching_picks

    async def post_father_son_bid_to_draft_channel(self, db, player_id, player_name, pos, age, ovr,
                                                     bidding_team_name, bidding_emoji_id,
                                                     fs_team_name, fs_emoji_id):
        """Post father/son bid to draft channel"""
        try:
            # Get draft channel
            cursor = await db.execute(
                "SELECT setting_value FROM settings WHERE setting_key = 'draft_channel_id'"
            )
            result = await cursor.fetchone()
            if not result or not result[0]:
                return

            draft_channel = self.bot.get_channel(int(result[0]))
            if not draft_channel:
                return

            # No round header here - this pick's "on the clock" post
            # (send_pick_notification) already put one up if it opens a
            # round, so repeating it doubled the header whenever a round's
            # first pick was a father-son bid.

            # Get emojis
            bidding_emoji_str = ""
            if bidding_emoji_id:
                bidding_emoji_str = get_team_emoji_str(self.bot, bidding_emoji_id)

            fs_emoji_str = ""
            if fs_emoji_id:
                fs_emoji_str = get_team_emoji_str(self.bot, fs_emoji_id)

            message = f"{bidding_emoji_str}Bid pending..."
            await draft_channel.send(message)

        except Exception as e:
            print(f"Error posting F/S bid to draft channel: {e}")

    async def auto_pass_father_son_bid(self, db, player_id, player_name, pos, age, ovr,
                                        fs_team_id, fs_team_name, fs_emoji_id,
                                        bidding_team_name, bidding_emoji_id, has_picks):
        """Automatically pass on F/S bid when club doesn't have enough points"""
        # Get rookie contract years
        cursor = await db.execute(
            "SELECT rookie_contract_years, season_number FROM drafts WHERE draft_id = ?",
            (self.draft_id,)
        )
        rookie_years, season_number = await cursor.fetchone()

        # Update the bid pick with the player
        await db.execute(
            """UPDATE draft_picks
               SET player_selected_id = ?, picked_at = CURRENT_TIMESTAMP
               WHERE draft_id = ? AND pick_number = ?""",
            (player_id, self.draft_id, self.pick_number)
        )

        # Assign player to bidding team
        await assign_drafted_player(db, self.team_id, player_id, season_number, rookie_years)

        await db.commit()

        # Post auto-pass result to draft channel
        try:
            cursor = await db.execute(
                "SELECT setting_value FROM settings WHERE setting_key = 'draft_channel_id'"
            )
            result = await cursor.fetchone()
            if result and result[0]:
                draft_channel = self.bot.get_channel(int(result[0]))
                if draft_channel:
                    # Get emojis
                    bidding_emoji_str = ""
                    if bidding_emoji_id:
                        bidding_emoji_str = get_team_emoji_str(self.bot, bidding_emoji_id)

                    fs_emoji_str = ""
                    if fs_emoji_id:
                        fs_emoji_str = get_team_emoji_str(self.bot, fs_emoji_id)

                    # Get plays_like info
                    cursor = await db.execute(
                        "SELECT plays_like FROM players WHERE player_id = ?",
                        (player_id,)
                    )
                    plays_like_result = await cursor.fetchone()
                    plays_like = plays_like_result[0] if plays_like_result and plays_like_result[0] else None

                    # Build main message line
                    message = f"**Pick {self.pick_number}:** {bidding_emoji_str}select **{player_name.upper()}** ({pos}, {age} yo, {ovr} OVR)"

                    # Add plays like on main line
                    if plays_like:
                        message += f" - Plays like *{plays_like}*"

                    # Add matching status on new line
                    message += f"\n└ {fs_emoji_str}Unable to match"

                    await draft_channel.send(message)
        except Exception as e:
            print(f"Error posting auto-pass result to draft channel: {e}")

        # Continue with next pick
        cursor = await db.execute(
            "SELECT current_pick_number FROM drafts WHERE draft_id = ?",
            (self.draft_id,)
        )
        current_pick = (await cursor.fetchone())[0]
        next_pick = current_pick + 1

        await db.execute(
            "UPDATE drafts SET current_pick_number = ? WHERE draft_id = ?",
            (next_pick, self.draft_id)
        )
        await db.commit()

        # Send next pick notification
        draft_commands = self.bot.get_cog('DraftCommands')
        await draft_commands.send_pick_notification(db, self.draft_id, self.draft_name, next_pick)

    async def post_to_draft_channel(self, db, player_id, is_pass):
        """Post pick result to draft channel"""
        try:
            # Get draft channel
            cursor = await db.execute(
                "SELECT setting_value FROM settings WHERE setting_key = 'draft_channel_id'"
            )
            result = await cursor.fetchone()
            if not result or not result[0]:
                return

            draft_channel = self.bot.get_channel(int(result[0]))
            if not draft_channel:
                return

            # Get pick info
            cursor = await db.execute(
                """SELECT dp.round_number, dp.pick_number, t.team_name, t.emoji_id
                   FROM draft_picks dp
                   JOIN teams t ON dp.current_team_id = t.team_id
                   WHERE dp.draft_id = ? AND dp.pick_number = ?""",
                (self.draft_id, self.pick_number)
            )
            round_num, pick_num, team_name, emoji_id = await cursor.fetchone()

            # Get emoji
            emoji_str = ""
            if emoji_id:
                emoji_str = get_team_emoji_str(self.bot, emoji_id)

            # Check if this is the first pick of a new round (post round header)
            if is_pass:
                message = f"**Pick {pick_num}:** {emoji_str}- PASS"
            else:
                # Get player info
                cursor = await db.execute(
                    "SELECT name, position, age, overall_rating, plays_like, father_son_club_id FROM players WHERE player_id = ?",
                    (player_id,)
                )
                player_data = await cursor.fetchone()
                player_name, pos, age, ovr, plays_like, father_son_club_id = player_data

                message = f"**Pick {pick_num}:** {emoji_str}select **{player_name.upper()}** ({pos}, {age} yo, {ovr} OVR)"

                # Add plays like info on main line
                if plays_like:
                    message += f" - Plays like *{plays_like}*"

            await draft_channel.send(message)

        except Exception as e:
            print(f"Error posting to draft channel: {e}")


class FatherSonMatchView(discord.ui.View):
    """View for father/son club to match or pass on a bid"""
    def __init__(self, bot, draft_id, draft_name, bid_pick_number, player_id, player_name, pos, age, ovr,
                 fs_team_id, fs_team_name, bidding_team_id, bidding_team_name,
                 bid_value, required_value, matching_picks, can_match=True):
        super().__init__(timeout=None)  # No timeout for important decisions
        self.bot = bot
        self.draft_id = draft_id
        self.draft_name = draft_name
        self.bid_pick_number = bid_pick_number
        self.player_id = player_id
        self.player_name = player_name
        self.pos = pos
        self.age = age
        self.ovr = ovr
        self.fs_team_id = fs_team_id
        self.fs_team_name = fs_team_name
        self.bidding_team_id = bidding_team_id
        self.bidding_team_name = bidding_team_name
        self.bid_value = bid_value
        self.required_value = required_value
        self.matching_picks = matching_picks
        self.can_match = can_match

    async def create_embed(self, db):
        """Create the match notification embed"""
        embed = discord.Embed(
            title=f"⚠️ Father/Son Bid - {self.player_name}",
            description=f"**{self.bidding_team_name}** has bid on your father/son player with pick **#{self.bid_pick_number}**",
            color=discord.Color.orange()
        )

        # Combine player, bid value, and required to match into one field to reduce gaps
        info_text = f"Player: **{self.player_name}** ({self.pos}, {self.age} yo)\n"
        info_text += f"Bid Value: **{self.bid_value} points** (Pick #{self.bid_pick_number})\n"
        info_text += f"Required to Match: **{self.required_value} points** (20% discount)"

        embed.add_field(
            name="\u200b",
            value=info_text,
            inline=False
        )

        # Check if team can match the bid
        if not self.can_match:
            # Show insufficient points message instead of picks
            embed.add_field(
                name="\n\nPicks Needed to Match",
                value="Insufficient points to match, player has been drafted by the bidding team",
                inline=False
            )
            # Disable and grey out both buttons
            for item in self.children:
                if isinstance(item, discord.ui.Button):
                    item.disabled = True
                    item.style = discord.ButtonStyle.gray
        else:
            # Calculate total value of matching picks
            total_match_value = sum(p[3] for p in self.matching_picks)
            excess_points = total_match_value - self.required_value

            # Show which picks are needed to match
            picks_text = ""
            for pick_num, round_num, origin, points in self.matching_picks:
                picks_text += f"• Pick #{pick_num} (**{points} pts**)\n"
            picks_text += f"\n**Total: {total_match_value} points**"

            embed.add_field(
                name="\n\nPicks Needed to Match",
                value=picks_text,
                inline=False
            )

            # Show compensation pick if there's excess points
            if excess_points > 0:
                # Find the compensation pick value
                cursor = await db.execute(
                    """SELECT pick_number, points_value FROM draft_value_index
                       WHERE points_value <= ?
                       ORDER BY points_value DESC
                       LIMIT 1""",
                    (excess_points,)
                )
                comp_result = await cursor.fetchone()

                if comp_result:
                    comp_pick_num, comp_pick_value = comp_result
                    embed.add_field(
                        name="\n\nCompensation Pick",
                        value=f"You will receive Pick {comp_pick_num} as compensation due to {excess_points} excess points.",
                        inline=False
                    )

        return embed

    @discord.ui.button(label="Match Bid", style=discord.ButtonStyle.success, custom_id="fs_match")
    async def match_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Match the bid and draft the player"""
        async with aiosqlite.connect(DB_PATH) as db:
            problem = await draft_pick_problem(db, self.draft_id, self.bid_pick_number)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        await interaction.response.defer()

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Calculate total value
                total_match_value = sum(p[3] for p in self.matching_picks)

                # Verify they have enough points
                if total_match_value < self.required_value:
                    await interaction.followup.send("❌ Insufficient draft points to match!", ephemeral=True)
                    return

                # Process the match
                await self.process_match(db, interaction)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @discord.ui.button(label="Pass on Bid", style=discord.ButtonStyle.danger, custom_id="fs_pass")
    async def pass_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Pass on the bid - let bidding team select the player"""
        async with aiosqlite.connect(DB_PATH) as db:
            problem = await draft_pick_problem(db, self.draft_id, self.bid_pick_number)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        await interaction.response.defer()

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Process the pass
                await self.process_pass(db, interaction)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    async def process_match(self, db, interaction):
        """Father/son club matched the bid - consume picks and draft player"""
        # Get rookie contract years and draft info
        cursor = await db.execute(
            "SELECT rookie_contract_years, season_number, draft_name FROM drafts WHERE draft_id = ?",
            (self.draft_id,)
        )
        rookie_years, season_number, draft_name = await cursor.fetchone()

        # Get the round number for the bid pick
        cursor = await db.execute(
            "SELECT round_number FROM draft_picks WHERE draft_id = ? AND pick_number = ?",
            (self.draft_id, self.bid_pick_number)
        )
        round_number = (await cursor.fetchone())[0]

        # Calculate excess points for compensation pick
        total_match_value = sum(p[3] for p in self.matching_picks)
        excess_points = total_match_value - self.required_value

        # The matching picks' ids, so trade offers including them can be
        # voided once they're spent (below).
        spent_pick_ids = []
        for pick_num, _, _, _ in self.matching_picks:
            cursor = await db.execute(
                "SELECT pick_id FROM draft_picks WHERE draft_id = ? AND pick_number = ?", (self.draft_id, pick_num)
            )
            row = await cursor.fetchone()
            if row:
                spent_pick_ids.append(row[0])

        # Step 1: Delete the consumed matching picks (these are the F/S club's picks used to match)
        for pick_num, _, _, _ in self.matching_picks:
            await db.execute(
                "DELETE FROM draft_picks WHERE draft_id = ? AND pick_number = ?",
                (self.draft_id, pick_num)
            )

        # Step 2: Get all remaining picks and renumber them sequentially,
        # but insert the F/S pick at the bid position
        cursor = await db.execute(
            """SELECT pick_id, pick_number FROM draft_picks
               WHERE draft_id = ?
               ORDER BY pick_number ASC""",
            (self.draft_id,)
        )
        all_picks = await cursor.fetchall()

        # Renumber picks: everything before bid stays same, bid position onwards shifts by 1
        new_pick_number = 1
        for pick_id, old_pick_number in all_picks:
            if new_pick_number == self.bid_pick_number:
                # Skip this number - it will be used for the F/S pick
                new_pick_number += 1

            if new_pick_number != old_pick_number:
                await db.execute(
                    "UPDATE draft_picks SET pick_number = ? WHERE pick_id = ?",
                    (new_pick_number, pick_id)
                )
            new_pick_number += 1

        # Step 3: Insert new pick for father/son club at the bid position
        await db.execute(
            """INSERT INTO draft_picks (
                draft_id, draft_name, season_number, round_number, pick_number,
                pick_origin, original_team_id, current_team_id, player_selected_id, picked_at,
                pick_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 'father_son')""",
            (self.draft_id, draft_name, season_number, round_number, self.bid_pick_number,
             f"{self.fs_team_name} F/S Match", self.fs_team_id, self.fs_team_id, self.player_id)
        )

        # Step 4: Add compensation pick for excess points (if any)
        if excess_points > 0:
            # Find the pick number that has the highest value that doesn't exceed excess points
            cursor = await db.execute(
                """SELECT pick_number, points_value FROM draft_value_index
                   WHERE points_value <= ?
                   ORDER BY points_value DESC
                   LIMIT 1""",
                (excess_points,)
            )
            result = await cursor.fetchone()

            if result:
                comp_pick_equivalent = result[0]
                comp_pick_value = result[1]

                # The compensation pick should be inserted at the position matching its value
                comp_pick_number = comp_pick_equivalent

                # Get the round number from the pick at the equivalent position
                cursor = await db.execute(
                    """SELECT round_number FROM draft_picks
                       WHERE draft_id = ? AND pick_number = ?""",
                    (self.draft_id, comp_pick_equivalent)
                )
                round_result = await cursor.fetchone()
                if round_result:
                    comp_round_number = round_result[0]
                else:
                    # Fallback: calculate based on number of teams if the exact pick doesn't exist
                    cursor = await db.execute("SELECT COUNT(*) FROM teams WHERE team_name != 'Draft Pool'")
                    num_teams = (await cursor.fetchone())[0]
                    comp_round_number = ((comp_pick_equivalent - 1) // num_teams) + 1

                # Renumber all picks at or after this position to make room
                cursor = await db.execute(
                    """SELECT pick_id, pick_number FROM draft_picks
                       WHERE draft_id = ? AND pick_number >= ?
                       ORDER BY pick_number DESC""",
                    (self.draft_id, comp_pick_number)
                )
                picks_to_renumber = await cursor.fetchall()

                for pick_id, old_pick_number in picks_to_renumber:
                    await db.execute(
                        "UPDATE draft_picks SET pick_number = ? WHERE pick_id = ?",
                        (old_pick_number + 1, pick_id)
                    )

                # Insert compensation pick at the correct position
                await db.execute(
                    """INSERT INTO draft_picks (
                        draft_id, draft_name, season_number, round_number, pick_number,
                        pick_origin, original_team_id, current_team_id, player_selected_id, picked_at,
                        pick_type
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'father_son')""",
                    (self.draft_id, draft_name, season_number, comp_round_number, comp_pick_number,
                     f"{self.fs_team_name} F/S Comp", self.fs_team_id, self.fs_team_id, None)
                )

        # Step 5: Assign player to father/son club
        await assign_drafted_player(db, self.fs_team_id, self.player_id, season_number, rookie_years)

        await db.commit()
        await void_trades_for_picks(self.bot, spent_pick_ids)

        # Post match result to draft channel
        await self.post_match_result_to_draft_channel(db, matched=True)

        # Disable buttons and update message
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(content="✅ **Bid Matched!** You have drafted this player.", view=self)

        # Continue with next pick (bidding team picks again with their pushed-back pick)
        await self.continue_draft(db)

    async def process_pass(self, db, interaction):
        """Father/son club passed - bidding team gets the player"""
        # Get rookie contract years
        cursor = await db.execute(
            "SELECT rookie_contract_years, season_number FROM drafts WHERE draft_id = ?",
            (self.draft_id,)
        )
        rookie_years, season_number = await cursor.fetchone()

        # Update the bid pick with the player
        await db.execute(
            """UPDATE draft_picks
               SET player_selected_id = ?, picked_at = CURRENT_TIMESTAMP
               WHERE draft_id = ? AND pick_number = ?""",
            (self.player_id, self.draft_id, self.bid_pick_number)
        )

        # Assign player to bidding team
        await assign_drafted_player(db, self.bidding_team_id, self.player_id, season_number, rookie_years)

        await db.commit()

        cursor = await db.execute(
            "SELECT pick_id FROM draft_picks WHERE draft_id = ? AND pick_number = ?",
            (self.draft_id, self.bid_pick_number)
        )
        used = await cursor.fetchone()
        if used:
            await void_trades_for_picks(self.bot, [used[0]])

        # Post pass result to draft channel
        await self.post_match_result_to_draft_channel(db, matched=False)

        # Disable buttons and update message
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(content="❌ **Bid Passed** - Bidding team selects the player.", view=self)

        # Continue with next pick
        await self.continue_draft(db)

    async def post_match_result_to_draft_channel(self, db, matched):
        """Post the match/pass result to draft channel"""
        try:
            # Get draft channel
            cursor = await db.execute(
                "SELECT setting_value FROM settings WHERE setting_key = 'draft_channel_id'"
            )
            result = await cursor.fetchone()
            if not result or not result[0]:
                return

            draft_channel = self.bot.get_channel(int(result[0]))
            if not draft_channel:
                return

            # Get emojis
            cursor = await db.execute(
                "SELECT emoji_id FROM teams WHERE team_id = ?",
                (self.bidding_team_id,)
            )
            bidding_result = await cursor.fetchone()
            bidding_emoji_id = bidding_result[0] if bidding_result else None
            bidding_emoji_str = ""
            if bidding_emoji_id:
                bidding_emoji_str = get_team_emoji_str(self.bot, bidding_emoji_id)

            cursor = await db.execute(
                "SELECT emoji_id FROM teams WHERE team_id = ?",
                (self.fs_team_id,)
            )
            fs_result = await cursor.fetchone()
            fs_emoji_id = fs_result[0] if fs_result else None
            fs_emoji_str = ""
            if fs_emoji_id:
                fs_emoji_str = get_team_emoji_str(self.bot, fs_emoji_id)

            # Get plays_like info
            cursor = await db.execute(
                "SELECT plays_like FROM players WHERE player_id = ?",
                (self.player_id,)
            )
            plays_like_result = await cursor.fetchone()
            plays_like = plays_like_result[0] if plays_like_result and plays_like_result[0] else None

            # Build main message line
            # If matched, F/S team selects. If not matched, bidding team selects.
            selecting_emoji = fs_emoji_str if matched else bidding_emoji_str
            message = f"**Pick {self.bid_pick_number}:** {selecting_emoji}select **{self.player_name.upper()}** ({self.pos}, {self.age} yo, {self.ovr} OVR)"

            # Add plays like on main line
            if plays_like:
                message += f" - Plays like *{plays_like}*"

            # Add matching status on new line
            if matched:
                # Show picks consumed
                picks_list = [str(p[0]) for p in self.matching_picks]
                if len(picks_list) > 1:
                    picks_text = ", ".join(picks_list[:-1]) + " & " + picks_list[-1]
                else:
                    picks_text = picks_list[0] if picks_list else ""
                message += f"\n└ {fs_emoji_str}Matched using pick/s {picks_text}"
            else:
                message += f"\n└ {fs_emoji_str}Elected not to match"

            await draft_channel.send(message)

        except Exception as e:
            print(f"Error posting F/S match result to draft channel: {e}")

    async def continue_draft(self, db):
        """Continue the draft with the next pick"""
        # Increment current pick number
        cursor = await db.execute(
            "SELECT current_pick_number FROM drafts WHERE draft_id = ?",
            (self.draft_id,)
        )
        current_pick = (await cursor.fetchone())[0]
        next_pick = current_pick + 1

        await db.execute(
            "UPDATE drafts SET current_pick_number = ? WHERE draft_id = ?",
            (next_pick, self.draft_id)
        )
        await db.commit()

        # Send next pick notification
        draft_commands = self.bot.get_cog('DraftCommands')
        await draft_commands.send_pick_notification(db, self.draft_id, self.draft_name, next_pick)


class DraftPointsCalculatorView(discord.ui.View):
    """Interactive view for calculating draft points and matching bids"""
    def __init__(self, bot, draft_name, all_picks, guild):
        super().__init__(timeout=300)
        self.bot = bot
        self.draft_name = draft_name
        self.all_picks = all_picks  # List of (pick_id, pick_number, emoji_id, team_name, points_value)
        self.guild = guild
        self.selected_picks = []  # List of pick_ids
        self.current_page = 0
        self.picks_per_page = 25

    def get_emoji(self, emoji_id):
        """Convert emoji_id to Discord emoji or return empty string"""
        return get_team_emoji_str(self.guild, emoji_id)

    def create_embed(self):
        """Create the calculator embed"""
        embed = discord.Embed(
            title="Draft Points Calculator",
            description="Select draft picks from the dropdown to calculate the highest bid you can match.",
            color=discord.Color.blue()
        )

        # Calculate total points from selected picks
        total_points = 0
        if self.selected_picks:
            for pick_id in self.selected_picks:
                pick = next((p for p in self.all_picks if p[0] == pick_id), None)
                if pick:
                    total_points += pick[4]  # points_value

        # Show selected picks
        if self.selected_picks:
            selected_text = ""
            for pick_id in self.selected_picks:
                pick = next((p for p in self.all_picks if p[0] == pick_id), None)
                if pick:
                    _, pick_number, emoji_id, _, points_value = pick
                    emoji_str = self.get_emoji(emoji_id) if emoji_id else ""
                    selected_text += f"{emoji_str}Pick #{pick_number} - **{points_value} pts**\n"

            embed.add_field(
                name="Selected Picks",
                value=selected_text,
                inline=False
            )

            # Find the earliest pick whose required matching points (points_value * 0.8) does not exceed total_points
            # Search from pick 1 down until we find a pick we CAN match
            max_matchable_pick = None
            for _, pick_number, _, _, points_value in self.all_picks:
                required_to_match = int(points_value * 0.8)
                if required_to_match <= total_points:
                    # Found the first pick we can match - this is the highest value pick we can match
                    max_matchable_pick = pick_number
                    break

            embed.add_field(
                name="Total Points",
                value=f"**{total_points} points**",
                inline=False
            )

            if max_matchable_pick:
                embed.add_field(
                    name="\n✅ Maximum Bid Match",
                    value=f"These picks are enough to match a bid as high as **Pick #{max_matchable_pick}**!",
                    inline=False
                )
            else:
                embed.add_field(
                    name="\n❌ No Match Available",
                    value="These picks do not have enough value to match any bid.",
                    inline=False
                )
        else:
            embed.add_field(
                name="No Picks Selected",
                value="Use the dropdown below to select picks and calculate match potential.",
                inline=False
            )

        embed.set_footer(text="Remember the 20% matching discount - only 80% of the bid value needs to be met for a successful match!")

        return embed

    @discord.ui.select(placeholder="Select picks to add to calculator...", min_values=0, max_values=25, row=0)
    async def pick_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        """Handle pick selection"""
        # Get picks from current page
        start_idx = self.current_page * self.picks_per_page
        end_idx = min(start_idx + self.picks_per_page, len(self.all_picks))
        current_page_pick_ids = [p[0] for p in self.all_picks[start_idx:end_idx]]

        # Remove picks from current page from selected list
        self.selected_picks = [pid for pid in self.selected_picks if pid not in current_page_pick_ids]

        # Add newly selected picks from current page
        new_selections = [int(val.replace("pick_", "")) for val in select.values]
        self.selected_picks.extend(new_selections)

        # Update the dropdown to show correct selections
        self.update_dropdown()

        # Update the embed
        embed = self.create_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _go_to_page(self, interaction: discord.Interaction, page):
        self.current_page = page
        self.update_dropdown()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(label="Clear Selection", style=discord.ButtonStyle.danger, row=1)
    async def clear_selection(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Clear all selected picks"""
        self.selected_picks = []
        self.update_dropdown()
        embed = self.create_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    def update_dropdown(self):
        """Update the dropdown options based on current page"""
        start_idx = self.current_page * self.picks_per_page
        end_idx = min(start_idx + self.picks_per_page, len(self.all_picks))
        page_picks = self.all_picks[start_idx:end_idx]

        options = []
        for pick_id, pick_number, emoji_id, team_name, points_value in page_picks:
            # Format: "Pick #X - Z pts"
            label = f"Pick #{pick_number} - {points_value} pts"

            # Get emoji object for the SelectOption emoji parameter
            pick_emoji = None
            if emoji_id:
                pick_emoji = get_team_emoji(self.guild, emoji_id)

            options.append(
                discord.SelectOption(
                    label=label,
                    value=f"pick_{pick_id}",
                    emoji=pick_emoji,
                    default=(pick_id in self.selected_picks)
                )
            )

        # Update the select menu - max_values must never exceed the number
        # of options Discord actually has to offer (it defaults to a fixed
        # 25, matching a full page, but the last page can hold fewer than
        # that - e.g. 54 picks across pages of 25 leaves only 4 on page 3,
        # and Discord rejects a Select whose max_values exceeds its option
        # count with a "components.0.components.0.options: Must be 25 or
        # more in length" error).
        for item in self.children:
            if isinstance(item, discord.ui.Select):
                item.options = options
                item.max_values = max(1, len(options))
                break

        # Standard page buttons, rebuilt for the current page - with Clear
        # Selection kept after them on the same row.
        total_pages = (len(self.all_picks) + self.picks_per_page - 1) // self.picks_per_page
        replace_page_buttons(self, self.current_page, total_pages, self._go_to_page, row=1)
        clear = next((item for item in self.children if getattr(item, "label", None) == "Clear Selection"), None)
        if clear is not None:
            self.remove_item(clear)
            self.add_item(clear)


class DraftPoolView(discord.ui.View):
    """Paginated list of the Draft Pool for /viewdraftpool.

    Purely in-memory paging over a list fetched once at command time (the
    pool only changes when an admin edits it or a draft runs), matching
    PlayerSearchResultsView's shape rather than re-querying per page.
    """

    PLAYERS_PER_PAGE = 20

    def __init__(self, players, position_filter=None):
        super().__init__(timeout=300)
        self.players = players            # (name, position, age, father_son_club_name)
        self.position_filter = position_filter
        self.current_page = 0
        self.update_components()

    @property
    def total_pages(self):
        if not self.players:
            return 1
        return (len(self.players) + self.PLAYERS_PER_PAGE - 1) // self.PLAYERS_PER_PAGE

    def page_players(self):
        start = self.current_page * self.PLAYERS_PER_PAGE
        return self.players[start:start + self.PLAYERS_PER_PAGE]

    def update_components(self):
        self.clear_items()
        add_page_buttons(self, self.current_page, self.total_pages, self._go_to_page)

    def create_embed(self):
        lines = []
        for name, position, age, fs_club_name in self.page_players():
            # No OVR - pool ratings stay hidden until a player is drafted.
            line = f"**{name}** - {position}, {age}yo"
            if fs_club_name:
                line += f" (F/S: {fs_club_name})"
            lines.append(line)

        title = "Draft Pool"
        if self.position_filter:
            title += f" - {self.position_filter}"
        title += f" ({len(self.players)})"

        embed = discord.Embed(
            title=title,
            description="\n".join(lines),
            color=discord.Color.teal(),
        )

        footer = "Ratings are hidden until players are drafted"
        if self.total_pages > 1:
            start = self.current_page * self.PLAYERS_PER_PAGE
            end = min(start + self.PLAYERS_PER_PAGE, len(self.players))
            footer = (f"Page {self.current_page + 1}/{self.total_pages} - "
                      f"showing {start + 1}-{end} of {len(self.players)} • " + footer)
        embed.set_footer(text=footer)
        return embed

    async def _go_to_page(self, interaction: discord.Interaction, page):
        self.current_page = page
        self.update_components()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)


async def _draft_round_order(db, draft_id):
    """The team order a new round should follow: this draft's own round 1
    order, so an added round mirrors the ladder order the draft was built
    from rather than an alphabetical or arbitrary one.

    Uses original_team_id (who the pick was created FOR), not
    current_team_id, so past trades don't reshuffle a brand new round.
    Falls back to the earliest round present if there's no round 1.
    """
    cursor = await db.execute(
        "SELECT MIN(round_number) FROM draft_picks WHERE draft_id = ?", (draft_id,)
    )
    row = await cursor.fetchone()
    base_round = row[0] if row and row[0] is not None else None
    if base_round is None:
        return []

    cursor = await db.execute(
        """SELECT original_team_id FROM draft_picks
           WHERE draft_id = ? AND round_number = ? AND original_team_id IS NOT NULL
           ORDER BY pick_number""",
        (draft_id, base_round)
    )
    order = []
    for (team_id,) in await cursor.fetchall():
        if team_id not in order:
            order.append(team_id)
    return order


async def _picks_beyond_round(db, draft_id, rounds):
    """Picks sitting in a round higher than `rounds` - what lowering a
    draft's round count would orphan. Returns
    [(pick_id, pick_number, round_number, team_name, is_traded, player_name)].
    """
    cursor = await db.execute(
        """SELECT dp.pick_id, dp.pick_number, dp.round_number, ct.team_name,
                  CASE WHEN dp.current_team_id != dp.original_team_id THEN 1 ELSE 0 END,
                  p.name
           FROM draft_picks dp
           LEFT JOIN teams ct ON dp.current_team_id = ct.team_id
           LEFT JOIN players p ON dp.player_selected_id = p.player_id
           WHERE dp.draft_id = ? AND dp.round_number > ?
           ORDER BY dp.pick_number""",
        (draft_id, rounds)
    )
    return await cursor.fetchall()


async def _add_draft_rounds(db, draft_id, draft_name, season_number, from_round, to_round):
    """Appends full rounds of picks for every team, in the draft's own order.
    Returns the number of picks created."""
    order = await _draft_round_order(db, draft_id)
    if not order:
        return 0

    cursor = await db.execute(
        "SELECT COALESCE(MAX(pick_number), 0) FROM draft_picks WHERE draft_id = ?", (draft_id,)
    )
    next_number = (await cursor.fetchone())[0] + 1

    created = 0
    for round_num in range(from_round + 1, to_round + 1):
        for team_id in order:
            await db.execute(
                """INSERT INTO draft_picks
                   (draft_id, draft_name, season_number, round_number, pick_number,
                    original_team_id, current_team_id, pick_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'ladder')""",
                (draft_id, draft_name, season_number or 0, round_num, next_number,
                 team_id, team_id)
            )
            next_number += 1
            created += 1
    return created


async def _renumber_draft_picks(db, draft_id):
    """Rewrites pick_number 1..N in the draft's existing order, closing any
    gaps left by a removal and making room after an insert. Ordered by the
    CURRENT pick_number so relative order is preserved; picks with no number
    yet (a freshly inserted one is given a fractional number by the caller)
    sort naturally into place."""
    cursor = await db.execute(
        """SELECT pick_id FROM draft_picks
           WHERE draft_id = ?
           ORDER BY pick_number IS NULL, pick_number, pick_id""",
        (draft_id,)
    )
    for new_number, (pick_id,) in enumerate((await cursor.fetchall()), start=1):
        await db.execute(
            "UPDATE draft_picks SET pick_number = ? WHERE pick_id = ?",
            (new_number, pick_id)
        )


class EditDraftView(discord.ui.View):
    """Main /editdraft menu: shows the draft's settings and its picks, with
    buttons for each kind of edit. Every edit re-queries and re-renders this
    same message, so the admin always sees the current state."""

    def __init__(self, bot, draft, teams):
        super().__init__(timeout=600)
        self.bot = bot
        (self.draft_id, self.draft_name, self.season_number,
         self.status, self.rounds, self.rookie_contract_years) = draft
        self.teams = teams                                  # [(team_id, team_name, emoji_id)]
        self.team_name_by_id = {row[0]: row[1] for row in teams}
        self.picks = []
        self.page = 0

    async def refresh(self, db):
        """Re-reads the draft row and its picks."""
        cursor = await db.execute(
            """SELECT draft_name, season_number, status, rounds, rookie_contract_years
               FROM drafts WHERE draft_id = ?""",
            (self.draft_id,)
        )
        row = await cursor.fetchone()
        if row:
            (self.draft_name, self.season_number, self.status,
             self.rounds, self.rookie_contract_years) = row

        # emoji_id and the selected player's name are fetched so the pick
        # list can render through format_draft_pick_line, the same formatter
        # /draftorder uses.
        cursor = await db.execute(
            """SELECT dp.pick_id, dp.pick_number, dp.round_number, dp.pick_origin,
                      ct.team_name, ct.emoji_id, p.name, dp.pick_type, ot.team_name, ot.emoji_id
               FROM draft_picks dp
               LEFT JOIN teams ct ON dp.current_team_id = ct.team_id
               LEFT JOIN teams ot ON dp.original_team_id = ot.team_id
               LEFT JOIN players p ON dp.player_selected_id = p.player_id
               WHERE dp.draft_id = ?
               ORDER BY dp.pick_number""",
            (self.draft_id,)
        )
        self.picks = await cursor.fetchall()
        if self.page >= self.total_pages:
            self.page = max(self.total_pages - 1, 0)
        self.update_components()

    @property
    def total_pick_pages(self):
        """Pages needed to list every pick in a Select (Discord caps one at
        25 options) - drives the Prev/Next buttons in the edit and remove
        windows."""
        if not self.picks:
            return 1
        return (len(self.picks) + PICK_SELECT_OPTIONS_PER_PAGE - 1) // PICK_SELECT_OPTIONS_PER_PAGE

    @property
    def rounds_present(self):
        """Round numbers this draft's picks actually span, in order - the
        list is paged one round at a time, the same way /draftorder does."""
        return sorted({p[2] for p in self.picks})

    @property
    def total_pages(self):
        return max(1, len(self.rounds_present))

    @property
    def current_round_number(self):
        rounds = self.rounds_present
        if not rounds:
            return None
        return rounds[min(self.page, len(rounds) - 1)]

    def page_picks(self):
        current = self.current_round_number
        if current is None:
            return []
        return [p for p in self.picks if p[2] == current]

    def update_components(self):
        self.clear_items()

        settings = discord.ui.Button(label="Edit Settings", style=discord.ButtonStyle.primary, row=0)
        settings.callback = self._edit_settings
        self.add_item(settings)

        add = discord.ui.Button(label="Add Pick", style=discord.ButtonStyle.success, row=0)
        add.callback = self._add_pick
        self.add_item(add)

        # Nothing to remove or transfer in an empty draft.
        if self.picks:
            edit_pick = discord.ui.Button(label="Edit Pick", style=discord.ButtonStyle.secondary, row=0)
            edit_pick.callback = self._edit_pick
            self.add_item(edit_pick)

            remove = discord.ui.Button(label="Remove Pick", style=discord.ButtonStyle.danger, row=0)
            remove.callback = self._remove_pick
            self.add_item(remove)

        add_page_buttons(self, self.page, self.total_pages, self._go_to_page, row=1, count_label="Round")

    def create_embed(self):
        embed = discord.Embed(
            title=f"Editing: {self.draft_name}",
            color=discord.Color.orange(),
        )
        season_text = "Custom (not season-linked)" if not self.season_number else f"Season {self.season_number}"
        header = (
            f"**Status:** {self.status}  •  **Season:** {season_text}\n"
            f"**Rounds:** {self.rounds}  •  **Rookie contract:** "
            f"{self.rookie_contract_years} years"
        )

        if not self.picks:
            embed.description = header + "\n\nThis draft has no picks."
            return embed

        # Same line format as /draftorder, via the shared formatter.
        lines = [
            format_draft_pick_line(
                self.bot, pick_number,
                pick_origin_label(pick_type, pick_origin, round_number,
                                  get_team_emoji_str(self.bot, original_emoji, trailing_space=False)
                                  or original_name),
                emoji_id, player_name)
            for (_pick_id, pick_number, round_number, pick_origin, _team, emoji_id, player_name,
                 pick_type, original_name, original_emoji) in self.page_picks()
        ]

        # The pick list goes in the DESCRIPTION, not a field: a field caps at
        # 1024 characters, and one round of 20+ picks exceeds that once real
        # team emojis (~26 chars each) are rendered. The description's limit
        # is 4096. /draftorder does the same for the same reason.
        # Picks can legitimately sit above the stored round count - lowering
        # it offers to delete them but never forces it. Surface the mismatch
        # so the header's "Rounds: N" isn't quietly contradicted by the list.
        beyond = [p for p in self.picks if p[2] > self.rounds]
        if beyond:
            header += (f"\n⚠️ {len(beyond)} pick(s) sit in rounds above "
                       f"{self.rounds}.")

        body = header + f"\n\n**Round {self.current_round_number}**\n" + "\n".join(lines)
        if len(body) > 4096:
            # Far beyond any realistic draft, but truncate rather than let
            # Discord reject the whole embed.
            body = body[:4080].rsplit("\n", 1)[0] + "\n…"
        embed.description = body

        embed.set_footer(
            text=f"Round {self.current_round_number} of {self.rounds_present[-1]} "
                 f"• {len(self.picks)} picks total"
        )
        return embed

    async def rerender(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            await self.refresh(db)
        await interaction.response.edit_message(
            content=None, embed=self.create_embed(), view=self
        )

    async def refresh_and_report(self, interaction: discord.Interaction, note):
        """Re-reads the draft and returns to the main menu, with a one-line
        note about what just changed carried above the embed."""
        async with aiosqlite.connect(DB_PATH) as db:
            await self.refresh(db)
        await interaction.response.edit_message(
            content=note, embed=self.create_embed(), view=self
        )

    async def _go_to_page(self, interaction: discord.Interaction, page):
        self.page = page
        self.update_components()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    async def _edit_settings(self, interaction: discord.Interaction):
        await interaction.response.send_modal(EditDraftSettingsModal(self))

    async def _add_pick(self, interaction: discord.Interaction):
        view = _AddPickView(self)
        await interaction.response.edit_message(
            content="**Add a pick** - choose the team that will own it.",
            embed=None, view=view
        )

    async def _edit_pick(self, interaction: discord.Interaction):
        view = _EditPickView(self)
        await interaction.response.edit_message(
            content=view.status_text(), embed=None, view=view
        )

    async def _remove_pick(self, interaction: discord.Interaction):
        view = _RemovePickView(self)
        await interaction.response.edit_message(
            content="**Remove a pick** - choose the pick to delete.",
            embed=None, view=view
        )


class EditDraftSettingsModal(discord.ui.Modal, title="Edit Draft Settings"):
    """Name, rounds and rookie contract length in one form.

    Changing `rounds` only changes the stored number - it does NOT add or
    delete picks, since regenerating them would silently undo any pick trade
    made against this draft (the same reasoning
    update_indicative_draft_order documents in season_commands.py). Use the
    Add/Remove Pick buttons for the picks themselves.
    """

    def __init__(self, parent_view):
        super().__init__()
        self.parent_view = parent_view

        self.name_input = discord.ui.TextInput(
            label="Draft name", default=parent_view.draft_name, max_length=100
        )
        self.rounds_input = discord.ui.TextInput(
            label="Rounds", default=str(parent_view.rounds), max_length=2
        )
        self.contract_input = discord.ui.TextInput(
            label="Rookie contract (years)",
            default=str(parent_view.rookie_contract_years), max_length=2
        )
        self.add_item(self.name_input)
        self.add_item(self.rounds_input)
        self.add_item(self.contract_input)

    async def on_submit(self, interaction: discord.Interaction):
        new_name = self.name_input.value.strip()
        if not new_name:
            await interaction.response.send_message("❌ Draft name can't be empty.", ephemeral=True)
            return

        try:
            new_rounds = int(self.rounds_input.value.strip())
            new_contract = int(self.contract_input.value.strip())
        except ValueError:
            await interaction.response.send_message(
                "❌ Rounds and rookie contract must be whole numbers.", ephemeral=True
            )
            return

        if not 1 <= new_rounds <= 10:
            await interaction.response.send_message(
                "❌ Rounds must be between 1 and 10.", ephemeral=True
            )
            return
        if not 1 <= new_contract <= 10:
            await interaction.response.send_message(
                "❌ Rookie contract must be between 1 and 10 years.", ephemeral=True
            )
            return

        async with aiosqlite.connect(DB_PATH) as db:
            if new_name != self.parent_view.draft_name:
                cursor = await db.execute(
                    "SELECT draft_id FROM drafts WHERE draft_name = ? AND draft_id != ?",
                    (new_name, self.parent_view.draft_id)
                )
                if await cursor.fetchone():
                    await interaction.response.send_message(
                        f"❌ A draft named '{new_name}' already exists.", ephemeral=True
                    )
                    return

            old_rounds = self.parent_view.rounds

            # LOWERING the round count would orphan every pick above the new
            # number. Those picks may have been traded, so they are never
            # deleted silently - the admin is shown exactly what would go and
            # has to confirm. The name/contract changes still apply now.
            doomed = []
            if new_rounds < old_rounds:
                doomed = await _picks_beyond_round(db, self.parent_view.draft_id, new_rounds)

            await db.execute(
                "UPDATE drafts SET draft_name = ?, rounds = ?, rookie_contract_years = ? WHERE draft_id = ?",
                (new_name, new_rounds, new_contract, self.parent_view.draft_id)
            )
            # draft_picks carries a denormalised draft_name, so a rename has
            # to update both or the picks stop matching their draft.
            await db.execute(
                "UPDATE draft_picks SET draft_name = ? WHERE draft_id = ?",
                (new_name, self.parent_view.draft_id)
            )

            # RAISING it fills in the new rounds, so the draft actually has
            # the picks its round count claims.
            added = 0
            if new_rounds > old_rounds:
                added = await _add_draft_rounds(
                    db, self.parent_view.draft_id, new_name,
                    self.parent_view.season_number, old_rounds, new_rounds
                )
            await db.commit()

        if doomed:
            confirm_view = _ConfirmRoundCullView(self.parent_view, new_rounds, doomed)
            await interaction.response.edit_message(
                content=confirm_view.prompt_text(), embed=None, view=confirm_view
            )
            return

        note = None
        if added:
            note = (f"✅ Added {added} pick(s) across "
                    f"round{'s' if new_rounds - old_rounds > 1 else ''} "
                    f"{old_rounds + 1}-{new_rounds}.")
        if note:
            await self.parent_view.refresh_and_report(interaction, note)
        else:
            await self.parent_view.rerender(interaction)


class _ConfirmRoundCullView(discord.ui.View):
    """Lowering a draft's round count leaves picks stranded above it. This
    asks before deleting them, and calls out any that have been TRADED -
    deleting one of those silently would erase a trade with no undo."""

    def __init__(self, parent_view, new_rounds, doomed):
        super().__init__(timeout=300)
        self.parent_view = parent_view
        self.new_rounds = new_rounds
        self.doomed = doomed

    def prompt_text(self):
        traded = [d for d in self.doomed if d[4]]
        used = [d for d in self.doomed if d[5]]
        lines = [
            f"⚠️ **{len(self.doomed)} pick(s)** sit in rounds above {self.new_rounds}.",
            "",
            "The round count has been updated. Do you also want to **delete** those picks?",
        ]
        if traded:
            lines += [
                "",
                f"🔁 **{len(traded)} of them have been traded** - deleting them "
                f"undoes those trades:",
            ]
            lines += [f"• #{d[1]} (R{d[2]}) → {d[3] or 'Unknown'}" for d in traded[:10]]
            if len(traded) > 10:
                lines.append(f"• …and {len(traded) - 10} more")
        if used:
            lines += ["", f"✅ **{len(used)} have already been used** on a player."]
        lines += ["", "Keeping them is safe - they simply stay in the draft."]
        return "\n".join(lines)

    @discord.ui.button(label="Delete those picks", style=discord.ButtonStyle.danger)
    async def delete_them(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM draft_picks WHERE draft_id = ? AND round_number > ?",
                (self.parent_view.draft_id, self.new_rounds)
            )
            await _renumber_draft_picks(db, self.parent_view.draft_id)
            await db.commit()
        await self.parent_view.refresh_and_report(
            interaction, f"🗑️ Deleted {len(self.doomed)} pick(s) above round {self.new_rounds}."
        )

    @discord.ui.button(label="Keep them", style=discord.ButtonStyle.secondary)
    async def keep_them(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.parent_view.refresh_and_report(
            interaction,
            f"✅ Round count updated. {len(self.doomed)} pick(s) above "
            f"round {self.new_rounds} were kept."
        )


class _BackToEditDraftButton(discord.ui.Button):
    def __init__(self, parent_view, row=4):
        super().__init__(label="◀ Back", style=discord.ButtonStyle.secondary, row=row)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            await self.parent_view.refresh(db)
        await interaction.response.edit_message(
            content=None, embed=self.parent_view.create_embed(), view=self.parent_view
        )


class _AddPickView(discord.ui.View):
    """Pick a team, then a position to insert at."""

    def __init__(self, parent_view):
        super().__init__(timeout=300)
        self.parent_view = parent_view
        self.add_item(_AddPickTeamSelect(parent_view))
        self.add_item(_BackToEditDraftButton(parent_view, row=1))


class _AddPickTeamSelect(discord.ui.Select):
    def __init__(self, parent_view):
        # Discord caps a Select at 25 options.
        options = build_team_options(parent_view.bot, parent_view.teams)
        super().__init__(placeholder="Team that owns the new pick...", options=options, row=0)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        team_id = int(self.values[0])
        await interaction.response.send_modal(_AddPickModal(self.parent_view, team_id))


class _AddPickModal(discord.ui.Modal, title="Add Pick"):
    def __init__(self, parent_view, team_id):
        super().__init__()
        self.parent_view = parent_view
        self.team_id = team_id

        next_number = len(parent_view.picks) + 1
        self.position_input = discord.ui.TextInput(
            label="Insert at pick number",
            default=str(next_number),
            placeholder=f"1 to {next_number}",
            max_length=4,
        )
        self.round_input = discord.ui.TextInput(
            label="Round", default="1", max_length=2
        )
        self.origin_input = discord.ui.TextInput(
            label="Origin (optional)",
            placeholder="e.g. Compensation Pick",
            required=False, max_length=100,
        )
        self.add_item(self.position_input)
        self.add_item(self.round_input)
        self.add_item(self.origin_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            insert_at = int(self.position_input.value.strip())
            round_number = int(self.round_input.value.strip())
        except ValueError:
            await interaction.response.send_message(
                "❌ Pick number and round must be whole numbers.", ephemeral=True
            )
            return

        max_position = len(self.parent_view.picks) + 1
        if not 1 <= insert_at <= max_position:
            await interaction.response.send_message(
                f"❌ Insert position must be between 1 and {max_position}.", ephemeral=True
            )
            return
        if round_number < 1:
            await interaction.response.send_message("❌ Round must be 1 or higher.", ephemeral=True)
            return

        team_name = self.parent_view.team_name_by_id.get(self.team_id, "Unknown")
        origin = self.origin_input.value.strip()

        async with aiosqlite.connect(DB_PATH) as db:
            # Insert just BEFORE the pick currently at that number by giving
            # the new row a fractional pick_number, then renumbering 1..N.
            # (The old /addpick shifted every later pick one at a time.)
            await db.execute(
                """INSERT INTO draft_picks
                   (draft_id, draft_name, season_number, round_number, pick_number,
                    pick_origin, original_team_id, current_team_id, pick_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'custom')""",
                (self.parent_view.draft_id, self.parent_view.draft_name,
                 self.parent_view.season_number or 0, round_number, insert_at - 0.5,
                 origin or None, self.team_id, self.team_id)
            )
            await _renumber_draft_picks(db, self.parent_view.draft_id)
            await db.commit()

        await self.parent_view.refresh_and_report(
            interaction, f"✅ Added a pick for **{team_name}** at #{insert_at}."
        )


PICK_SELECT_OPTIONS_PER_PAGE = 25


class _EditPickView(discord.ui.View):
    """One window for everything about a single pick: choose the pick, choose
    a new owner, and confirm - plus a button for editing the pick's own
    details (its number and origin).

    Both dropdowns are shown together rather than one leading to the other,
    so the admin can see and change either before committing. Nothing is
    written until Confirm is pressed.
    """

    def __init__(self, parent_view):
        super().__init__(timeout=300)
        self.parent_view = parent_view
        self.pick_id = None
        self.new_team_id = None
        self.page = 0
        self.update_components()

    @property
    def selected_pick(self):
        if self.pick_id is None:
            return None
        return next((p for p in self.parent_view.picks if p[0] == self.pick_id), None)

    def update_components(self):
        self.clear_items()
        # Rows 0-1: pick chooser (+ its pagination), row 2: new owner,
        # rows 3-4: actions.
        self.add_item(_PickSelect(self.parent_view, self, "Pick to edit...", row=0,
                                  page=self.page, selected_id=self.pick_id))

        add_page_buttons(self, self.page, self.parent_view.total_pick_pages, self._go_to_page, row=1,
                         prev_label="◀ Prev picks", next_label="Next picks ▶")

        self.add_item(_EditPickTeamSelect(self.parent_view, self, row=2))

        confirm = discord.ui.Button(
            label="Confirm Transfer", style=discord.ButtonStyle.success, row=3,
            disabled=(self.pick_id is None or self.new_team_id is None),
        )
        confirm.callback = self._confirm
        self.add_item(confirm)

        details = discord.ui.Button(
            label="Edit Pick Details", style=discord.ButtonStyle.primary, row=3,
            disabled=(self.pick_id is None),
        )
        details.callback = self._edit_details
        self.add_item(details)

        self.add_item(_BackToEditDraftButton(self.parent_view, row=4))

    def status_text(self):
        pick = self.selected_pick
        if pick is None:
            return "**Edit a pick** - choose a pick to begin."
        label = f"#{pick[1]} ({pick[4] or 'Unknown'})"
        if self.new_team_id is None:
            return (f"**Editing {label}** - choose a new owner to transfer it, "
                    f"or use Edit Pick Details.")
        new_owner = self.parent_view.team_name_by_id.get(self.new_team_id, "Unknown")
        return f"**Editing {label}** - transfer to **{new_owner}**? Press Confirm Transfer."

    async def rerender(self, interaction: discord.Interaction):
        self.update_components()
        await interaction.response.edit_message(content=self.status_text(), view=self)

    async def _go_to_page(self, interaction: discord.Interaction, page):
        self.page = page
        await self.rerender(interaction)

    async def _edit_details(self, interaction: discord.Interaction):
        pick = self.selected_pick
        if pick is None:
            await interaction.response.send_message("❌ Choose a pick first.", ephemeral=True)
            return
        await interaction.response.send_modal(_EditPickDetailsModal(self.parent_view, self, pick))

    async def _confirm(self, interaction: discord.Interaction):
        if self.pick_id is None or self.new_team_id is None:
            await interaction.response.send_message(
                "❌ Choose both a pick and a new owner first.", ephemeral=True
            )
            return

        team_name = self.parent_view.team_name_by_id.get(self.new_team_id, "Unknown")
        async with aiosqlite.connect(DB_PATH) as db:
            # current_team_id only - original_team_id records who EARNED the
            # pick and must survive any number of trades.
            await db.execute(
                "UPDATE draft_picks SET current_team_id = ? WHERE pick_id = ?",
                (self.new_team_id, self.pick_id)
            )
            await db.commit()

        await self.parent_view.refresh_and_report(
            interaction, f"✅ Pick transferred to **{team_name}**."
        )


class _EditPickTeamSelect(discord.ui.Select):
    def __init__(self, parent_view, edit_view, row):
        options = build_team_options(
            parent_view.bot, parent_view.teams, selected=edit_view.new_team_id
        )
        super().__init__(placeholder="Transfer to...", options=options, row=row)
        self.parent_view = parent_view
        self.edit_view = edit_view

    async def callback(self, interaction: discord.Interaction):
        self.edit_view.new_team_id = int(self.values[0])
        await self.edit_view.rerender(interaction)


class _EditPickDetailsModal(discord.ui.Modal, title="Edit Pick Details"):
    """The pick's own number and origin text. Changing the number moves the
    pick in the order; everything is renumbered 1..N afterwards so there are
    never gaps or duplicates."""

    def __init__(self, parent_view, edit_view, pick):
        super().__init__()
        self.parent_view = parent_view
        self.edit_view = edit_view
        self.pick_id = pick[0]
        self.current_number = pick[1]

        self.number_input = discord.ui.TextInput(
            label="Pick number", default=str(pick[1]), max_length=4
        )
        self.origin_input = discord.ui.TextInput(
            label="Origin (optional)", default=pick[3] or "",
            placeholder=("Leave blank to show \"R1 tied to <team>\"" if pick[7] == 'ladder'
                         else "e.g. Trade with Adelaide"),
            required=False, max_length=100,
        )
        self.add_item(self.number_input)
        self.add_item(self.origin_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            new_number = int(self.number_input.value.strip())
        except ValueError:
            await interaction.response.send_message(
                "❌ Pick number must be a whole number.", ephemeral=True
            )
            return

        total = len(self.parent_view.picks)
        if not 1 <= new_number <= total:
            await interaction.response.send_message(
                f"❌ Pick number must be between 1 and {total}.", ephemeral=True
            )
            return

        origin = self.origin_input.value.strip()

        async with aiosqlite.connect(DB_PATH) as db:
            if new_number != self.current_number:
                # Nudge just past (or before) the target so the renumber pass
                # lands this pick on exactly that number, then close the gaps.
                nudge = new_number + (0.5 if new_number > self.current_number else -0.5)
                await db.execute(
                    "UPDATE draft_picks SET pick_number = ?, pick_origin = ? WHERE pick_id = ?",
                    (nudge, origin, self.pick_id)
                )
                await _renumber_draft_picks(db, self.parent_view.draft_id)
            else:
                await db.execute(
                    "UPDATE draft_picks SET pick_origin = ? WHERE pick_id = ?",
                    (origin, self.pick_id)
                )
            await db.commit()

        await self.parent_view.refresh_and_report(
            interaction, f"✅ Pick updated (now #{new_number})."
        )


class _RemovePickView(discord.ui.View):
    """Choose a pick to delete, with pagination when the draft has more picks
    than Discord's 25-option Select cap."""

    def __init__(self, parent_view):
        super().__init__(timeout=300)
        self.parent_view = parent_view
        self.page = 0
        self.update_components()

    def update_components(self):
        self.clear_items()
        self.add_item(_PickSelect(self.parent_view, self, "Pick to remove...",
                                  row=0, page=self.page))

        add_page_buttons(self, self.page, self.parent_view.total_pick_pages, self._go_to_page, row=1,
                         prev_label="◀ Prev picks", next_label="Next picks ▶")

        self.add_item(_BackToEditDraftButton(self.parent_view, row=2))

    async def _go_to_page(self, interaction: discord.Interaction, page):
        self.page = page
        self.update_components()
        await interaction.response.edit_message(view=self)


class _PickSelect(discord.ui.Select):
    """One page of the draft's picks. Discord caps a Select at 25 options, so
    a long draft is paged by the owning view's Prev/Next buttons rather than
    silently showing only the first 25."""

    def __init__(self, parent_view, owner_view, placeholder, row, page=0, selected_id=None):
        picks = parent_view.picks
        start = page * PICK_SELECT_OPTIONS_PER_PAGE
        page_picks = picks[start:start + PICK_SELECT_OPTIONS_PER_PAGE]

        options = []
        for (pick_id, pick_number, round_number, pick_origin, team_name, _emoji_id, player_name,
             pick_type, original_name, _original_emoji) in page_picks:
            label = f"#{pick_number} - {team_name or 'Unknown'}"
            description = f"Round {round_number}"
            # Dropdowns can't show custom emojis, so the team's name here.
            origin = pick_origin_label(pick_type, pick_origin, round_number, original_name)
            if origin:
                description += f" • {origin}"
            if player_name:
                description += f" • used on {player_name}"
            options.append(discord.SelectOption(
                label=label[:100], value=str(pick_id),
                description=description[:100],
                default=(pick_id == selected_id),
            ))

        # A Select with no options is rejected by Discord; an empty page can
        # only happen if every pick was removed while the view was open.
        if not options:
            options = [discord.SelectOption(label="No picks available", value="none")]

        super().__init__(placeholder=placeholder, options=options, row=row)
        self.parent_view = parent_view
        self.owner_view = owner_view

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.send_message("❌ No picks available.", ephemeral=True)
            return

        pick_id = int(self.values[0])

        if isinstance(self.owner_view, _EditPickView):
            self.owner_view.pick_id = pick_id
            await self.owner_view.rerender(interaction)
            return

        # Remove.
        chosen = next((p for p in self.parent_view.picks if p[0] == pick_id), None)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM draft_picks WHERE pick_id = ?", (pick_id,))
            # Close the gap so pick numbers stay 1..N with no hole.
            await _renumber_draft_picks(db, self.parent_view.draft_id)
            await db.commit()

        label = f"#{chosen[1]} ({chosen[4]})" if chosen else "Pick"
        await self.parent_view.refresh_and_report(interaction, f"✅ Removed {label}.")


async def setup(bot):
    await bot.add_cog(DraftCommands(bot))
