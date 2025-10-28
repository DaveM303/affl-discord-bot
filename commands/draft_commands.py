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
        """Autocomplete for draft names"""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "SELECT DISTINCT draft_name FROM draft_picks ORDER BY draft_name DESC"
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

    @app_commands.command(name="createdraft", description="[ADMIN] Create a draft by setting ladder order")
    @app_commands.describe(
        draft_name="Name for this draft (e.g. 'Season 9 Draft', 'Mid-Season Draft')",
        rounds="Number of rounds (default: 4)",
        save_ladder_for_season="Optional: Season number to save this ladder for (for historical records)"
    )
    async def create_draft(self, interaction: discord.Interaction, draft_name: str, rounds: int = 4, save_ladder_for_season: int = None):
        await interaction.response.defer(ephemeral=True)

        # Check if user has admin role
        if not any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles):
            await interaction.followup.send("❌ You don't have permission to use this command.", ephemeral=True)
            return

        if rounds < 1 or rounds > 10:
            await interaction.followup.send("❌ Number of rounds must be between 1 and 10!", ephemeral=True)
            return

        # Validate save_ladder_for_season if provided
        if save_ladder_for_season is not None:
            try:
                async with aiosqlite.connect(DB_PATH) as db:
                    cursor = await db.execute("SELECT season_id FROM seasons WHERE season_number = ?", (save_ladder_for_season,))
                    season_data = await cursor.fetchone()
                    if not season_data:
                        await interaction.followup.send(f"❌ Season {save_ladder_for_season} does not exist!", ephemeral=True)
                        return
            except Exception as e:
                await interaction.followup.send(f"❌ Error validating season: {e}", ephemeral=True)
                return

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Check if draft name already exists
                cursor = await db.execute("SELECT COUNT(*) FROM draft_picks WHERE draft_name = ?", (draft_name,))
                existing_count = (await cursor.fetchone())[0]
                if existing_count > 0:
                    await interaction.followup.send(
                        f"❌ A draft named '{draft_name}' already exists!\n"
                        f"Please choose a different name.",
                        ephemeral=True
                    )
                    return

                # Get all teams
                cursor = await db.execute("SELECT team_id, team_name FROM teams ORDER BY team_name")
                teams = await cursor.fetchall()

                if not teams:
                    await interaction.followup.send("❌ No teams found!", ephemeral=True)
                    return

                # Send the modal via a button
                view = LadderEntryStartView(teams, draft_name, rounds, save_ladder_for_season)
                await interaction.followup.send(
                    f"📊 **Create Draft: {draft_name}**\n\n"
                    f"**Rounds:** {rounds}\n"
                    f"{'**Save ladder as:** Season ' + str(save_ladder_for_season) + ' ladder' if save_ladder_for_season else ''}\n\n"
                    f"Click the button below to enter the ladder order.\n"
                    f"You'll paste teams in order from 1st place to last place (one team per line).\n"
                    f"The draft order will be the reverse of the ladder (last place picks first).",
                    view=view,
                    ephemeral=True
                )

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @app_commands.command(name="draftorder", description="View the draft order")
    @app_commands.describe(draft_name="Optional: Name of the draft to view (defaults to latest)")
    @app_commands.autocomplete(draft_name=draft_name_autocomplete)
    async def draft_order(self, interaction: discord.Interaction, draft_name: str = None):
        await interaction.response.defer(ephemeral=True)

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # If no draft name provided, get the most recent draft
                if draft_name is None:
                    cursor = await db.execute(
                        """SELECT DISTINCT draft_name
                           FROM draft_picks
                           ORDER BY pick_id DESC
                           LIMIT 1"""
                    )
                    draft_result = await cursor.fetchone()
                    if not draft_result:
                        await interaction.followup.send(
                            "❌ No drafts found!\n"
                            "Use `/createdraft` to create a draft."
                        )
                        return
                    draft_name = draft_result[0]

                # Get draft picks with team emojis
                cursor = await db.execute(
                    """SELECT dp.pick_number, dp.round_number,
                              dp.pick_origin,
                              ct.team_name as current_team, ct.emoji_id as current_emoji,
                              p.name as player_selected
                       FROM draft_picks dp
                       JOIN teams ct ON dp.current_team_id = ct.team_id
                       LEFT JOIN players p ON dp.player_selected_id = p.player_id
                       WHERE dp.draft_name = ?
                       ORDER BY dp.pick_number""",
                    (draft_name,)
                )
                picks = await cursor.fetchall()

                if not picks:
                    await interaction.followup.send(
                        f"❌ No draft picks found for '{draft_name}'!\n"
                        f"Use `/createdraft` to create a draft."
                    )
                    return

                # Create paginated view
                view = DraftOrderView(picks, draft_name, interaction.guild)
                embed = view.create_embed()
                await interaction.followup.send(embed=embed, view=view)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}")

    @app_commands.command(name="transferpick", description="[ADMIN] Transfer a draft pick to another team")
    @app_commands.describe(
        draft_name="Name of the draft",
        pick_number="Overall pick number to transfer",
        to_team="Team to transfer pick to"
    )
    @app_commands.autocomplete(draft_name=draft_name_autocomplete, to_team=team_autocomplete)
    async def transfer_pick(self, interaction: discord.Interaction, draft_name: str, pick_number: int, to_team: str):
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
                    """SELECT dp.pick_id, dp.pick_origin, dp.round_number, ct.team_name
                       FROM draft_picks dp
                       JOIN teams ct ON dp.current_team_id = ct.team_id
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

                pick_id, pick_origin, round_num, current_team = pick_data

                # Transfer the pick
                await db.execute(
                    "UPDATE draft_picks SET current_team_id = ? WHERE pick_id = ?",
                    (new_team_id, pick_id)
                )
                await db.commit()

                await interaction.followup.send(
                    f"✅ **Pick Transferred!**\n\n"
                    f"**Draft:** {draft_name}\n"
                    f"**Pick:** #{pick_number} ({pick_origin})\n"
                    f"**From:** {current_team}\n"
                    f"**To:** {new_team_name}",
                    ephemeral=True
                )

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @app_commands.command(name="addpick", description="[ADMIN] Insert a pick into the draft order")
    @app_commands.describe(
        draft_name="Name of the draft",
        insert_at="Position to insert pick at (pushes others back)",
        team="Team that owns the pick",
        round_number="Draft round",
        pick_origin="Origin description (e.g., 'Adelaide R1', 'Compensation Pick')"
    )
    @app_commands.autocomplete(draft_name=draft_name_autocomplete, team=team_autocomplete)
    async def add_pick(
        self,
        interaction: discord.Interaction,
        draft_name: str,
        insert_at: int,
        team: str,
        round_number: int,
        pick_origin: str = None
    ):
        await interaction.response.defer(ephemeral=True)

        # Check if user has admin role
        if not any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles):
            await interaction.followup.send("❌ You don't have permission to use this command.", ephemeral=True)
            return

        if round_number < 1:
            await interaction.followup.send("❌ Round number must be at least 1!", ephemeral=True)
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

                # Default pick_origin if not provided
                if not pick_origin:
                    pick_origin = f"{team_name} R{round_number}"

                # Shift all picks at or after insert position back by 1
                await db.execute(
                    """UPDATE draft_picks
                       SET pick_number = pick_number + 1
                       WHERE draft_name = ? AND pick_number >= ?
                       ORDER BY pick_number DESC""",
                    (draft_name, insert_at)
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
    def __init__(self, teams, draft_name, rounds, save_ladder_for_season):
        super().__init__(timeout=300)
        self.teams = teams
        self.draft_name = draft_name
        self.rounds = rounds
        self.save_ladder_for_season = save_ladder_for_season

    @discord.ui.button(label="📝 Enter Ladder Order", style=discord.ButtonStyle.primary)
    async def enter_ladder_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = LadderEntryModal(self.teams, self.draft_name, self.rounds, self.save_ladder_for_season)
        await interaction.response.send_modal(modal)


class LadderEntryModal(discord.ui.Modal):
    def __init__(self, teams, draft_name, rounds, save_ladder_for_season):
        super().__init__(title=f"Ladder Order: {draft_name[:30]}")
        self.teams = teams
        self.draft_name = draft_name
        self.rounds = rounds
        self.save_ladder_for_season = save_ladder_for_season

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

                # Generate draft picks in reverse order (last place picks first)
                pick_counter = 1
                for round_num in range(1, self.rounds + 1):
                    for team_id, team_name, position in reversed(team_order):
                        pick_origin = f"{team_name} R{round_num}"
                        await db.execute(
                            """INSERT INTO draft_picks (draft_name, round_number, pick_number, pick_origin, current_team_id)
                               VALUES (?, ?, ?, ?, ?)""",
                            (self.draft_name, round_num, pick_counter, pick_origin, team_id)
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
