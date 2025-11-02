import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from config import DB_PATH, ADMIN_ROLE_ID

class DraftCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

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
        save_ladder_for_season="Optional: Season number to save this ladder for (for historical records)"
    )
    async def create_draft(self, interaction: discord.Interaction, season_number: int = None, draft_name: str = None, rounds: int = 4, save_ladder_for_season: int = None):
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
                view = LadderEntryStartView(teams, final_draft_name, rounds, save_ladder_for_season, linked_season)

                draft_type = "Season-Linked" if linked_season else "Manual"
                message = f"📊 **Create Draft: {final_draft_name}**\n\n"
                message += f"**Type:** {draft_type}\n"
                if linked_season:
                    message += f"**Linked to:** Season {linked_season}\n"
                message += f"**Rounds:** {rounds}\n"
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

    async def pick_origin_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for pick origins in a draft"""
        try:
            # Get the draft_name from the current interaction namespace
            draft_name = interaction.namespace.draft_name
            if not draft_name:
                return []

            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    """SELECT pick_origin, pick_number, round_number, current_team_id
                       FROM draft_picks
                       WHERE draft_name = ?
                       ORDER BY
                         CASE WHEN pick_number IS NULL THEN 1 ELSE 0 END,
                         pick_number,
                         round_number""",
                    (draft_name,)
                )
                picks = await cursor.fetchall()

                # Get team name for display
                choices = []
                for pick_origin, pick_number, round_number, current_team_id in picks:
                    cursor = await db.execute(
                        "SELECT team_name FROM teams WHERE team_id = ?",
                        (current_team_id,)
                    )
                    team_result = await cursor.fetchone()
                    team_name = team_result[0] if team_result else "Unknown"

                    if pick_number is not None:
                        # Current pick with number
                        display_name = f"Pick #{pick_number} - {pick_origin} (currently {team_name})"
                    else:
                        # Future pick without number
                        display_name = f"{pick_origin} - Round {round_number} (currently {team_name})"

                    if current.lower() in display_name.lower():
                        choices.append(app_commands.Choice(name=display_name, value=pick_origin))

                return choices[:25]
        except:
            return []

    @app_commands.command(name="transferpick", description="[ADMIN] Transfer a draft pick to another team")
    @app_commands.describe(
        draft_name="Name of the draft",
        pick_origin="The pick to transfer (e.g., 'Adelaide R1')",
        to_team="Team to transfer pick to"
    )
    @app_commands.autocomplete(draft_name=all_drafts_autocomplete, pick_origin=pick_origin_autocomplete, to_team=team_autocomplete)
    async def transfer_pick(self, interaction: discord.Interaction, draft_name: str, pick_origin: str, to_team: str):
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

                # Get the pick
                cursor = await db.execute(
                    """SELECT dp.pick_id, dp.pick_number, dp.round_number, ct.team_name
                       FROM draft_picks dp
                       JOIN teams ct ON dp.current_team_id = ct.team_id
                       WHERE dp.draft_name = ? AND dp.pick_origin = ?""",
                    (draft_name, pick_origin)
                )
                pick_data = await cursor.fetchone()

                if not pick_data:
                    await interaction.followup.send(
                        f"❌ Pick '{pick_origin}' not found in '{draft_name}'!",
                        ephemeral=True
                    )
                    return

                pick_id, pick_number, round_num, current_team = pick_data

                # Transfer the pick
                await db.execute(
                    "UPDATE draft_picks SET current_team_id = ? WHERE pick_id = ?",
                    (new_team_id, pick_id)
                )
                await db.commit()

                # Format display message
                if pick_number is not None:
                    pick_display = f"#{pick_number} ({pick_origin})"
                else:
                    pick_display = f"{pick_origin} - Round {round_num}"

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
                title=f"{team_emoji_str}{target_team_name}'s Draft Hand",
                color=discord.Color.blue()
            )

            # Add picks for each season
            for season_num in sorted(picks_by_season.keys(), key=lambda x: (x is None, x)):
                picks = picks_by_season[season_num]
                pick_lines = []

                for pick_num, round_num, pick_origin, orig_emoji_id, draft_name in picks:
                    if pick_num is not None:
                        # Current pick with number
                        pick_lines.append(f"Pick #{pick_num}")
                    else:
                        # Future pick - format as "Future 1st ([emoji] S10)"
                        round_suffix = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}.get(round_num, f"{round_num}th")
                        orig_emoji = None
                        if orig_emoji_id:
                            orig_emoji = self.bot.get_emoji(int(orig_emoji_id))
                        emoji_str = f"{orig_emoji} " if orig_emoji else ""
                        # Use season_num - 1 for display (draft naming convention)
                        pick_lines.append(f"Future {round_suffix} ({emoji_str}S{season_num - 1})")

                # Create field for this season
                if season_num is None:
                    season_header = "Unknown Season"
                else:
                    season_header = f"Season {season_num}"

                embed.add_field(
                    name=season_header,
                    value="\n".join(pick_lines) if pick_lines else "*No picks*",
                    inline=False
                )

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


class LadderEntryStartView(discord.ui.View):
    def __init__(self, teams, draft_name, rounds, save_ladder_for_season, linked_season=None):
        super().__init__(timeout=300)
        self.teams = teams
        self.draft_name = draft_name
        self.rounds = rounds
        self.save_ladder_for_season = save_ladder_for_season
        self.linked_season = linked_season

    @discord.ui.button(label="📝 Enter Ladder Order", style=discord.ButtonStyle.primary, row=0)
    async def enter_ladder_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = LadderEntryModal(self.teams, self.draft_name, self.rounds, self.save_ladder_for_season, self.linked_season)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="⏭️ Skip - Future Draft", style=discord.ButtonStyle.secondary, row=0)
    async def skip_ladder_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Create draft in drafts table with status 'future'
                cursor = await db.execute(
                    """INSERT INTO drafts (draft_name, season_number, status, rounds)
                       VALUES (?, ?, 'future', ?)""",
                    (self.draft_name, self.linked_season, self.rounds)
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
    def __init__(self, teams, draft_name, rounds, save_ladder_for_season, linked_season=None):
        super().__init__(title=f"Ladder Order: {draft_name[:30]}")
        self.teams = teams
        self.draft_name = draft_name
        self.rounds = rounds
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
                    """INSERT INTO drafts (draft_name, season_number, status, rounds, ladder_set_at)
                       VALUES (?, ?, 'current', ?, CURRENT_TIMESTAMP)""",
                    (self.draft_name, self.linked_season, self.rounds)
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
            pick_desc = f"**{pick_num}.** {current_emoji_str}{pick_origin}"

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


async def setup(bot):
    await bot.add_cog(DraftCommands(bot))
