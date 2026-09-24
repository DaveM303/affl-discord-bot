import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from config import DB_PATH
from utils import (is_admin_user, assign_drafted_player, get_team_emoji,
                   get_team_emoji_str, build_team_options, fetch_teams_for_dropdown)

class DraftCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

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
                        team_name = team_name_by_id[team_id]
                        pick_origin = f"{team_name} R{round_num}"
                        await db.execute(
                            """INSERT INTO draft_picks (draft_id, draft_name, season_number, round_number, pick_number,
                                                        pick_origin, original_team_id, current_team_id)
                               VALUES (?, ?, 0, ?, ?, ?, ?, ?)""",
                            (draft_id, draft_name, round_num, pick_counter, pick_origin, team_id, team_id)
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
                              p.name as player_selected
                       FROM draft_picks dp
                       JOIN teams ct ON dp.current_team_id = ct.team_id
                       LEFT JOIN players p ON dp.player_selected_id = p.player_id
                       WHERE dp.draft_name = ? AND dp.pick_number IS NOT NULL
                       ORDER BY dp.pick_number""",
                    (draft_name,)
                )
                picks = await cursor.fetchall()

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

    @app_commands.command(name="livedraft", description="[ADMIN] Manage live draft")
    @app_commands.describe(
        action="Action to perform",
        draft_name="Name of the draft"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="start", value="start"),
        app_commands.Choice(name="end", value="end"),
        app_commands.Choice(name="re-send current pick notification", value="resend")
    ])
    @app_commands.autocomplete(draft_name=draft_name_autocomplete)
    async def live_draft(self, interaction: discord.Interaction, action: str, draft_name: str):
        await interaction.response.defer(ephemeral=True)

        # Check if user has admin role
        if not await is_admin_user(interaction):
            await interaction.followup.send("❌ You don't have permission to use this command.", ephemeral=True)
            return

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get draft info
                cursor = await db.execute(
                    "SELECT draft_id, status, rounds, season_number, current_pick_number FROM drafts WHERE draft_name = ?",
                    (draft_name,)
                )
                draft_info = await cursor.fetchone()

                if not draft_info:
                    await interaction.followup.send(f"❌ Draft '{draft_name}' not found!", ephemeral=True)
                    return

                draft_id, status, rounds, season_number, current_pick_number = draft_info

                if action == "start":
                    # Check if draft is current (has ladder set)
                    if status != 'current':
                        await interaction.followup.send(
                            f"❌ Draft '{draft_name}' is not ready to start (status: {status})!\n"
                            f"A season-linked draft's order is set automatically once its season starts; "
                            f"a custom draft's order is set at creation via `/createcustomdraft`.",
                            ephemeral=True
                        )
                        return

                    # Check if draft has already been started
                    cursor = await db.execute(
                        "SELECT started_at FROM drafts WHERE draft_id = ?",
                        (draft_id,)
                    )
                    started_at = (await cursor.fetchone())[0]
                    if started_at:
                        await interaction.followup.send(
                            f"❌ Draft '{draft_name}' has already been started!",
                            ephemeral=True
                        )
                        return

                    # Get draft channel from settings
                    cursor = await db.execute(
                        "SELECT setting_value FROM settings WHERE setting_key = 'draft_channel_id'"
                    )
                    result = await cursor.fetchone()
                    if not result or not result[0]:
                        await interaction.followup.send(
                            "❌ No draft channel configured! Use `/config` to set the draft channel.",
                            ephemeral=True
                        )
                        return

                    draft_channel_id = int(result[0])
                    draft_channel = self.bot.get_channel(draft_channel_id)
                    if not draft_channel:
                        await interaction.followup.send(
                            "❌ Draft channel not found! Please check the configuration.",
                            ephemeral=True
                        )
                        return

                    # Check if there are draft-eligible players (in Draft Pool team)
                    cursor = await db.execute(
                        """SELECT COUNT(*) FROM players p
                           JOIN teams t ON p.team_id = t.team_id
                           WHERE t.team_name = 'Draft Pool'"""
                    )
                    draft_pool_count = (await cursor.fetchone())[0]

                    if draft_pool_count == 0:
                        await interaction.followup.send(
                            "❌ No players in the Draft Pool! Use `/updateplayer` to assign players to the 'Draft Pool' team.",
                            ephemeral=True
                        )
                        return

                    # Update draft status to 'in_progress' and set started_at
                    await db.execute(
                        """UPDATE drafts
                           SET status = 'in_progress', started_at = CURRENT_TIMESTAMP, current_pick_number = 1
                           WHERE draft_id = ?""",
                        (draft_id,)
                    )
                    await db.commit()

                    # Post draft start message to draft channel
                    await draft_channel.send(f"# {draft_name}")

                    # Send first pick notification
                    await self.send_pick_notification(db, draft_id, draft_name, 1)

                    await interaction.followup.send(
                        f"✅ **Draft Started!**\n\n"
                        f"**Draft:** {draft_name}\n"
                        f"**Rounds:** {rounds}\n"
                        f"**Players Available:** {draft_pool_count}\n\n"
                        f"Pick notifications have been sent to team channels.",
                        ephemeral=True
                    )

                elif action == "end":
                    # Check if draft is in progress
                    if status != 'in_progress':
                        await interaction.followup.send(
                            f"❌ Draft '{draft_name}' is not currently in progress (status: {status})!",
                            ephemeral=True
                        )
                        return

                    # End the draft
                    await self.complete_draft(db, draft_id, draft_name)

                    await interaction.followup.send(
                        f"✅ **Draft Ended!**\n\n"
                        f"**Draft:** {draft_name}\n"
                        f"The draft has been marked as completed.",
                        ephemeral=True
                    )

                elif action == "resend":
                    # Check if draft is in progress
                    if status != 'in_progress':
                        await interaction.followup.send(
                            f"❌ Draft '{draft_name}' is not currently in progress (status: {status})!",
                            ephemeral=True
                        )
                        return

                    if not current_pick_number:
                        await interaction.followup.send(
                            f"❌ No current pick to re-send!",
                            ephemeral=True
                        )
                        return

                    # Re-send the current pick notification
                    await self.send_pick_notification(db, draft_id, draft_name, current_pick_number)

                    await interaction.followup.send(
                        f"✅ **Pick Notification Re-sent!**\n\n"
                        f"**Draft:** {draft_name}\n"
                        f"**Current Pick:** {current_pick_number}\n\n"
                        f"Notification has been re-sent to the draft channel and team channel.",
                        ephemeral=True
                    )

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)
            import traceback
            traceback.print_exc()

    async def send_pick_notification(self, db, draft_id, draft_name, pick_number):
        """Send draft pick notification to team's channel"""
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
                if (pick_num - 1) % picks_per_round == 0:
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

            print(f"Draft '{draft_name}' completed!")

        except Exception as e:
            print(f"Error completing draft: {e}")
            import traceback
            traceback.print_exc()


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
        # Enable/disable buttons based on current round
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                if item.custom_id == "prev":
                    item.disabled = (self.current_round == 1)
                elif item.custom_id == "next":
                    item.disabled = (self.current_round == self.max_rounds)

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.gray, custom_id="prev")
    async def previous_round(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_round > 1:
            self.current_round -= 1
            self.update_buttons()
            embed = self.create_embed()
            await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.gray, custom_id="next")
    async def next_round(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_round < self.max_rounds:
            self.current_round += 1
            self.update_buttons()
            embed = self.create_embed()
            await interaction.response.edit_message(embed=embed, view=self)


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

        # Update pagination buttons
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                if item.custom_id == "draft_prev_page":
                    item.disabled = (self.current_page == 0)
                elif item.custom_id == "draft_next_page":
                    item.disabled = (self.current_page >= total_pages - 1)

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

    @discord.ui.button(label="◀ Previous Page", style=discord.ButtonStyle.gray, custom_id="draft_prev_page", row=1)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Go to previous page"""
        if self.current_page > 0:
            self.current_page -= 1
            async with aiosqlite.connect(DB_PATH) as db:
                embed = await self.create_embed(db)
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="Next Page ▶", style=discord.ButtonStyle.gray, custom_id="draft_next_page", row=1)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Go to next page"""
        self.current_page += 1
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

            # Get pick info for round header
            cursor = await db.execute(
                """SELECT dp.round_number
                   FROM draft_picks dp
                   WHERE dp.draft_id = ? AND dp.pick_number = ?""",
                (self.draft_id, self.pick_number)
            )
            round_num = (await cursor.fetchone())[0]

            # Check if this is the first pick of a new round (post round header)
            cursor = await db.execute(
                "SELECT COUNT(*) FROM draft_picks WHERE draft_id = ? AND round_number = 1",
                (self.draft_id,)
            )
            picks_per_round = (await cursor.fetchone())[0]

            if (self.pick_number - 1) % picks_per_round == 0:
                # Calculate the actual round number for this pick
                actual_round = ((self.pick_number - 1) // picks_per_round) + 1
                await draft_channel.send(f"**-- ROUND {actual_round} --**")

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
                pick_origin, original_team_id, current_team_id, player_selected_id, picked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
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
                        pick_origin, original_team_id, current_team_id, player_selected_id, picked_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                    (self.draft_id, draft_name, season_number, comp_round_number, comp_pick_number,
                     f"{self.fs_team_name} F/S Comp", self.fs_team_id, self.fs_team_id, None)
                )

        # Step 5: Assign player to father/son club
        await assign_drafted_player(db, self.fs_team_id, self.player_id, season_number, rookie_years)

        await db.commit()

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

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.gray, row=1)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Go to previous page"""
        if self.current_page > 0:
            self.current_page -= 1
            self.update_dropdown()
            embed = self.create_embed()
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.gray, row=1)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Go to next page"""
        total_pages = (len(self.all_picks) + self.picks_per_page - 1) // self.picks_per_page
        if self.current_page < total_pages - 1:
            self.current_page += 1
            self.update_dropdown()
            embed = self.create_embed()
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.defer()

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

        # Update button states
        total_pages = (len(self.all_picks) + self.picks_per_page - 1) // self.picks_per_page
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                if "◀ Previous" in item.label:
                    item.disabled = (self.current_page == 0)
                elif "Next ▶" in item.label:
                    item.disabled = (self.current_page >= total_pages - 1)


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
        if self.total_pages <= 1:
            return

        prev_button = discord.ui.Button(
            label="◀ Prev", style=discord.ButtonStyle.secondary,
            disabled=(self.current_page == 0)
        )
        prev_button.callback = self.previous_page
        self.add_item(prev_button)

        next_button = discord.ui.Button(
            label="Next ▶", style=discord.ButtonStyle.secondary,
            disabled=(self.current_page >= self.total_pages - 1)
        )
        next_button.callback = self.next_page
        self.add_item(next_button)

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

    async def previous_page(self, interaction: discord.Interaction):
        if self.current_page > 0:
            self.current_page -= 1
            self.update_components()
            await interaction.response.edit_message(embed=self.create_embed(), view=self)
        else:
            await interaction.response.defer()

    async def next_page(self, interaction: discord.Interaction):
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            self.update_components()
            await interaction.response.edit_message(embed=self.create_embed(), view=self)
        else:
            await interaction.response.defer()


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

    team_names = {}
    cursor = await db.execute("SELECT team_id, team_name FROM teams")
    for team_id, team_name in await cursor.fetchall():
        team_names[team_id] = team_name

    created = 0
    for round_num in range(from_round + 1, to_round + 1):
        for team_id in order:
            origin = f"{team_names.get(team_id, 'Unknown')} R{round_num}"
            await db.execute(
                """INSERT INTO draft_picks
                   (draft_id, draft_name, season_number, round_number, pick_number,
                    pick_origin, original_team_id, current_team_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (draft_id, draft_name, season_number or 0, round_num, next_number,
                 origin, team_id, team_id)
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
                      ct.team_name, ct.emoji_id, p.name
               FROM draft_picks dp
               LEFT JOIN teams ct ON dp.current_team_id = ct.team_id
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

        if self.total_pages > 1:
            prev_button = discord.ui.Button(
                label="◀ Previous Round", style=discord.ButtonStyle.secondary,
                disabled=(self.page == 0), row=1
            )
            prev_button.callback = self._previous_page
            self.add_item(prev_button)

            next_button = discord.ui.Button(
                label="Next Round ▶", style=discord.ButtonStyle.secondary,
                disabled=(self.page >= self.total_pages - 1), row=1
            )
            next_button.callback = self._next_page
            self.add_item(next_button)

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
            format_draft_pick_line(self.bot, pick_number, pick_origin, emoji_id, player_name)
            for _pick_id, pick_number, _round, pick_origin, _team, emoji_id, player_name
            in self.page_picks()
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

    async def _previous_page(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        self.update_components()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    async def _next_page(self, interaction: discord.Interaction):
        self.page = min(self.total_pages - 1, self.page + 1)
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
                    pick_origin, original_team_id, current_team_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (self.parent_view.draft_id, self.parent_view.draft_name,
                 self.parent_view.season_number or 0, round_number, insert_at - 0.5,
                 origin, self.team_id, self.team_id)
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

        if self.parent_view.total_pick_pages > 1:
            prev_button = discord.ui.Button(
                label="◀ Prev picks", style=discord.ButtonStyle.secondary,
                disabled=(self.page == 0), row=1
            )
            prev_button.callback = self._previous_page
            self.add_item(prev_button)

            page_label = discord.ui.Button(
                label=f"{self.page + 1}/{self.parent_view.total_pick_pages}",
                style=discord.ButtonStyle.secondary, disabled=True, row=1
            )
            self.add_item(page_label)

            next_button = discord.ui.Button(
                label="Next picks ▶", style=discord.ButtonStyle.secondary,
                disabled=(self.page >= self.parent_view.total_pick_pages - 1), row=1
            )
            next_button.callback = self._next_page
            self.add_item(next_button)

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

    async def _previous_page(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        await self.rerender(interaction)

    async def _next_page(self, interaction: discord.Interaction):
        self.page = min(self.parent_view.total_pick_pages - 1, self.page + 1)
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
            placeholder="e.g. Adelaide R1", required=False, max_length=100,
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

        if self.parent_view.total_pick_pages > 1:
            prev_button = discord.ui.Button(
                label="◀ Prev picks", style=discord.ButtonStyle.secondary,
                disabled=(self.page == 0), row=1
            )
            prev_button.callback = self._previous_page
            self.add_item(prev_button)

            page_label = discord.ui.Button(
                label=f"{self.page + 1}/{self.parent_view.total_pick_pages}",
                style=discord.ButtonStyle.secondary, disabled=True, row=1
            )
            self.add_item(page_label)

            next_button = discord.ui.Button(
                label="Next picks ▶", style=discord.ButtonStyle.secondary,
                disabled=(self.page >= self.parent_view.total_pick_pages - 1), row=1
            )
            next_button.callback = self._next_page
            self.add_item(next_button)

        self.add_item(_BackToEditDraftButton(self.parent_view, row=2))

    async def _previous_page(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        self.update_components()
        await interaction.response.edit_message(view=self)

    async def _next_page(self, interaction: discord.Interaction):
        self.page = min(self.parent_view.total_pick_pages - 1, self.page + 1)
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
        for pick_id, pick_number, round_number, pick_origin, team_name, _emoji_id, player_name in page_picks:
            label = f"#{pick_number} - {team_name or 'Unknown'}"
            description = f"Round {round_number}"
            if pick_origin:
                description += f" • {pick_origin}"
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
