import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from config import DB_PATH, ADMIN_ROLE_ID

# Finals structure - added after regular season
FINALS_ROUNDS = [
    "Pre-Finals Bye",
    "Finals Week 1",
    "Finals Week 2",
    "Preliminary Final",
    "Grand Final"
]

def get_round_name(round_num, regular_season_rounds):
    """Get the name for a given round number"""
    if round_num <= regular_season_rounds:
        return f"Round {round_num}"
    else:
        # Finals rounds
        finals_index = round_num - regular_season_rounds - 1
        if finals_index < len(FINALS_ROUNDS):
            return FINALS_ROUNDS[finals_index]
        else:
            return f"Round {round_num}"

class SeasonCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Check if user has admin permissions for admin commands"""
        # Check if command is a public command
        if interaction.command.name in ['currentseason']:
            return True

        # Admin check for other commands
        if interaction.guild.owner_id == interaction.user.id:
            return True

        if ADMIN_ROLE_ID:
            member = interaction.guild.get_member(interaction.user.id) or interaction.user
            if member:
                admin_role_id = int(ADMIN_ROLE_ID) if isinstance(ADMIN_ROLE_ID, str) else ADMIN_ROLE_ID
                if any(role.id == admin_role_id for role in member.roles):
                    return True

            await interaction.response.send_message(
                "❌ You need the admin role to use this command.",
                ephemeral=True
            )
            return False

        try:
            if interaction.user.guild_permissions.administrator:
                return True
        except:
            pass

        member = interaction.guild.get_member(interaction.user.id)
        if member:
            for role in member.roles:
                if role.permissions.administrator:
                    return True

        await interaction.response.send_message(
            "❌ You need Administrator permissions to use this command.",
            ephemeral=True
        )
        return False

    @app_commands.command(name="migrateseasons", description="[ADMIN] Migrate seasons table (run once after update)")
    async def migrate_seasons(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as db:
            try:
                # Drop old table
                await db.execute("DROP TABLE IF EXISTS seasons")

                # Create new table
                await db.execute('''
                    CREATE TABLE seasons (
                        season_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        season_number INTEGER NOT NULL UNIQUE,
                        current_round INTEGER DEFAULT 0,
                        regular_rounds INTEGER DEFAULT 24,
                        total_rounds INTEGER DEFAULT 29,
                        round_name TEXT DEFAULT 'Offseason',
                        status TEXT DEFAULT 'offseason'
                    )
                ''')

                await db.commit()

                await interaction.followup.send(
                    "✅ Seasons table migrated successfully!\n"
                    "You can now use `/createseason` to create seasons.",
                    ephemeral=True
                )
            except Exception as e:
                await interaction.followup.send(
                    f"❌ Migration failed: {str(e)}",
                    ephemeral=True
                )

    @app_commands.command(name="createseason", description="[ADMIN] Create a new season")
    @app_commands.describe(
        season_number="Season number (e.g., 1, 2, 3)",
        regular_rounds="Number of regular season rounds (default: 24, finals added automatically)"
    )
    async def create_season(
        self,
        interaction: discord.Interaction,
        season_number: int,
        regular_rounds: int = 24
    ):
        async with aiosqlite.connect(DB_PATH) as db:
            # Check if season already exists
            cursor = await db.execute(
                "SELECT season_id FROM seasons WHERE season_number = ?",
                (season_number,)
            )
            existing = await cursor.fetchone()

            if existing:
                await interaction.response.send_message(
                    f"❌ Season {season_number} already exists!",
                    ephemeral=True
                )
                return

            # Total rounds = regular season + finals (5 rounds)
            total_rounds = regular_rounds + len(FINALS_ROUNDS)

            # Create the season
            await db.execute(
                """INSERT INTO seasons (season_number, current_round, regular_rounds, total_rounds, round_name, status)
                   VALUES (?, 0, ?, ?, 'Offseason', 'offseason')""",
                (season_number, regular_rounds, total_rounds)
            )
            await db.commit()

            await interaction.response.send_message(
                f"✅ Created **Season {season_number}**\n"
                f"• Regular Season: {regular_rounds} rounds\n"
                f"• Finals: {len(FINALS_ROUNDS)} rounds\n"
                f"• Total: {total_rounds} rounds\n\n"
                f"Use `/startseason` to begin the season."
            )

    @app_commands.command(name="startseason", description="[ADMIN] Start the current offseason")
    async def start_season(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            # Find the offseason
            cursor = await db.execute(
                """SELECT season_id, season_number, regular_rounds FROM seasons
                   WHERE status = 'offseason'
                   ORDER BY season_number DESC LIMIT 1"""
            )
            season = await cursor.fetchone()

            if not season:
                await interaction.response.send_message(
                    "❌ No offseason found! Create a season first with `/createseason`.",
                    ephemeral=True
                )
                return

            season_id, season_number, regular_rounds = season

            # Start the season
            round_name = get_round_name(1, regular_rounds)
            await db.execute(
                """UPDATE seasons
                   SET current_round = 1, round_name = ?, status = 'active'
                   WHERE season_id = ?""",
                (round_name, season_id)
            )
            await db.commit()

            await interaction.response.send_message(
                f"✅ **Season {season_number}** has started!\n"
                f"Current: {round_name}"
            )

    @app_commands.command(name="nextround", description="[ADMIN] Advance to the next round")
    async def next_round(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            # Get active season
            cursor = await db.execute(
                """SELECT season_id, season_number, current_round, regular_rounds, total_rounds
                   FROM seasons WHERE status = 'active' LIMIT 1"""
            )
            season = await cursor.fetchone()

            if not season:
                await interaction.response.send_message(
                    "❌ No active season! Start a season first with `/startseason`.",
                    ephemeral=True
                )
                return

            season_id, season_number, current_round, regular_rounds, total_rounds = season

            # Check if season is complete
            if current_round >= total_rounds:
                await interaction.response.send_message(
                    f"❌ Season {season_number} is complete! Use `/endseason` to finish it.",
                    ephemeral=True
                )
                return

            # Advance to next round
            next_round_num = current_round + 1
            next_round_name = get_round_name(next_round_num, regular_rounds)

            await db.execute(
                """UPDATE seasons
                   SET current_round = ?, round_name = ?
                   WHERE season_id = ?""",
                (next_round_num, next_round_name, season_id)
            )
            await db.commit()

            await interaction.response.send_message(
                f"✅ Advanced to **{next_round_name}** of Season {season_number}\n"
                f"({next_round_num}/{total_rounds})"
            )

    @app_commands.command(name="endseason", description="[ADMIN] End the current season")
    async def end_season(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            # Get active season
            cursor = await db.execute(
                """SELECT season_id, season_number FROM seasons
                   WHERE status = 'active' LIMIT 1"""
            )
            season = await cursor.fetchone()

            if not season:
                await interaction.response.send_message(
                    "❌ No active season to end!",
                    ephemeral=True
                )
                return

            season_id, season_number = season

            # End the season
            await db.execute(
                """UPDATE seasons
                   SET status = 'completed', round_name = 'Season Complete'
                   WHERE season_id = ?""",
                (season_id,)
            )
            await db.commit()

            await interaction.response.send_message(
                f"✅ **Season {season_number}** has ended!\n"
                f"Create a new season with `/createseason {season_number + 1}`"
            )

    @app_commands.command(name="currentseason", description="View the current season status")
    async def current_season(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            # Get active or most recent season
            cursor = await db.execute(
                """SELECT season_number, current_round, total_rounds, round_name, status
                   FROM seasons
                   ORDER BY
                       CASE status
                           WHEN 'active' THEN 1
                           WHEN 'offseason' THEN 2
                           ELSE 3
                       END,
                       season_number DESC
                   LIMIT 1"""
            )
            season = await cursor.fetchone()

            if not season:
                await interaction.response.send_message(
                    "No seasons created yet! An admin can create one with `/createseason`."
                )
                return

            season_number, current_round, total_rounds, round_name, status = season

            # Build embed
            if status == 'active':
                color = discord.Color.green()
                status_text = "🟢 Active"
            elif status == 'offseason':
                color = discord.Color.blue()
                status_text = "🔵 Offseason"
            else:
                color = discord.Color.grey()
                status_text = "⚫ Completed"

            embed = discord.Embed(
                title=f"Season {season_number}",
                color=color
            )
            embed.add_field(name="Status", value=status_text, inline=True)
            embed.add_field(name="Current", value=round_name, inline=True)

            if status == 'active':
                embed.add_field(
                    name="Progress",
                    value=f"{current_round}/{total_rounds} rounds",
                    inline=True
                )

            await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(SeasonCommands(bot))
