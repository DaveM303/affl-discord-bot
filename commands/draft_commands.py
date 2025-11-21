import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from config import DB_PATH, ADMIN_ROLE_ID

class DraftCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        """Called when the cog is loaded - re-register persistent views"""
        await self.register_persistent_views()

    async def register_persistent_views(self):
        """Re-register all persistent draft pick views on bot startup"""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Find any in-progress drafts
                cursor = await db.execute(
                    "SELECT draft_id, draft_name FROM drafts WHERE status = 'in_progress'"
                )
                active_drafts = await cursor.fetchall()

                for draft_id, draft_name in active_drafts:
                    # Get current pick number
                    cursor = await db.execute(
                        "SELECT current_pick_number FROM drafts WHERE draft_id = ?",
                        (draft_id,)
                    )
                    current_pick = (await cursor.fetchone())[0]

                    # Get the pick info for the current pick
                    cursor = await db.execute(
                        """SELECT dp.current_team_id, dp.pick_number
                           FROM draft_picks dp
                           WHERE dp.draft_id = ? AND dp.pick_number = ? AND dp.player_selected_id IS NULL""",
                        (draft_id, current_pick)
                    )
                    pick_info = await cursor.fetchone()

                    if pick_info:
                        team_id, pick_number = pick_info
                        view = DraftPickView(self.bot, draft_id, draft_name, team_id, pick_number)
                        self.bot.add_view(view)

                print(f"Re-registered draft pick views for {len(active_drafts)} active draft(s)")

        except Exception as e:
            print(f"Error registering draft persistent views: {e}")
            import traceback
            traceback.print_exc()

    async def draft_name_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for draft names - only shows current drafts with ladder set"""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    """SELECT draft_name FROM drafts
                       WHERE status = 'current'
                       ORDER BY draft_id DESC"""
                )
                drafts = await cursor.fetchall()

            choices = []
            for (draft_name,) in drafts:
                if current.lower() in draft_name.lower():
                    choices.append(app_commands.Choice(name=draft_name, value=draft_name))

            return choices[:25]
        except:
            return []

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
        except:
            return []

    @app_commands.command(name="createdraft", description="[ADMIN] Create a draft (season-linked or manual)")
    @app_commands.describe(
        season_number="Season number to link this draft to (leave blank for manual draft)",
        draft_name="Custom draft name (only for manual drafts, leave blank for season-linked)",
        rounds="Number of rounds (default: 4)",
        rookie_contract_years="Rookie contract length in years (default: 3)",
        save_ladder_for_season="Optional: Season number to save this ladder for (for historical records)"
    )
    async def create_draft(self, interaction: discord.Interaction, season_number: int = None, draft_name: str = None, rounds: int = 4, rookie_contract_years: int = 3, save_ladder_for_season: int = None):
        await interaction.response.defer(ephemeral=True)

        # Check if user has admin role
        if not any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles):
            await interaction.followup.send("❌ You don't have permission to use this command.", ephemeral=True)
            return

        # Validate parameters
        if season_number is None and draft_name is None:
            await interaction.followup.send(
                "❌ You must provide either `season_number` (for season-linked draft) OR `draft_name` (for manual draft)!",
                ephemeral=True
            )
            return

        if season_number is not None and draft_name is not None:
            await interaction.followup.send(
                "❌ Provide only ONE: either `season_number` OR `draft_name`, not both!",
                ephemeral=True
            )
            return

        if rounds < 1 or rounds > 10:
            await interaction.followup.send("❌ Number of rounds must be between 1 and 10!", ephemeral=True)
            return

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Determine draft name and season linkage
                if season_number is not None:
                    # Season-linked draft: auto-generate name
                    final_draft_name = f"Season {season_number - 1} National Draft"
                    linked_season = season_number

                    # Verify season exists
                    cursor = await db.execute(
                        "SELECT season_id FROM seasons WHERE season_number = ?",
                        (season_number,)
                    )
                    if not await cursor.fetchone():
                        await interaction.followup.send(
                            f"❌ Season {season_number} doesn't exist!",
                            ephemeral=True
                        )
                        return
                else:
                    # Manual draft: use provided name
                    final_draft_name = draft_name
                    linked_season = None

                # Check if draft already exists
                cursor = await db.execute("SELECT draft_id FROM drafts WHERE draft_name = ?", (final_draft_name,))
                if await cursor.fetchone():
                    await interaction.followup.send(
                        f"❌ A draft named '{final_draft_name}' already exists!",
                        ephemeral=True
                    )
                    return

                # Validate save_ladder_for_season if provided
                if save_ladder_for_season is not None:
                    cursor = await db.execute(
                        "SELECT season_id FROM seasons WHERE season_number = ?",
                        (save_ladder_for_season,)
                    )
                    if not await cursor.fetchone():
                        await interaction.followup.send(
                            f"❌ Season {save_ladder_for_season} does not exist!",
                            ephemeral=True
                        )
                        return

                # Get all teams
                cursor = await db.execute("SELECT team_id, team_name FROM teams ORDER BY team_name")
                teams = await cursor.fetchall()

                if not teams:
                    await interaction.followup.send("❌ No teams found!", ephemeral=True)
                    return

                # Send the view with Enter Ladder or Skip buttons
                view = LadderEntryStartView(teams, final_draft_name, rounds, rookie_contract_years, save_ladder_for_season, linked_season)

                draft_type = "Season-Linked" if linked_season else "Manual"
                message = f"📊 **Create Draft: {final_draft_name}**\n\n"
                message += f"**Type:** {draft_type}\n"
                if linked_season:
                    message += f"**Linked to:** Season {linked_season}\n"
                message += f"**Rounds:** {rounds}\n"
                message += f"**Rookie Contract:** {rookie_contract_years} years\n"
                if save_ladder_for_season:
                    message += f"**Save ladder as:** Season {save_ladder_for_season} ladder\n"
                message += f"\n**Choose an option:**\n"
                message += f"• **Enter Ladder Order** - Set pick order now (draft status: 'current')\n"
                message += f"• **Skip - Future Draft** - Create without ladder order (draft status: 'future')\n\n"
                message += f"*For future drafts, you can set the ladder order later using `/setdraftladder`*"

                await interaction.followup.send(message, view=view, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @app_commands.command(name="setdraftladder", description="[ADMIN] Set ladder order for a future draft")
    @app_commands.describe(draft_name="Name of the future draft to set ladder for")
    @app_commands.autocomplete(draft_name=draft_name_autocomplete)
    async def set_draft_ladder(self, interaction: discord.Interaction, draft_name: str):
        await interaction.response.defer(ephemeral=True)

        # Check if user has admin role
        if not any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles):
            await interaction.followup.send("❌ You don't have permission to use this command.", ephemeral=True)
            return

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get draft by name
                cursor = await db.execute(
                    "SELECT draft_id, status, rounds, season_number FROM drafts WHERE draft_name = ?",
                    (draft_name,)
                )
                draft_info = await cursor.fetchone()

                if not draft_info:
                    await interaction.followup.send(
                        f"❌ Draft '{draft_name}' not found!",
                        ephemeral=True
                    )
                    return

                draft_id, status, rounds, season_number = draft_info

                # Check if draft is 'future' status
                if status != 'future':
                    await interaction.followup.send(
                        f"❌ Draft '{draft_name}' already has ladder order set (status: {status})!\n"
                        f"You can only set ladder order for drafts with 'future' status.",
                        ephemeral=True
                    )
                    return

                # Get all teams
                cursor = await db.execute("SELECT team_id, team_name FROM teams ORDER BY team_name")
                teams = await cursor.fetchall()

                if not teams:
                    await interaction.followup.send("❌ No teams found!", ephemeral=True)
                    return

                # Send the ladder entry modal
                view = SetLadderView(teams, draft_id, draft_name, rounds, season_number)

                message = f"📊 **Set Ladder Order: {draft_name}**\n\n"
                message += f"**Current Status:** {status}\n"
                message += f"**Rounds:** {rounds}\n"
                if season_number:
                    message += f"**Linked to:** Season {season_number}\n"
                message += f"\nClick the button below to enter the ladder order.\n"
                message += f"You'll paste teams in order from 1st place to last place (one team per line).\n"
                message += f"The draft order will be the reverse of the ladder (last place picks first)."

                await interaction.followup.send(message, view=view, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @app_commands.command(name="draftorder", description="View the draft order")
    @app_commands.describe(draft_name="Optional: Name of the draft to view (defaults to latest)")
    @app_commands.autocomplete(draft_name=draft_name_autocomplete)
    async def draft_order(self, interaction: discord.Interaction, draft_name: str = None):
        await interaction.response.defer(ephemeral=True)

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # If no draft name provided, get the most recent current draft
                if draft_name is None:
                    cursor = await db.execute(
                        """SELECT draft_name
                           FROM drafts
                           WHERE status = 'current'
                           ORDER BY draft_id DESC
                           LIMIT 1"""
                    )
                    draft_result = await cursor.fetchone()
                    if not draft_result:
                        await interaction.followup.send(
                            "❌ No current drafts found!\n"
                            "Use `/setdraftladder` to set the order for a future draft."
                        )
                        return
                    draft_name = draft_result[0]

                # Verify this draft is current (has ladder set)
                cursor = await db.execute(
                    "SELECT status FROM drafts WHERE draft_name = ?",
                    (draft_name,)
                )
                draft_status = await cursor.fetchone()
                if not draft_status or draft_status[0] != 'current':
                    await interaction.followup.send(
                        f"❌ Draft '{draft_name}' is not a current draft (status: {draft_status[0] if draft_status else 'unknown'})!\n"
                        f"Only current drafts with ladder order set can be viewed.\n"
                        f"Use `/setdraftladder` to set the order for a future draft."
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
        except:
            return []

    async def pick_identifier_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for pick identifiers in a draft (pick number for current, origin for future)"""
        try:
            # Get the draft_name from the current interaction namespace
            draft_name = interaction.namespace.draft_name
            if not draft_name:
                return []

            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    """SELECT dp.pick_origin, dp.pick_number, dp.round_number, dp.current_team_id,
                              t_orig.team_name as original_team
                       FROM draft_picks dp
                       JOIN teams t_orig ON dp.original_team_id = t_orig.team_id
                       WHERE dp.draft_name = ?
                       ORDER BY
                         CASE WHEN dp.pick_number IS NULL THEN 1 ELSE 0 END,
                         dp.pick_number,
                         dp.round_number""",
                    (draft_name,)
                )
                picks = await cursor.fetchall()

                # Get team name for display
                choices = []
                for pick_origin, pick_number, round_number, current_team_id, original_team in picks:
                    cursor = await db.execute(
                        "SELECT team_name FROM teams WHERE team_id = ?",
                        (current_team_id,)
                    )
                    team_result = await cursor.fetchone()
                    current_team_name = team_result[0] if team_result else "Unknown"

                    if pick_number is not None:
                        # Current draft - use pick number as value
                        display_name = f"Pick #{pick_number} (owned by {current_team_name})"
                        value = str(pick_number)
                    else:
                        # Future draft - use pick origin as value, format with round suffix
                        round_suffix = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}.get(round_number, f"{round_number}th")
                        display_name = f"{original_team} {round_suffix} (owned by {current_team_name})"
                        value = pick_origin

                    if current.lower() in display_name.lower():
                        choices.append(app_commands.Choice(name=display_name, value=value))

                return choices[:25]
        except:
            return []

    @app_commands.command(name="transferpick", description="[ADMIN] Transfer a draft pick to another team")
    @app_commands.describe(
        draft_name="Name of the draft",
        pick="The pick to transfer (pick number for current drafts, origin for future drafts)",
        to_team="Team to transfer pick to"
    )
    @app_commands.autocomplete(draft_name=all_drafts_autocomplete, pick=pick_identifier_autocomplete, to_team=team_autocomplete)
    async def transfer_pick(self, interaction: discord.Interaction, draft_name: str, pick: str, to_team: str):
        await interaction.response.defer(ephemeral=True)

        # Check if user has admin role
        if not any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles):
            await interaction.followup.send("❌ You don't have permission to use this command.", ephemeral=True)
            return

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get target team
                cursor = await db.execute("SELECT team_id, team_name FROM teams WHERE LOWER(team_name) = LOWER(?)", (to_team,))
                team_data = await cursor.fetchone()
                if not team_data:
                    await interaction.followup.send(f"❌ Team '{to_team}' not found!", ephemeral=True)
                    return

                new_team_id, new_team_name = team_data

                # Try to determine if this is a pick number or pick origin
                # If it's all digits, treat as pick number, otherwise as pick origin
                pick_data = None
                if pick.isdigit():
                    # Current draft - search by pick number
                    cursor = await db.execute(
                        """SELECT dp.pick_id, dp.pick_number, dp.round_number, dp.pick_origin,
                                  ct.team_name, ot.team_name
                           FROM draft_picks dp
                           JOIN teams ct ON dp.current_team_id = ct.team_id
                           JOIN teams ot ON dp.original_team_id = ot.team_id
                           WHERE dp.draft_name = ? AND dp.pick_number = ?""",
                        (draft_name, int(pick))
                    )
                    pick_data = await cursor.fetchone()
                else:
                    # Future draft - search by pick origin
                    cursor = await db.execute(
                        """SELECT dp.pick_id, dp.pick_number, dp.round_number, dp.pick_origin,
                                  ct.team_name, ot.team_name
                           FROM draft_picks dp
                           JOIN teams ct ON dp.current_team_id = ct.team_id
                           JOIN teams ot ON dp.original_team_id = ot.team_id
                           WHERE dp.draft_name = ? AND dp.pick_origin = ?""",
                        (draft_name, pick)
                    )
                    pick_data = await cursor.fetchone()

                if not pick_data:
                    await interaction.followup.send(
                        f"❌ Pick '{pick}' not found in '{draft_name}'!",
                        ephemeral=True
                    )
                    return

                pick_id, pick_number, round_num, pick_origin, current_team, original_team = pick_data

                # Transfer the pick
                await db.execute(
                    "UPDATE draft_picks SET current_team_id = ? WHERE pick_id = ?",
                    (new_team_id, pick_id)
                )
                await db.commit()

                # Format display message
                if pick_number is not None:
                    pick_display = f"Pick #{pick_number}"
                else:
                    round_suffix = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}.get(round_num, f"{round_num}th")
                    pick_display = f"{original_team} {round_suffix}"

                await interaction.followup.send(
                    f"✅ **Pick Transferred!**\n\n"
                    f"**Draft:** {draft_name}\n"
                    f"**Pick:** {pick_display}\n"
                    f"**From:** {current_team}\n"
                    f"**To:** {new_team_name}",
                    ephemeral=True
                )

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

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
                team_emoji = self.bot.get_emoji(int(emoji_result[0]))

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
                            orig_emoji = self.bot.get_emoji(int(orig_emoji_id))
                        emoji_str = f"{orig_emoji} " if orig_emoji else ""
                        # Use season_num - 1 for display (draft naming convention)
                        all_pick_lines.append(f"Future {round_suffix} ({emoji_str}S{season_num - 1})")

            # Display all picks in embed description
            embed.description = "\n".join(all_pick_lines) if all_pick_lines else "*No picks*"

            await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="addpick", description="[ADMIN] Insert a pick into the draft order")
    @app_commands.describe(
        draft_name="Name of the draft",
        insert_at="Position to insert pick at (pushes others back)",
        team="Team that owns the pick",
        pick_origin="Origin description (e.g., 'Adelaide R1', 'Compensation Pick') - optional"
    )
    @app_commands.autocomplete(draft_name=draft_name_autocomplete, team=team_autocomplete)
    async def add_pick(
        self,
        interaction: discord.Interaction,
        draft_name: str,
        insert_at: int,
        team: str,
        pick_origin: str = None
    ):
        await interaction.response.defer(ephemeral=True)

        # Check if user has admin role
        if not any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles):
            await interaction.followup.send("❌ You don't have permission to use this command.", ephemeral=True)
            return

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get team
                cursor = await db.execute("SELECT team_id, team_name FROM teams WHERE LOWER(team_name) = LOWER(?)", (team,))
                team_data = await cursor.fetchone()
                if not team_data:
                    await interaction.followup.send(f"❌ Team '{team}' not found!", ephemeral=True)
                    return

                team_id, team_name = team_data

                # Determine round number based on the pick at insert_at position (or the one before)
                cursor = await db.execute(
                    """SELECT round_number FROM draft_picks
                       WHERE draft_name = ? AND pick_number >= ?
                       ORDER BY pick_number ASC LIMIT 1""",
                    (draft_name, insert_at)
                )
                pick_at_position = await cursor.fetchone()

                if pick_at_position:
                    round_number = pick_at_position[0]
                else:
                    # If no pick at or after this position, check the last pick
                    cursor = await db.execute(
                        """SELECT round_number FROM draft_picks
                           WHERE draft_name = ?
                           ORDER BY pick_number DESC LIMIT 1""",
                        (draft_name,)
                    )
                    last_pick = await cursor.fetchone()
                    round_number = last_pick[0] if last_pick else 1

                # Use pick_origin as-is (defaults to None/empty if not provided)
                if pick_origin is None:
                    pick_origin = ""

                # Get all picks that need to be shifted (we need to update them in reverse order)
                cursor = await db.execute(
                    """SELECT pick_id FROM draft_picks
                       WHERE draft_name = ? AND pick_number >= ?
                       ORDER BY pick_number DESC""",
                    (draft_name, insert_at)
                )
                picks_to_shift = await cursor.fetchall()

                # Shift picks one by one in reverse order to avoid conflicts
                for (pick_id,) in picks_to_shift:
                    await db.execute(
                        "UPDATE draft_picks SET pick_number = pick_number + 1 WHERE pick_id = ?",
                        (pick_id,)
                    )

                # Insert the new pick
                await db.execute(
                    """INSERT INTO draft_picks (draft_name, round_number, pick_number, pick_origin, current_team_id)
                       VALUES (?, ?, ?, ?, ?)""",
                    (draft_name, round_number, insert_at, pick_origin, team_id)
                )

                await db.commit()

                await interaction.followup.send(
                    f"✅ **Pick Added!**\n\n"
                    f"**Draft:** {draft_name}\n"
                    f"**Position:** #{insert_at}\n"
                    f"**Team:** {team_name}\n"
                    f"**Round:** {round_number}\n"
                    f"**Origin:** {pick_origin}\n\n"
                    f"All picks after #{insert_at} have been shifted back.",
                    ephemeral=True
                )

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @app_commands.command(name="removepick", description="[ADMIN] Remove a pick from the draft order")
    @app_commands.describe(
        draft_name="Name of the draft",
        pick_number="Overall pick number to remove"
    )
    @app_commands.autocomplete(draft_name=draft_name_autocomplete)
    async def remove_pick(self, interaction: discord.Interaction, draft_name: str, pick_number: int):
        await interaction.response.defer(ephemeral=True)

        # Check if user has admin role
        if not any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles):
            await interaction.followup.send("❌ You don't have permission to use this command.", ephemeral=True)
            return

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get the pick info
                cursor = await db.execute(
                    """SELECT dp.pick_id, dp.pick_origin, dp.round_number
                       FROM draft_picks dp
                       WHERE dp.draft_name = ? AND dp.pick_number = ?""",
                    (draft_name, pick_number)
                )
                pick_data = await cursor.fetchone()

                if not pick_data:
                    await interaction.followup.send(
                        f"❌ Pick #{pick_number} not found in '{draft_name}'!",
                        ephemeral=True
                    )
                    return

                pick_id, pick_origin, round_num = pick_data

                # Delete the pick
                await db.execute("DELETE FROM draft_picks WHERE pick_id = ?", (pick_id,))

                # Shift all picks after this one forward by 1
                await db.execute(
                    """UPDATE draft_picks
                       SET pick_number = pick_number - 1
                       WHERE draft_name = ? AND pick_number > ?""",
                    (draft_name, pick_number)
                )

                await db.commit()

                await interaction.followup.send(
                    f"✅ **Pick Removed!**\n\n"
                    f"**Draft:** {draft_name}\n"
                    f"**Position:** #{pick_number}\n"
                    f"**Description:** {pick_origin}\n\n"
                    f"All picks after #{pick_number} have been shifted forward.",
                    ephemeral=True
                )

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @app_commands.command(name="deletedraft", description="[ADMIN] Delete an entire draft")
    @app_commands.describe(draft_name="Name of the draft to delete")
    @app_commands.autocomplete(draft_name=draft_name_autocomplete)
    async def delete_draft(self, interaction: discord.Interaction, draft_name: str):
        await interaction.response.defer(ephemeral=True)

        # Check if user has admin role
        if not any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles):
            await interaction.followup.send("❌ You don't have permission to use this command.", ephemeral=True)
            return

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Check if draft exists
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM draft_picks WHERE draft_name = ?",
                    (draft_name,)
                )
                count = (await cursor.fetchone())[0]

                if count == 0:
                    await interaction.followup.send(
                        f"❌ No draft found with name '{draft_name}'!",
                        ephemeral=True
                    )
                    return

                # Delete all picks from this draft
                await db.execute("DELETE FROM draft_picks WHERE draft_name = ?", (draft_name,))
                await db.commit()

                await interaction.followup.send(
                    f"✅ **Draft Deleted!**\n\n"
                    f"**Draft:** {draft_name}\n"
                    f"**Picks Removed:** {count}",
                    ephemeral=True
                )

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @app_commands.command(name="startdraft", description="[ADMIN] Start a live draft")
    @app_commands.describe(draft_name="Name of the draft to start")
    @app_commands.autocomplete(draft_name=draft_name_autocomplete)
    async def start_draft(self, interaction: discord.Interaction, draft_name: str):
        await interaction.response.defer(ephemeral=True)

        # Check if user has admin role
        if not any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles):
            await interaction.followup.send("❌ You don't have permission to use this command.", ephemeral=True)
            return

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get draft info
                cursor = await db.execute(
                    "SELECT draft_id, status, rounds, season_number FROM drafts WHERE draft_name = ?",
                    (draft_name,)
                )
                draft_info = await cursor.fetchone()

                if not draft_info:
                    await interaction.followup.send(f"❌ Draft '{draft_name}' not found!", ephemeral=True)
                    return

                draft_id, status, rounds, season_number = draft_info

                # Check if draft is current (has ladder set)
                if status != 'current':
                    await interaction.followup.send(
                        f"❌ Draft '{draft_name}' is not ready to start (status: {status})!\n"
                        f"Use `/setdraftladder` to set the draft order first.",
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

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)
            import traceback
            traceback.print_exc()

    async def send_pick_notification(self, db, draft_id, draft_name, pick_number):
        """Send draft pick notification to team's channel"""
        try:
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
                try:
                    emoji = self.bot.get_emoji(int(emoji_result[0]))
                    team_emoji = str(emoji) + " " if emoji else ""
                except:
                    pass

            # Get draft channel
            cursor = await db.execute(
                "SELECT setting_value FROM settings WHERE setting_key = 'draft_channel_id'"
            )
            result = await cursor.fetchone()
            draft_channel_id = int(result[0]) if result and result[0] else None
            draft_channel = self.bot.get_channel(draft_channel_id) if draft_channel_id else None

            # Post "on the clock" message to draft channel
            if draft_channel:
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


class LadderEntryStartView(discord.ui.View):
    def __init__(self, teams, draft_name, rounds, rookie_contract_years, save_ladder_for_season, linked_season=None):
        super().__init__(timeout=300)
        self.teams = teams
        self.draft_name = draft_name
        self.rounds = rounds
        self.rookie_contract_years = rookie_contract_years
        self.save_ladder_for_season = save_ladder_for_season
        self.linked_season = linked_season

    @discord.ui.button(label="📝 Enter Ladder Order", style=discord.ButtonStyle.primary, row=0)
    async def enter_ladder_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = LadderEntryModal(self.teams, self.draft_name, self.rounds, self.rookie_contract_years, self.save_ladder_for_season, self.linked_season)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="⏭️ Skip - Future Draft", style=discord.ButtonStyle.secondary, row=0)
    async def skip_ladder_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Create draft in drafts table with status 'future'
                cursor = await db.execute(
                    """INSERT INTO drafts (draft_name, season_number, status, rounds, rookie_contract_years)
                       VALUES (?, ?, 'future', ?, ?)""",
                    (self.draft_name, self.linked_season, self.rounds, self.rookie_contract_years)
                )
                draft_id = cursor.lastrowid

                # Generate picks for all teams (pick_number = NULL for future drafts)
                for team_id, team_name in self.teams:
                    for round_num in range(1, self.rounds + 1):
                        pick_origin = f"{team_name} R{round_num}"
                        await db.execute(
                            """INSERT INTO draft_picks (draft_id, draft_name, season_number, round_number,
                                                        pick_number, pick_origin, original_team_id, current_team_id)
                               VALUES (?, ?, ?, ?, NULL, ?, ?, ?)""",
                            (draft_id, self.draft_name, self.linked_season, round_num, pick_origin, team_id, team_id)
                        )

                await db.commit()

                message = f"✅ **Future draft created: {self.draft_name}**\n\n"
                message += f"**Status:** Future (no ladder order set)\n"
                message += f"**Rounds:** {self.rounds}\n"
                if self.linked_season:
                    message += f"**Linked to:** Season {self.linked_season}\n"
                message += f"**Picks generated:** {len(self.teams) * self.rounds} picks\n\n"
                message += f"📌 These picks are now tradeable!\n"
                message += f"Use `/setdraftladder` to set the ladder order later."

                await interaction.followup.send(message, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Error creating future draft: {e}", ephemeral=True)


class LadderEntryModal(discord.ui.Modal):
    def __init__(self, teams, draft_name, rounds, rookie_contract_years, save_ladder_for_season, linked_season=None):
        super().__init__(title=f"Ladder Order: {draft_name[:30]}")
        self.teams = teams
        self.draft_name = draft_name
        self.rounds = rounds
        self.rookie_contract_years = rookie_contract_years
        self.save_ladder_for_season = save_ladder_for_season
        self.linked_season = linked_season

        # Create a map of team names (case insensitive) to team IDs
        self.team_map = {name.lower(): (tid, name) for tid, name in teams}

        self.ladder_input = discord.ui.TextInput(
            label="Ladder Order (1st to last, one per line)",
            style=discord.TextStyle.paragraph,
            placeholder="Adelaide\nBrisbane\nCarlton\nCollingwood\n...\n(Paste from spreadsheet or type)",
            required=True,
            max_length=2000
        )
        self.add_item(self.ladder_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            # Parse the input
            lines = [line.strip() for line in self.ladder_input.value.strip().split('\n') if line.strip()]

            if len(lines) != len(self.teams):
                await interaction.followup.send(
                    f"❌ Expected {len(self.teams)} teams, but got {len(lines)}!\n"
                    f"Please enter one team per line, from 1st place to last place.",
                    ephemeral=True
                )
                return

            # Match team names
            team_order = []
            errors = []
            for position, team_name in enumerate(lines, 1):
                team_lower = team_name.lower()
                if team_lower in self.team_map:
                    team_id, actual_name = self.team_map[team_lower]
                    team_order.append((team_id, actual_name, position))
                else:
                    errors.append(f"Position {position}: '{team_name}' not found")

            if errors:
                await interaction.followup.send(
                    f"❌ **Team name errors:**\n" + "\n".join(errors[:10]),
                    ephemeral=True
                )
                return

            # Check for duplicates
            team_ids_used = [tid for tid, _, _ in team_order]
            if len(team_ids_used) != len(set(team_ids_used)):
                await interaction.followup.send(
                    "❌ Duplicate teams detected! Each team should appear exactly once.",
                    ephemeral=True
                )
                return

            # Create the draft
            async with aiosqlite.connect(DB_PATH) as db:
                # Save ladder for season if requested
                if self.save_ladder_for_season is not None:
                    cursor = await db.execute("SELECT season_id FROM seasons WHERE season_number = ?", (self.save_ladder_for_season,))
                    season_data = await cursor.fetchone()
                    if season_data:
                        season_id = season_data[0]
                        # Delete existing ladder for this season
                        await db.execute("DELETE FROM ladder_positions WHERE season_id = ?", (season_id,))

                        # Insert new ladder positions
                        for team_id, team_name, position in team_order:
                            await db.execute(
                                "INSERT INTO ladder_positions (season_id, team_id, position) VALUES (?, ?, ?)",
                                (season_id, team_id, position)
                            )

                # Create draft in drafts table with status 'current' (ladder is set)
                cursor = await db.execute(
                    """INSERT INTO drafts (draft_name, season_number, status, rounds, rookie_contract_years, ladder_set_at)
                       VALUES (?, ?, 'current', ?, ?, CURRENT_TIMESTAMP)""",
                    (self.draft_name, self.linked_season, self.rounds, self.rookie_contract_years)
                )
                draft_id = cursor.lastrowid

                # Generate draft picks in reverse order (last place picks first)
                pick_counter = 1
                for round_num in range(1, self.rounds + 1):
                    for team_id, team_name, position in reversed(team_order):
                        pick_origin = f"{team_name} R{round_num}"
                        await db.execute(
                            """INSERT INTO draft_picks (draft_id, draft_name, season_number, round_number, pick_number,
                                                        pick_origin, original_team_id, current_team_id)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            (draft_id, self.draft_name, self.linked_season, round_num, pick_counter, pick_origin, team_id, team_id)
                        )
                        pick_counter += 1

                await db.commit()

                # Get first and last teams
                first_place_team = team_order[0][1]
                last_place_team = team_order[-1][1]

                total_picks = len(self.teams) * self.rounds
                response = f"✅ **Draft '{self.draft_name}' Created!**\n\n"
                response += f"**Ladder:**\n"
                response += f"  1st: {first_place_team}\n"
                response += f"  ...\n"
                response += f"  {len(team_order)}th: {last_place_team}\n\n"
                response += f"**Total Picks:** {total_picks} ({len(self.teams)} teams × {self.rounds} rounds)\n"
                response += f"**First pick:** {last_place_team} (last place)\n"
                if self.save_ladder_for_season:
                    response += f"**Ladder saved as:** Season {self.save_ladder_for_season} ladder\n"
                response += f"\nUse `/draftorder \"{self.draft_name}\"` to view the full draft order."

                await interaction.followup.send(response, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Error creating draft: {e}", ephemeral=True)


class SetLadderView(discord.ui.View):
    """View for setting ladder order on existing future draft"""
    def __init__(self, teams, draft_id, draft_name, rounds, season_number):
        super().__init__(timeout=300)
        self.teams = teams
        self.draft_id = draft_id
        self.draft_name = draft_name
        self.rounds = rounds
        self.season_number = season_number

    @discord.ui.button(label="📝 Enter Ladder Order", style=discord.ButtonStyle.primary)
    async def enter_ladder_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = SetLadderModal(self.teams, self.draft_id, self.draft_name, self.rounds, self.season_number)
        await interaction.response.send_modal(modal)


class SetLadderModal(discord.ui.Modal):
    """Modal for setting ladder order on existing future draft"""
    def __init__(self, teams, draft_id, draft_name, rounds, season_number):
        super().__init__(title=f"Set Ladder: {draft_name[:30]}")
        self.teams = teams
        self.draft_id = draft_id
        self.draft_name = draft_name
        self.rounds = rounds
        self.season_number = season_number

        # Create a map of team names (case insensitive) to team IDs
        self.team_map = {name.lower(): (tid, name) for tid, name in teams}

        self.ladder_input = discord.ui.TextInput(
            label="Ladder Order (1st to last, one per line)",
            style=discord.TextStyle.paragraph,
            placeholder="Adelaide\nBrisbane\nCarlton\nCollingwood\n...\n(Paste from spreadsheet or type)",
            required=True,
            max_length=2000
        )
        self.add_item(self.ladder_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            # Parse the input
            lines = [line.strip() for line in self.ladder_input.value.strip().split('\n') if line.strip()]

            if len(lines) != len(self.teams):
                await interaction.followup.send(
                    f"❌ Expected {len(self.teams)} teams, but got {len(lines)}!\n"
                    f"Please enter one team per line, from 1st place to last place.",
                    ephemeral=True
                )
                return

            # Match team names
            team_order = []
            errors = []
            for position, team_name in enumerate(lines, 1):
                team_lower = team_name.lower()
                if team_lower in self.team_map:
                    team_id, actual_name = self.team_map[team_lower]
                    team_order.append((team_id, actual_name, position))
                else:
                    errors.append(f"Position {position}: '{team_name}' not found")

            if errors:
                await interaction.followup.send(
                    f"❌ **Team name errors:**\n" + "\n".join(errors[:10]),
                    ephemeral=True
                )
                return

            # Check for duplicates
            team_ids_used = [tid for tid, _, _ in team_order]
            if len(team_ids_used) != len(set(team_ids_used)):
                await interaction.followup.send(
                    "❌ Duplicate teams detected! Each team should appear exactly once.",
                    ephemeral=True
                )
                return

            # Update the draft
            async with aiosqlite.connect(DB_PATH) as db:
                # Update draft status to 'current' and set ladder_set_at timestamp
                await db.execute(
                    """UPDATE drafts SET status = 'current', ladder_set_at = CURRENT_TIMESTAMP
                       WHERE draft_id = ?""",
                    (self.draft_id,)
                )

                # Delete all existing picks for this draft
                await db.execute("DELETE FROM draft_picks WHERE draft_id = ?", (self.draft_id,))

                # Save ladder positions if this draft is linked to a season
                if self.season_number is not None:
                    cursor = await db.execute("SELECT season_id FROM seasons WHERE season_number = ?", (self.season_number,))
                    season_data = await cursor.fetchone()
                    if season_data:
                        season_id = season_data[0]
                        # Delete existing ladder for this season
                        await db.execute("DELETE FROM ladder_positions WHERE season_id = ?", (season_id,))

                        # Insert new ladder positions
                        for team_id, team_name, position in team_order:
                            await db.execute(
                                "INSERT INTO ladder_positions (season_id, team_id, position) VALUES (?, ?, ?)",
                                (season_id, team_id, position)
                            )

                # Generate new picks with pick_number set, in reverse order (last place picks first)
                pick_counter = 1
                for round_num in range(1, self.rounds + 1):
                    for team_id, team_name, position in reversed(team_order):
                        pick_origin = f"{team_name} R{round_num}"
                        await db.execute(
                            """INSERT INTO draft_picks (draft_id, draft_name, season_number, round_number, pick_number,
                                                        pick_origin, original_team_id, current_team_id)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            (self.draft_id, self.draft_name, self.season_number, round_num, pick_counter, pick_origin, team_id, team_id)
                        )
                        pick_counter += 1

                await db.commit()

                # Get first and last teams
                first_place_team = team_order[0][1]
                last_place_team = team_order[-1][1]

                total_picks = len(self.teams) * self.rounds
                response = f"✅ **Ladder Order Set for '{self.draft_name}'!**\n\n"
                response += f"**Status:** Future → Current\n"
                response += f"**Ladder:**\n"
                response += f"  1st: {first_place_team}\n"
                response += f"  ...\n"
                response += f"  {len(team_order)}th: {last_place_team}\n\n"
                response += f"**Total Picks:** {total_picks} ({len(self.teams)} teams × {self.rounds} rounds)\n"
                response += f"**First pick:** {last_place_team} (last place)\n"
                response += f"\nUse `/draftorder \"{self.draft_name}\"` to view the full draft order."

                await interaction.followup.send(response, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Error setting ladder: {e}", ephemeral=True)


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
        if not emoji_id:
            return ""
        try:
            emoji = self.guild.get_emoji(int(emoji_id))
            return str(emoji) + " " if emoji else ""
        except:
            return ""

    def create_embed(self):
        embed = discord.Embed(
            title=f"{self.draft_name} - Round {self.current_round}",
            color=discord.Color.blue()
        )

        round_picks = self.picks_by_round.get(self.current_round, [])

        if not round_picks:
            embed.description = "No picks in this round"
            return embed

        description = ""
        for pick_num, round_num, pick_origin, current_team, current_emoji, player_selected in round_picks:
            # Get emoji for current team
            current_emoji_str = self.get_emoji(current_emoji)

            # Build pick - number, emoji, and origin
            pick_desc = f"**{pick_num}.** {current_emoji_str}"

            # Show pick origin (from pick_origin field)
            if pick_origin:
                pick_desc += f"*({pick_origin})*"

            # Show if player selected
            if player_selected:
                pick_desc += f"\n→ **{player_selected}**"

            description += pick_desc + "\n"

        embed.description = description
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
            try:
                emoji = self.bot.get_emoji(int(emoji_id))
                if emoji:
                    emoji_str = f"{emoji} "
            except:
                pass

        # Get ALL available players from Draft Pool
        cursor = await db.execute(
            """SELECT p.player_id, p.name, p.position, p.age
               FROM players p
               JOIN teams t ON p.team_id = t.team_id
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
        for player_id, name, pos, age in page_players:
            # Don't show OVR for draft pool players, add "yo" after age
            options.append(
                discord.SelectOption(
                    label=f"{name} ({pos}, {age} yo)",
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

    @discord.ui.select(placeholder="Select a player to draft...", min_values=0, max_values=1, custom_id="player_select")
    async def player_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        """Handle player selection"""
        if select.values:
            self.selected_player_id = int(select.values[0])
        else:
            self.selected_player_id = None

        await interaction.response.defer()

    @discord.ui.button(label="Confirm Selection", style=discord.ButtonStyle.primary)
    async def confirm_pick(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Confirm the draft pick"""
        if not self.selected_player_id:
            await interaction.response.send_message("❌ Please select a player first!", ephemeral=True)
            return

        await interaction.response.defer()

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

    @discord.ui.button(label="Pass Pick", style=discord.ButtonStyle.secondary)
    async def pass_pick(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Pass on this pick"""
        await interaction.response.defer()

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Process as a pass
                await self.process_pick(db, None, True)

                # Update the message
                await interaction.followup.send("✅ Pick passed!", ephemeral=True)
                await interaction.message.edit(view=None)  # Remove buttons

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @discord.ui.button(label="◀ Previous Page", style=discord.ButtonStyle.gray, custom_id="draft_prev_page", row=2)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Go to previous page"""
        if self.current_page > 0:
            self.current_page -= 1
            async with aiosqlite.connect(DB_PATH) as db:
                embed = await self.create_embed(db)
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="Next Page ▶", style=discord.ButtonStyle.gray, custom_id="draft_next_page", row=2)
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
            contract_expiry = season_number + rookie_years
            await db.execute(
                """UPDATE players
                   SET team_id = ?, contract_expiry = ?
                   WHERE player_id = ?""",
                (self.team_id, contract_expiry, player_id)
            )

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

        # Calculate which picks the father/son club needs to match
        matching_picks = await self.calculate_matching_picks(db, father_son_club_id, required_value)

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

        # Send match notification to father/son club
        if fs_channel_id:
            fs_channel = self.bot.get_channel(int(fs_channel_id))
            if fs_channel:
                # Create match notification view
                match_view = FatherSonMatchView(
                    self.bot, self.draft_id, self.draft_name, self.pick_number,
                    player_id, player_name, pos, age, ovr,
                    father_son_club_id, fs_team_name,
                    self.team_id, bidding_team_name,
                    bid_value, required_value, matching_picks
                )

                # Create embed
                embed = await match_view.create_embed(db)
                await fs_channel.send(embed=embed, view=match_view)

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
                await draft_channel.send(f"**-- ROUND {round_num} --**")

            # Get emojis
            bidding_emoji_str = ""
            if bidding_emoji_id:
                try:
                    emoji = self.bot.get_emoji(int(bidding_emoji_id))
                    if emoji:
                        bidding_emoji_str = f"{emoji} "
                except:
                    pass

            fs_emoji_str = ""
            if fs_emoji_id:
                try:
                    emoji = self.bot.get_emoji(int(fs_emoji_id))
                    if emoji:
                        fs_emoji_str = f"{emoji} "
                except:
                    pass

            message = f"**Pick {self.pick_number}:** {bidding_emoji_str}{bidding_team_name} bid on **{player_name.upper()}** ({pos}, {age} yo, {ovr} OVR) - F/S tied to {fs_emoji_str}{fs_team_name}"
            await draft_channel.send(message)

        except Exception as e:
            print(f"Error posting F/S bid to draft channel: {e}")

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
                try:
                    emoji = self.bot.get_emoji(int(emoji_id))
                    if emoji:
                        emoji_str = f"{emoji} "
                except:
                    pass

            # Check if this is the first pick of a new round (post round header)
            # Get the number of picks in each round
            cursor = await db.execute(
                "SELECT COUNT(*) FROM draft_picks WHERE draft_id = ? AND round_number = 1",
                (self.draft_id,)
            )
            picks_per_round = (await cursor.fetchone())[0]

            # Check if this pick number is the start of a new round
            if (pick_num - 1) % picks_per_round == 0:
                await draft_channel.send(f"**-- ROUND {round_num} --**")

            if is_pass:
                message = f"**Pick {pick_num}:** {emoji_str}{team_name} - PASS"
            else:
                # Get player info
                cursor = await db.execute(
                    "SELECT name, position, age, overall_rating FROM players WHERE player_id = ?",
                    (player_id,)
                )
                player_name, pos, age, ovr = await cursor.fetchone()

                message = f"**Pick {pick_num}:** {emoji_str}{team_name} select **{player_name.upper()}** ({pos}, {age} yo, {ovr} OVR)"

            await draft_channel.send(message)

        except Exception as e:
            print(f"Error posting to draft channel: {e}")


class FatherSonMatchView(discord.ui.View):
    """View for father/son club to match or pass on a bid"""
    def __init__(self, bot, draft_id, draft_name, bid_pick_number, player_id, player_name, pos, age, ovr,
                 fs_team_id, fs_team_name, bidding_team_id, bidding_team_name,
                 bid_value, required_value, matching_picks):
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

    async def create_embed(self, db):
        """Create the match notification embed"""
        embed = discord.Embed(
            title=f"⚠️ Father/Son Bid - {self.player_name}",
            description=f"**{self.bidding_team_name}** has bid on your father/son player with pick **#{self.bid_pick_number}**",
            color=discord.Color.orange()
        )

        embed.add_field(
            name="Player",
            value=f"**{self.player_name}** ({self.pos}, {self.age} yo, {self.ovr} OVR)",
            inline=False
        )

        embed.add_field(
            name="Bid Value",
            value=f"{self.bid_value} points (Pick #{self.bid_pick_number})",
            inline=True
        )

        embed.add_field(
            name="Required to Match",
            value=f"{self.required_value} points (80% discount)",
            inline=True
        )

        # Calculate total value of matching picks
        total_match_value = sum(p[3] for p in self.matching_picks)

        # Show which picks are needed to match
        if self.matching_picks:
            picks_text = ""
            for pick_num, round_num, origin, points in self.matching_picks:
                picks_text += f"• Pick #{pick_num} (R{round_num}, {points} pts)\n"
            picks_text += f"\n**Total: {total_match_value} points**"

            embed.add_field(
                name="Picks Needed to Match",
                value=picks_text,
                inline=False
            )

            if total_match_value < self.required_value:
                embed.add_field(
                    name="⚠️ Insufficient Points",
                    value=f"You don't have enough draft points to match this bid. The bidding team will automatically select {self.player_name}.",
                    inline=False
                )
                # Disable the Match button
                self.match_button.disabled = True
        else:
            embed.add_field(
                name="⚠️ No Available Picks",
                value=f"You have no picks remaining to match this bid. The bidding team will automatically select {self.player_name}.",
                inline=False
            )
            # Disable the Match button
            self.match_button.disabled = True

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

        # Step 1: Push back all picks from bid_pick_number onwards by 1
        # Do this in reverse order to avoid unique constraint issues
        cursor = await db.execute(
            """SELECT pick_number FROM draft_picks
               WHERE draft_id = ? AND pick_number >= ?
               ORDER BY pick_number DESC""",
            (self.draft_id, self.bid_pick_number)
        )
        picks_to_push = await cursor.fetchall()

        for (pick_num,) in picks_to_push:
            await db.execute(
                "UPDATE draft_picks SET pick_number = ? WHERE draft_id = ? AND pick_number = ?",
                (pick_num + 1, self.draft_id, pick_num)
            )

        # Step 2: Delete the consumed matching picks
        for pick_num, _, _, _ in self.matching_picks:
            # The pick numbers have been pushed back by 1, so add 1 to the pick number
            await db.execute(
                "DELETE FROM draft_picks WHERE draft_id = ? AND pick_number = ?",
                (self.draft_id, pick_num + 1)
            )

        # Step 3: Renumber all picks after deletions to fill gaps
        await self.renumber_picks_after_deletion(db)

        # Step 4: Insert new pick for father/son club at the bid position
        await db.execute(
            """INSERT INTO draft_picks (
                draft_id, draft_name, season_number, round_number, pick_number,
                pick_origin, original_team_id, current_team_id, player_selected_id, picked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (self.draft_id, draft_name, season_number, round_number, self.bid_pick_number,
             f"{self.fs_team_name} F/S Match", self.fs_team_id, self.fs_team_id, self.player_id)
        )

        # Step 5: Assign player to father/son club
        contract_expiry = season_number + rookie_years
        await db.execute(
            "UPDATE players SET team_id = ?, contract_expiry = ? WHERE player_id = ?",
            (self.fs_team_id, contract_expiry, self.player_id)
        )

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
        contract_expiry = season_number + rookie_years
        await db.execute(
            "UPDATE players SET team_id = ?, contract_expiry = ? WHERE player_id = ?",
            (self.bidding_team_id, contract_expiry, self.player_id)
        )

        await db.commit()

        # Post pass result to draft channel
        await self.post_match_result_to_draft_channel(db, matched=False)

        # Disable buttons and update message
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(content="❌ **Bid Passed** - Bidding team selects the player.", view=self)

        # Continue with next pick
        await self.continue_draft(db)

    async def renumber_picks_after_deletion(self, db):
        """Renumber all picks after picks are deleted, shifting everything forward"""
        # Get all remaining picks ordered by pick_number
        cursor = await db.execute(
            """SELECT pick_id, pick_number FROM draft_picks
               WHERE draft_id = ?
               ORDER BY pick_number ASC""",
            (self.draft_id,)
        )
        all_picks = await cursor.fetchall()

        # Renumber sequentially
        new_pick_number = 1
        for pick_id, old_pick_number in all_picks:
            if new_pick_number != old_pick_number:
                await db.execute(
                    "UPDATE draft_picks SET pick_number = ? WHERE pick_id = ?",
                    (new_pick_number, pick_id)
                )
            new_pick_number += 1

        await db.commit()

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

            if matched:
                # Show picks consumed
                picks_text = ", ".join([f"#{p[0]}" for p in self.matching_picks])
                message = f"🔄 **{self.fs_team_name}** matched the bid! Drafted **{self.player_name.upper()}** using picks: {picks_text}"
            else:
                message = f"✅ **{self.bidding_team_name}** select **{self.player_name.upper()}** ({self.pos}, {self.age} yo, {self.ovr} OVR) - F/S bid not matched"

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


async def setup(bot):
    await bot.add_cog(DraftCommands(bot))
