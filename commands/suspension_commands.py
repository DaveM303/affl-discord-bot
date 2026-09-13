import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from config import DB_PATH, ADMIN_ROLE_ID
from utils import is_admin_user

class SuspensionCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def player_name_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for player names with format: Team Name (POS, age, OVR)"""
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                """SELECT p.player_id, p.name, p.position, p.age, p.overall_rating, t.team_name
                   FROM players p
                   LEFT JOIN teams t ON p.team_id = t.team_id
                   ORDER BY p.name"""
            )
            players = await cursor.fetchall()

        # Filter players based on what the user has typed
        choices = []
        for player_id, name, position, age, rating, team_name in players:
            # Check if current input matches player name
            if current.lower() in name.lower():
                # Format: Team Name (POS, age yo, OVR)
                team_prefix = team_name if team_name else "Delisted"
                display_name = f"{name} ({team_prefix}, {position}, {age}yo, {rating} OVR)"

                # Value is player_id so we can query by ID later
                choices.append(app_commands.Choice(name=display_name, value=str(player_id)))

        # Return up to 25 choices (Discord limit)
        return choices[:25]

    async def suspended_player_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for currently suspended players only"""
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                """SELECT p.player_id, p.name, p.position, p.age, p.overall_rating, t.team_name
                   FROM players p
                   LEFT JOIN teams t ON p.team_id = t.team_id
                   INNER JOIN suspensions s ON p.player_id = s.player_id
                   WHERE s.status = 'suspended'
                   ORDER BY p.name"""
            )
            players = await cursor.fetchall()

        # Filter players based on what the user has typed
        choices = []
        for player_id, name, position, age, rating, team_name in players:
            # Check if current input matches player name
            if current.lower() in name.lower():
                # Format: Team Name (POS, age yo, OVR)
                team_prefix = team_name if team_name else "Delisted"
                display_name = f"{name} ({team_prefix}, {position}, {age}yo, {rating} OVR)"

                # Value is player_id so we can query by ID later
                choices.append(app_commands.Choice(name=display_name, value=str(player_id)))

        # Return up to 25 choices (Discord limit)
        return choices[:25]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Check if user has admin permissions for admin commands"""
        if await is_admin_user(interaction):
            return True

        if ADMIN_ROLE_ID:
            await interaction.response.send_message(
                "❌ You need the admin role to use this command.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "❌ You need Administrator permissions to use this command.",
                ephemeral=True
            )
        return False

    async def get_current_round(self, db):
        """Get the current round number from active season"""
        cursor = await db.execute(
            "SELECT current_round FROM seasons WHERE status = 'active' LIMIT 1"
        )
        result = await cursor.fetchone()
        return result[0] if result else 0

    @app_commands.command(name="addsuspension", description="[ADMIN] Add a suspension to a player")
    @app_commands.describe(
        player_name="Player name",
        suspension_reason="Reason for suspension",
        games_missed="Number of games to miss"
    )
    @app_commands.autocomplete(player_name=player_name_autocomplete)
    async def add_suspension(
        self,
        interaction: discord.Interaction,
        player_name: str,
        suspension_reason: str,
        games_missed: int
    ):
        async with aiosqlite.connect(DB_PATH) as db:
            # Get player by ID (player_name is actually player_id from autocomplete)
            try:
                player_id = int(player_name)
            except ValueError:
                await interaction.response.send_message(
                    f"❌ Invalid player selection. Please use the autocomplete suggestions.",
                    ephemeral=True
                )
                return

            cursor = await db.execute(
                "SELECT player_id, name FROM players WHERE player_id = ?",
                (player_id,)
            )
            player = await cursor.fetchone()

            if not player:
                await interaction.response.send_message(
                    f"❌ Player not found. Please select from the autocomplete suggestions.",
                    ephemeral=True
                )
                return

            player_id, p_name = player

            # Check if player is already suspended
            cursor = await db.execute(
                """SELECT suspension_id FROM suspensions
                   WHERE player_id = ? AND status = 'suspended'""",
                (player_id,)
            )
            existing = await cursor.fetchone()

            if existing:
                await interaction.response.send_message(
                    f"❌ **{p_name}** is already suspended! Use `/editsuspension` to modify it.",
                    ephemeral=True
                )
                return

            # Get current round
            current_round = await self.get_current_round(db)

            if current_round == 0:
                await interaction.response.send_message(
                    "❌ No active season! Start a season first.",
                    ephemeral=True
                )
                return

            # Add suspension
            await db.execute(
                """INSERT INTO suspensions (player_id, suspension_reason, suspension_round, games_missed, games_remaining, status)
                   VALUES (?, ?, ?, ?, ?, 'suspended')""",
                (player_id, suspension_reason, current_round, games_missed, games_missed)
            )
            await db.commit()

            # Send response
            await interaction.response.send_message(
                f"🚫 **{p_name}** has been suspended!\n"
                f"• Reason: {suspension_reason}\n"
                f"• Games missed: {games_missed} game{'s' if games_missed != 1 else ''}",
                ephemeral=True
            )

    @app_commands.command(name="editsuspension", description="[ADMIN] Edit a player's suspension")
    @app_commands.describe(
        player_name="Player name",
        new_suspension_reason="New suspension reason (optional)",
        new_games_missed="New games missed (optional)"
    )
    @app_commands.autocomplete(player_name=suspended_player_autocomplete)
    async def edit_suspension(
        self,
        interaction: discord.Interaction,
        player_name: str,
        new_suspension_reason: str = None,
        new_games_missed: int = None
    ):
        async with aiosqlite.connect(DB_PATH) as db:
            # Get player by ID (player_name is actually player_id from autocomplete)
            try:
                player_id = int(player_name)
            except ValueError:
                await interaction.response.send_message(
                    f"❌ Invalid player selection. Please use the autocomplete suggestions.",
                    ephemeral=True
                )
                return

            cursor = await db.execute(
                """SELECT p.player_id, p.name
                   FROM players p
                   WHERE p.player_id = ?""",
                (player_id,)
            )
            player = await cursor.fetchone()

            if not player:
                await interaction.response.send_message(
                    f"❌ Player not found. Please select from the autocomplete suggestions.",
                    ephemeral=True
                )
                return

            player_id, p_name = player

            # Find active suspension
            cursor = await db.execute(
                """SELECT suspension_id, suspension_reason, suspension_round, games_missed, games_remaining
                   FROM suspensions
                   WHERE player_id = ? AND status = 'suspended'""",
                (player_id,)
            )
            suspension = await cursor.fetchone()

            if not suspension:
                await interaction.response.send_message(
                    f"❌ **{p_name}** has no active suspension!",
                    ephemeral=True
                )
                return

            suspension_id, old_suspension_reason, suspension_round, old_games_missed, old_games_remaining = suspension

            # Update fields
            updates = []
            values = []
            changes = []

            if new_suspension_reason:
                updates.append("suspension_reason = ?")
                values.append(new_suspension_reason)
                changes.append(f"Reason: {old_suspension_reason} → {new_suspension_reason}")

            if new_games_missed:
                updates.append("games_missed = ?, games_remaining = ?")
                values.extend([new_games_missed, new_games_missed])
                # old_games_remaining is NULL when this is a still-TBC
                # report-driven suspension (see season_commands.py's
                # _roll_pending_report_suspensions) - shown as "TBC" rather
                # than a raw None, same as /editinjury's equivalent case.
                old_display = "TBC" if old_games_remaining is None else f"{old_games_remaining} {'game' if old_games_remaining == 1 else 'games'}"
                new_game_text = "game" if new_games_missed == 1 else "games"
                changes.append(f"Games remaining: {old_display} → {new_games_missed} {new_game_text}")

            if not updates:
                await interaction.response.send_message(
                    "❌ No updates specified!",
                    ephemeral=True
                )
                return

            # Perform update
            values.append(suspension_id)
            query = f"UPDATE suspensions SET {', '.join(updates)} WHERE suspension_id = ?"

            await db.execute(query, values)
            await db.commit()

            response = f"✅ Updated suspension for **{p_name}**\n\n"
            response += "\n".join(changes)

            await interaction.response.send_message(response, ephemeral=True)

    @app_commands.command(name="removesuspension", description="[ADMIN] Remove a player's suspension")
    @app_commands.describe(player_name="Player name")
    @app_commands.autocomplete(player_name=suspended_player_autocomplete)
    async def remove_suspension(self, interaction: discord.Interaction, player_name: str):
        async with aiosqlite.connect(DB_PATH) as db:
            # Get player by ID (player_name is actually player_id from autocomplete)
            try:
                player_id = int(player_name)
            except ValueError:
                await interaction.response.send_message(
                    f"❌ Invalid player selection. Please use the autocomplete suggestions.",
                    ephemeral=True
                )
                return

            cursor = await db.execute(
                "SELECT player_id, name FROM players WHERE player_id = ?",
                (player_id,)
            )
            player = await cursor.fetchone()

            if not player:
                await interaction.response.send_message(
                    f"❌ Player not found. Please select from the autocomplete suggestions.",
                    ephemeral=True
                )
                return

            player_id, p_name = player

            # Find and remove active suspension
            cursor = await db.execute(
                """SELECT suspension_id FROM suspensions
                   WHERE player_id = ? AND status = 'suspended'""",
                (player_id,)
            )
            suspension = await cursor.fetchone()

            if not suspension:
                await interaction.response.send_message(
                    f"❌ **{p_name}** has no active suspension!",
                    ephemeral=True
                )
                return

            # Completed - remove the suspension record
            await db.execute(
                "DELETE FROM suspensions WHERE suspension_id = ?",
                (suspension[0],)
            )
            await db.commit()

            await interaction.response.send_message(
                f"✅ **{p_name}**'s suspension has been lifted!",
                ephemeral=True
            )


async def setup(bot):
    await bot.add_cog(SuspensionCommands(bot))
