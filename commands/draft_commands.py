import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from config import DB_PATH, ADMIN_ROLE_ID

class DraftCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="enterladder", description="[ADMIN] Enter ladder positions for draft order")
    @app_commands.describe(season="Season number to set ladder for")
    async def enter_ladder(self, interaction: discord.Interaction, season: int):
        await interaction.response.defer(ephemeral=True)

        # Check if user has admin role
        if not any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles):
            await interaction.followup.send("❌ You don't have permission to use this command.", ephemeral=True)
            return

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Check if season exists
                cursor = await db.execute("SELECT season_id FROM seasons WHERE season_number = ?", (season,))
                season_data = await cursor.fetchone()
                if not season_data:
                    await interaction.followup.send(f"❌ Season {season} does not exist!", ephemeral=True)
                    return

                season_id = season_data[0]

                # Get all teams
                cursor = await db.execute("SELECT team_id, team_name FROM teams ORDER BY team_name")
                teams = await cursor.fetchall()

                if not teams:
                    await interaction.followup.send("❌ No teams found!", ephemeral=True)
                    return

                # Create the ladder entry view
                view = LadderEntryView(teams, season_id, season)
                await interaction.followup.send(
                    f"📊 **Enter Ladder for Season {season}**\n\n"
                    f"Use the dropdowns below to set each team's ladder position (1 = 1st place, higher = lower on ladder).\n"
                    f"The draft order will be the reverse of the ladder (last place picks first).",
                    view=view,
                    ephemeral=True
                )

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @app_commands.command(name="generatedraft", description="[ADMIN] Generate draft picks based on ladder")
    @app_commands.describe(season="Season number to generate draft for")
    async def generate_draft(self, interaction: discord.Interaction, season: int):
        await interaction.response.defer(ephemeral=True)

        # Check if user has admin role
        if not any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles):
            await interaction.followup.send("❌ You don't have permission to use this command.", ephemeral=True)
            return

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Check if season exists
                cursor = await db.execute("SELECT season_id FROM seasons WHERE season_number = ?", (season,))
                season_data = await cursor.fetchone()
                if not season_data:
                    await interaction.followup.send(f"❌ Season {season} does not exist!", ephemeral=True)
                    return

                season_id = season_data[0]

                # Check if ladder exists for this season
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM ladder_positions WHERE season_id = ?",
                    (season_id,)
                )
                ladder_count = (await cursor.fetchone())[0]

                if ladder_count == 0:
                    await interaction.followup.send(
                        f"❌ No ladder positions set for Season {season}!\n"
                        f"Use `/enterladder {season}` first.",
                        ephemeral=True
                    )
                    return

                # Delete existing draft picks for this season
                await db.execute("DELETE FROM draft_picks WHERE season_id = ?", (season_id,))

                # Get ladder order (reversed for draft - last place picks first)
                cursor = await db.execute(
                    """SELECT t.team_id, t.team_name
                       FROM ladder_positions lp
                       JOIN teams t ON lp.team_id = t.team_id
                       WHERE lp.season_id = ?
                       ORDER BY lp.position DESC""",
                    (season_id,)
                )
                teams_by_ladder = await cursor.fetchall()

                # Generate 4 rounds of picks
                pick_counter = 1
                for round_num in range(1, 5):  # 4 rounds
                    for team_id, team_name in teams_by_ladder:
                        await db.execute(
                            """INSERT INTO draft_picks (season_id, round_number, pick_number, original_team_id, current_team_id)
                               VALUES (?, ?, ?, ?, ?)""",
                            (season_id, round_num, pick_counter, team_id, team_id)
                        )
                        pick_counter += 1

                await db.commit()

                total_picks = len(teams_by_ladder) * 4
                await interaction.followup.send(
                    f"✅ **Draft Generated for Season {season}!**\n\n"
                    f"**Total Picks:** {total_picks} ({len(teams_by_ladder)} teams × 4 rounds)\n"
                    f"**Draft Order:** Based on reverse ladder (last place picks first)\n\n"
                    f"Use `/draftorder {season}` to view the draft order.",
                    ephemeral=True
                )

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @app_commands.command(name="draftorder", description="View the draft order for a season")
    @app_commands.describe(season="Season number to view draft for")
    async def draft_order(self, interaction: discord.Interaction, season: int):
        await interaction.response.defer()

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Check if season exists
                cursor = await db.execute("SELECT season_id FROM seasons WHERE season_number = ?", (season,))
                season_data = await cursor.fetchone()
                if not season_data:
                    await interaction.followup.send(f"❌ Season {season} does not exist!")
                    return

                season_id = season_data[0]

                # Get draft picks
                cursor = await db.execute(
                    """SELECT dp.pick_number, dp.round_number,
                              ot.team_name as original_team, ct.team_name as current_team,
                              p.name as player_selected
                       FROM draft_picks dp
                       JOIN teams ot ON dp.original_team_id = ot.team_id
                       JOIN teams ct ON dp.current_team_id = ct.team_id
                       LEFT JOIN players p ON dp.player_selected_id = p.player_id
                       WHERE dp.season_id = ?
                       ORDER BY dp.pick_number""",
                    (season_id,)
                )
                picks = await cursor.fetchall()

                if not picks:
                    await interaction.followup.send(
                        f"❌ No draft picks found for Season {season}!\n"
                        f"Use `/generatedraft {season}` to create draft picks."
                    )
                    return

                # Create paginated view
                view = DraftOrderView(picks, season)
                embed = view.create_embed()
                await interaction.followup.send(embed=embed, view=view)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}")

    @app_commands.command(name="transferpick", description="[ADMIN] Transfer a draft pick to another team")
    @app_commands.describe(
        season="Season number",
        pick_number="Overall pick number to transfer",
        to_team="Team to transfer pick to"
    )
    async def transfer_pick(self, interaction: discord.Interaction, season: int, pick_number: int, to_team: str):
        await interaction.response.defer(ephemeral=True)

        # Check if user has admin role
        if not any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles):
            await interaction.followup.send("❌ You don't have permission to use this command.", ephemeral=True)
            return

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get season ID
                cursor = await db.execute("SELECT season_id FROM seasons WHERE season_number = ?", (season,))
                season_data = await cursor.fetchone()
                if not season_data:
                    await interaction.followup.send(f"❌ Season {season} does not exist!", ephemeral=True)
                    return

                season_id = season_data[0]

                # Get target team
                cursor = await db.execute("SELECT team_id, team_name FROM teams WHERE LOWER(team_name) = LOWER(?)", (to_team,))
                team_data = await cursor.fetchone()
                if not team_data:
                    await interaction.followup.send(f"❌ Team '{to_team}' not found!", ephemeral=True)
                    return

                new_team_id, new_team_name = team_data

                # Get the pick
                cursor = await db.execute(
                    """SELECT dp.pick_id, ot.team_name, dp.round_number, ct.team_name
                       FROM draft_picks dp
                       JOIN teams ot ON dp.original_team_id = ot.team_id
                       JOIN teams ct ON dp.current_team_id = ct.team_id
                       WHERE dp.season_id = ? AND dp.pick_number = ?""",
                    (season_id, pick_number)
                )
                pick_data = await cursor.fetchone()

                if not pick_data:
                    await interaction.followup.send(
                        f"❌ Pick #{pick_number} not found for Season {season}!",
                        ephemeral=True
                    )
                    return

                pick_id, original_team, round_num, current_team = pick_data

                # Transfer the pick
                await db.execute(
                    "UPDATE draft_picks SET current_team_id = ? WHERE pick_id = ?",
                    (new_team_id, pick_id)
                )
                await db.commit()

                await interaction.followup.send(
                    f"✅ **Pick Transferred!**\n\n"
                    f"**Pick:** #{pick_number} ({original_team} Round {round_num})\n"
                    f"**From:** {current_team}\n"
                    f"**To:** {new_team_name}",
                    ephemeral=True
                )

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @app_commands.command(name="addpick", description="[ADMIN] Insert a pick into the draft order")
    @app_commands.describe(
        season="Season number",
        insert_at="Position to insert pick at (pushes others back)",
        team="Team that owns the pick",
        round_number="Draft round (1-4)",
        description="Optional custom description (defaults to 'Team Round X')"
    )
    async def add_pick(
        self,
        interaction: discord.Interaction,
        season: int,
        insert_at: int,
        team: str,
        round_number: int,
        description: str = None
    ):
        await interaction.response.defer(ephemeral=True)

        # Check if user has admin role
        if not any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles):
            await interaction.followup.send("❌ You don't have permission to use this command.", ephemeral=True)
            return

        if round_number < 1 or round_number > 4:
            await interaction.followup.send("❌ Round number must be between 1 and 4!", ephemeral=True)
            return

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get season ID
                cursor = await db.execute("SELECT season_id FROM seasons WHERE season_number = ?", (season,))
                season_data = await cursor.fetchone()
                if not season_data:
                    await interaction.followup.send(f"❌ Season {season} does not exist!", ephemeral=True)
                    return

                season_id = season_data[0]

                # Get team
                cursor = await db.execute("SELECT team_id, team_name FROM teams WHERE LOWER(team_name) = LOWER(?)", (team,))
                team_data = await cursor.fetchone()
                if not team_data:
                    await interaction.followup.send(f"❌ Team '{team}' not found!", ephemeral=True)
                    return

                team_id, team_name = team_data

                # Shift all picks at or after insert position back by 1
                await db.execute(
                    """UPDATE draft_picks
                       SET pick_number = pick_number + 1
                       WHERE season_id = ? AND pick_number >= ?
                       ORDER BY pick_number DESC""",
                    (season_id, insert_at)
                )

                # Insert the new pick
                await db.execute(
                    """INSERT INTO draft_picks (season_id, round_number, pick_number, original_team_id, current_team_id)
                       VALUES (?, ?, ?, ?, ?)""",
                    (season_id, round_number, insert_at, team_id, team_id)
                )

                await db.commit()

                pick_desc = description if description else f"{team_name} Round {round_number}"

                await interaction.followup.send(
                    f"✅ **Pick Added!**\n\n"
                    f"**Position:** #{insert_at}\n"
                    f"**Description:** {pick_desc}\n"
                    f"**Team:** {team_name}\n"
                    f"**Round:** {round_number}\n\n"
                    f"All picks after #{insert_at} have been shifted back.",
                    ephemeral=True
                )

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @app_commands.command(name="removepick", description="[ADMIN] Remove a pick from the draft order")
    @app_commands.describe(
        season="Season number",
        pick_number="Overall pick number to remove"
    )
    async def remove_pick(self, interaction: discord.Interaction, season: int, pick_number: int):
        await interaction.response.defer(ephemeral=True)

        # Check if user has admin role
        if not any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles):
            await interaction.followup.send("❌ You don't have permission to use this command.", ephemeral=True)
            return

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get season ID
                cursor = await db.execute("SELECT season_id FROM seasons WHERE season_number = ?", (season,))
                season_data = await cursor.fetchone()
                if not season_data:
                    await interaction.followup.send(f"❌ Season {season} does not exist!", ephemeral=True)
                    return

                season_id = season_data[0]

                # Get the pick info
                cursor = await db.execute(
                    """SELECT dp.pick_id, ot.team_name, dp.round_number
                       FROM draft_picks dp
                       JOIN teams ot ON dp.original_team_id = ot.team_id
                       WHERE dp.season_id = ? AND dp.pick_number = ?""",
                    (season_id, pick_number)
                )
                pick_data = await cursor.fetchone()

                if not pick_data:
                    await interaction.followup.send(
                        f"❌ Pick #{pick_number} not found for Season {season}!",
                        ephemeral=True
                    )
                    return

                pick_id, original_team, round_num = pick_data

                # Delete the pick
                await db.execute("DELETE FROM draft_picks WHERE pick_id = ?", (pick_id,))

                # Shift all picks after this one forward by 1
                await db.execute(
                    """UPDATE draft_picks
                       SET pick_number = pick_number - 1
                       WHERE season_id = ? AND pick_number > ?""",
                    (season_id, pick_number)
                )

                await db.commit()

                await interaction.followup.send(
                    f"✅ **Pick Removed!**\n\n"
                    f"**Position:** #{pick_number}\n"
                    f"**Description:** {original_team} Round {round_num}\n\n"
                    f"All picks after #{pick_number} have been shifted forward.",
                    ephemeral=True
                )

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


class LadderEntryView(discord.ui.View):
    def __init__(self, teams, season_id, season_number):
        super().__init__(timeout=300)
        self.teams = teams
        self.season_id = season_id
        self.season_number = season_number
        self.ladder_positions = {}  # team_id: position

        # Create dropdowns (max 25 per select, so we might need multiple)
        teams_per_dropdown = 25
        for i in range(0, len(teams), teams_per_dropdown):
            chunk = teams[i:i + teams_per_dropdown]
            select = LadderPositionSelect(chunk, self, i // teams_per_dropdown)
            self.add_item(select)

    @discord.ui.button(label="Save Ladder", style=discord.ButtonStyle.green, row=4)
    async def save_ladder(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        # Check if all teams have positions
        if len(self.ladder_positions) != len(self.teams):
            await interaction.followup.send(
                f"❌ Please set positions for all {len(self.teams)} teams! "
                f"(Currently set: {len(self.ladder_positions)})",
                ephemeral=True
            )
            return

        # Check for duplicate positions
        positions = list(self.ladder_positions.values())
        if len(positions) != len(set(positions)):
            await interaction.followup.send("❌ Duplicate positions detected! Each team must have a unique position.", ephemeral=True)
            return

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Delete existing ladder for this season
                await db.execute("DELETE FROM ladder_positions WHERE season_id = ?", (self.season_id,))

                # Insert new ladder positions
                for team_id, position in self.ladder_positions.items():
                    await db.execute(
                        "INSERT INTO ladder_positions (season_id, team_id, position) VALUES (?, ?, ?)",
                        (self.season_id, team_id, position)
                    )

                await db.commit()

            await interaction.followup.send(
                f"✅ **Ladder saved for Season {self.season_number}!**\n\n"
                f"Use `/generatedraft {self.season_number}` to generate draft picks based on this ladder.",
                ephemeral=True
            )

            # Disable all components
            for item in self.children:
                item.disabled = True
            await interaction.message.edit(view=self)

        except Exception as e:
            await interaction.followup.send(f"❌ Error saving ladder: {e}", ephemeral=True)


class LadderPositionSelect(discord.ui.Select):
    def __init__(self, teams, parent_view, dropdown_index):
        self.parent_view = parent_view

        options = []
        for team_id, team_name in teams:
            options.append(discord.SelectOption(
                label=team_name,
                value=str(team_id),
                description="Click to set ladder position"
            ))

        super().__init__(
            placeholder=f"Select team to set position ({dropdown_index + 1})",
            options=options,
            row=dropdown_index
        )

    async def callback(self, interaction: discord.Interaction):
        team_id = int(self.values[0])
        team_name = next(name for tid, name in self.parent_view.teams if tid == team_id)

        # Create modal for position entry
        modal = LadderPositionModal(team_id, team_name, self.parent_view)
        await interaction.response.send_modal(modal)


class LadderPositionModal(discord.ui.Modal):
    def __init__(self, team_id, team_name, parent_view):
        super().__init__(title=f"Set Position for {team_name}")
        self.team_id = team_id
        self.team_name = team_name
        self.parent_view = parent_view

        self.position_input = discord.ui.TextInput(
            label="Ladder Position",
            placeholder=f"Enter position (1 to {len(parent_view.teams)})",
            required=True,
            max_length=2
        )
        self.add_item(self.position_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            position = int(self.position_input.value)
            if position < 1 or position > len(self.parent_view.teams):
                await interaction.response.send_message(
                    f"❌ Position must be between 1 and {len(self.parent_view.teams)}!",
                    ephemeral=True
                )
                return

            self.parent_view.ladder_positions[self.team_id] = position
            await interaction.response.send_message(
                f"✅ Set **{self.team_name}** to position **{position}**\n"
                f"({len(self.parent_view.ladder_positions)}/{len(self.parent_view.teams)} teams positioned)",
                ephemeral=True
            )

        except ValueError:
            await interaction.response.send_message("❌ Please enter a valid number!", ephemeral=True)


class DraftOrderView(discord.ui.View):
    def __init__(self, picks, season):
        super().__init__(timeout=180)
        self.picks = picks
        self.season = season
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

    def create_embed(self):
        embed = discord.Embed(
            title=f"🎯 Season {self.season} Draft Order - Round {self.current_round}",
            color=discord.Color.blue()
        )

        round_picks = self.picks_by_round.get(self.current_round, [])

        if not round_picks:
            embed.description = "No picks in this round"
            return embed

        description = ""
        for pick_num, round_num, original_team, current_team, player_selected in round_picks:
            # Build pick description
            pick_desc = f"**Pick {pick_num}:** {original_team} Round {round_num}"

            # Show if traded
            if original_team != current_team:
                pick_desc += f" *(traded to {current_team})*"

            # Show if player selected
            if player_selected:
                pick_desc += f" → **{player_selected}**"

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
