import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from config import DB_PATH, ADMIN_ROLE_ID

# Finals structure - added after regular season
FINALS_ROUNDS = [
    "Pre-Finals Bye",
    "Finals Week 1",
    "Semi Finals",
    "Preliminary Finals",
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

    @app_commands.command(name="migratedb", description="[ADMIN] Migrate database tables (run once after updates)")
    async def migrate_db(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as db:
            try:
                # Migrate seasons table - preserve existing data
                await db.execute('''
                    CREATE TABLE IF NOT EXISTS seasons (
                        season_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        season_number INTEGER NOT NULL UNIQUE,
                        current_round INTEGER DEFAULT 0,
                        regular_rounds INTEGER DEFAULT 24,
                        total_rounds INTEGER DEFAULT 29,
                        round_name TEXT DEFAULT 'Offseason',
                        status TEXT DEFAULT 'offseason'
                    )
                ''')

                # Create injuries table
                await db.execute('''
                    CREATE TABLE IF NOT EXISTS injuries (
                        injury_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        player_id INTEGER NOT NULL,
                        injury_type TEXT NOT NULL,
                        injury_round INTEGER NOT NULL,
                        recovery_rounds INTEGER NOT NULL,
                        return_round INTEGER NOT NULL,
                        status TEXT DEFAULT 'injured',
                        FOREIGN KEY (player_id) REFERENCES players(player_id)
                    )
                ''')

                # Create suspensions table
                await db.execute('''
                    CREATE TABLE IF NOT EXISTS suspensions (
                        suspension_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        player_id INTEGER NOT NULL,
                        suspension_reason TEXT NOT NULL,
                        suspension_round INTEGER NOT NULL,
                        games_missed INTEGER NOT NULL,
                        return_round INTEGER NOT NULL,
                        status TEXT DEFAULT 'suspended',
                        FOREIGN KEY (player_id) REFERENCES players(player_id)
                    )
                ''')

                # Create settings table for global bot settings
                await db.execute('''
                    CREATE TABLE IF NOT EXISTS settings (
                        setting_key TEXT PRIMARY KEY,
                        setting_value TEXT
                    )
                ''')

                # Create Starting Lineups table (for saved lineup presets)
                await db.execute('''
                    CREATE TABLE IF NOT EXISTS starting_lineups (
                        team_id INTEGER PRIMARY KEY,
                        lineup_data TEXT NOT NULL,
                        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (team_id) REFERENCES teams(team_id)
                    )
                ''')

                # Create Ladder Positions table (for draft order)
                await db.execute('''
                    CREATE TABLE IF NOT EXISTS ladder_positions (
                        ladder_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        season_id INTEGER NOT NULL,
                        team_id INTEGER NOT NULL,
                        position INTEGER NOT NULL,
                        FOREIGN KEY (season_id) REFERENCES seasons(season_id),
                        FOREIGN KEY (team_id) REFERENCES teams(team_id),
                        UNIQUE(season_id, team_id),
                        UNIQUE(season_id, position)
                    )
                ''')

                # Drop and recreate trades table with correct schema
                await db.execute('DROP TABLE IF EXISTS trades')
                await db.execute('''
                    CREATE TABLE trades (
                        trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        initiating_team_id INTEGER NOT NULL,
                        receiving_team_id INTEGER NOT NULL,
                        initiating_players TEXT,
                        receiving_players TEXT,
                        initiating_picks TEXT,
                        receiving_picks TEXT,
                        status TEXT DEFAULT 'pending',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        responded_at TIMESTAMP,
                        approved_at TIMESTAMP,
                        created_by_user_id TEXT,
                        responded_by_user_id TEXT,
                        approved_by_user_id TEXT,
                        original_trade_id INTEGER,
                        FOREIGN KEY (initiating_team_id) REFERENCES teams(team_id),
                        FOREIGN KEY (receiving_team_id) REFERENCES teams(team_id),
                        FOREIGN KEY (original_trade_id) REFERENCES trades(trade_id)
                    )
                ''')

                # Update draft_picks table schema (preserve data, migrate to pick_origin)
                cursor = await db.execute("PRAGMA table_info(draft_picks)")
                columns = await cursor.fetchall()
                column_names = [col[1] for col in columns]

                # Check if we need to migrate from old schema
                if 'original_team_id' in column_names and 'pick_origin' not in column_names:
                    # Backup existing draft data
                    cursor = await db.execute('''
                        SELECT dp.pick_id, dp.draft_name, dp.round_number, dp.pick_number,
                               t.team_name, dp.current_team_id, dp.player_selected_id
                        FROM draft_picks dp
                        LEFT JOIN teams t ON dp.original_team_id = t.team_id
                    ''')
                    draft_backup = await cursor.fetchall()

                    # Recreate table with new schema
                    await db.execute('DROP TABLE IF EXISTS draft_picks')
                    await db.execute('''
                        CREATE TABLE draft_picks (
                            pick_id INTEGER PRIMARY KEY AUTOINCREMENT,
                            draft_name TEXT NOT NULL,
                            round_number INTEGER,
                            pick_number INTEGER,
                            pick_origin TEXT,
                            current_team_id INTEGER,
                            player_selected_id INTEGER,
                            FOREIGN KEY (current_team_id) REFERENCES teams(team_id),
                            FOREIGN KEY (player_selected_id) REFERENCES players(player_id)
                        )
                    ''')

                    # Restore data with converted pick_origin
                    for pick in draft_backup:
                        pick_id, draft_name, round_num, pick_num, orig_team, curr_team, player = pick
                        pick_origin = f"{orig_team} R{round_num}" if orig_team else f"R{round_num}"
                        await db.execute('''
                            INSERT INTO draft_picks (pick_id, draft_name, round_number, pick_number,
                                                    pick_origin, current_team_id, player_selected_id)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        ''', (pick_id, draft_name, round_num, pick_num, pick_origin, curr_team, player))
                else:
                    # Table already has correct schema or doesn't exist yet
                    await db.execute('''
                        CREATE TABLE IF NOT EXISTS draft_picks (
                            pick_id INTEGER PRIMARY KEY AUTOINCREMENT,
                            draft_name TEXT NOT NULL,
                            round_number INTEGER,
                            pick_number INTEGER,
                            pick_origin TEXT,
                            current_team_id INTEGER,
                            player_selected_id INTEGER,
                            FOREIGN KEY (current_team_id) REFERENCES teams(team_id),
                            FOREIGN KEY (player_selected_id) REFERENCES players(player_id)
                        )
                    ''')

                # Remove lineup_channel_id from teams if it exists (moved to settings)
                cursor = await db.execute("PRAGMA table_info(teams)")
                columns = await cursor.fetchall()
                column_names = [col[1] for col in columns]

                if 'lineup_channel_id' in column_names:
                    # Can't drop column in SQLite, so just notify user it's deprecated
                    pass

                await db.commit()

                await interaction.followup.send(
                    "✅ Database migrated successfully!\n"
                    "• Seasons table created (existing data preserved)\n"
                    "• Injuries table created\n"
                    "• Trades table recreated with new schema\n"
                    "• Draft Picks table recreated with new schema (draft_name)\n"
                    "• Suspensions table created\n"
                    "• Starting Lineups table created\n"
                    "• Ladder Positions table created\n"
                    "• Settings table created\n\n"
                    "You can now use all season, injury, suspension, and lineup submission commands.\n"
                    "Use `/setlineupschannel` to configure where lineups are posted.",
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
                f"✅ Created **Season {season_number}** with {regular_rounds} rounds\n"
                f"Use `/startseason` to begin the season."
            )

    @app_commands.command(name="startseason", description="[ADMIN] Start the current offseason")
    @app_commands.describe(offseason_weeks="Number of weeks in offseason (default: 23)")
    async def start_season(self, interaction: discord.Interaction, offseason_weeks: int = 23):
        await interaction.response.defer(ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as db:
            # Find the offseason
            cursor = await db.execute(
                """SELECT season_id, season_number, regular_rounds, total_rounds FROM seasons
                   WHERE status = 'offseason'
                   ORDER BY season_number DESC LIMIT 1"""
            )
            season = await cursor.fetchone()

            if not season:
                await interaction.followup.send(
                    "❌ No offseason found! Create a season first with `/createseason`.",
                    ephemeral=True
                )
                return

            season_id, season_number, regular_rounds, total_rounds = season

            # Get previous season's final round to calculate injury carryover
            cursor = await db.execute(
                """SELECT season_id, total_rounds FROM seasons
                   WHERE status = 'completed'
                   ORDER BY season_number DESC LIMIT 1"""
            )
            prev_season = await cursor.fetchone()

            carried_over = 0
            healed_during_offseason = 0
            suspensions_carried_over = 0
            suspensions_completed_offseason = 0

            if prev_season:
                prev_season_id, prev_total_rounds = prev_season

                # Find injuries that were still active at end of previous season
                cursor = await db.execute(
                    """SELECT injury_id, player_id, injury_type, return_round
                       FROM injuries
                       WHERE status = 'injured' AND return_round > ?""",
                    (prev_total_rounds,)
                )
                active_injuries = await cursor.fetchall()

                for injury_id, player_id, injury_type, old_return_round in active_injuries:
                    # Calculate weeks remaining from end of last season
                    weeks_remaining = old_return_round - prev_total_rounds

                    # Subtract offseason weeks
                    weeks_into_new_season = weeks_remaining - offseason_weeks

                    if weeks_into_new_season <= 0:
                        # Injury healed during offseason
                        await db.execute(
                            "UPDATE injuries SET status = 'recovered' WHERE injury_id = ?",
                            (injury_id,)
                        )
                        healed_during_offseason += 1
                    else:
                        # Injury carries over - update return round for new season
                        new_return_round = weeks_into_new_season
                        await db.execute(
                            """UPDATE injuries
                               SET return_round = ?
                               WHERE injury_id = ?""",
                            (new_return_round, injury_id)
                        )
                        carried_over += 1

                # Find suspensions that were still active at end of previous season
                cursor = await db.execute(
                    """SELECT suspension_id, player_id, suspension_reason, return_round
                       FROM suspensions
                       WHERE status = 'suspended' AND return_round > ?""",
                    (prev_total_rounds,)
                )
                active_suspensions = await cursor.fetchall()

                for suspension_id, player_id, suspension_reason, old_return_round in active_suspensions:
                    # Calculate games remaining from end of last season
                    games_remaining = old_return_round - prev_total_rounds

                    # Subtract offseason weeks
                    games_into_new_season = games_remaining - offseason_weeks

                    if games_into_new_season <= 0:
                        # Suspension completed during offseason
                        await db.execute(
                            "UPDATE suspensions SET status = 'completed' WHERE suspension_id = ?",
                            (suspension_id,)
                        )
                        suspensions_completed_offseason += 1
                    else:
                        # Suspension carries over - update return round for new season
                        new_return_round = games_into_new_season
                        await db.execute(
                            """UPDATE suspensions
                               SET return_round = ?
                               WHERE suspension_id = ?""",
                            (new_return_round, suspension_id)
                        )
                        suspensions_carried_over += 1

                await db.commit()

            # Start the season
            round_name = get_round_name(1, regular_rounds)
            await db.execute(
                """UPDATE seasons
                   SET current_round = 1, round_name = ?, status = 'active'
                   WHERE season_id = ?""",
                (round_name, season_id)
            )
            await db.commit()

            message = f"✅ **Season {season_number}** has started!\nCurrent: {round_name}"

            if carried_over > 0 or healed_during_offseason > 0:
                message += f"\n\n**Injury Updates:**"
                if healed_during_offseason > 0:
                    message += f"\n• {healed_during_offseason} player(s) healed during offseason"
                if carried_over > 0:
                    message += f"\n• {carried_over} injury(ies) carried over to new season"

            if suspensions_carried_over > 0 or suspensions_completed_offseason > 0:
                message += f"\n\n**Suspension Updates:**"
                if suspensions_completed_offseason > 0:
                    message += f"\n• {suspensions_completed_offseason} suspension(s) completed during offseason"
                if suspensions_carried_over > 0:
                    message += f"\n• {suspensions_carried_over} suspension(s) carried over to new season"

            await interaction.followup.send(message, ephemeral=True)

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

            # Check for players who have recovered from injuries
            cursor = await db.execute(
                """SELECT i.injury_id, p.name, p.team_id, t.channel_id
                   FROM injuries i
                   JOIN players p ON i.player_id = p.player_id
                   LEFT JOIN teams t ON p.team_id = t.team_id
                   WHERE i.status = 'injured' AND i.return_round <= ?""",
                (next_round_num,)
            )
            recovered_players = await cursor.fetchall()

            # Mark recovered players and send notifications
            for injury_id, player_name, team_id, channel_id in recovered_players:
                # Mark as recovered
                await db.execute(
                    "UPDATE injuries SET status = 'recovered' WHERE injury_id = ?",
                    (injury_id,)
                )

                # Notify team channel
                if team_id and channel_id:
                    channel = self.bot.get_channel(int(channel_id))
                    if channel:
                        await channel.send(
                            f"✅ **Recovery Update**\n"
                            f"**{player_name}** has recovered from injury and is available for selection!"
                        )

            if recovered_players:
                await db.commit()

            # Check for players whose suspensions are complete
            cursor = await db.execute(
                """SELECT s.suspension_id, p.name, p.team_id, t.channel_id
                   FROM suspensions s
                   JOIN players p ON s.player_id = p.player_id
                   LEFT JOIN teams t ON p.team_id = t.team_id
                   WHERE s.status = 'suspended' AND s.return_round <= ?""",
                (next_round_num,)
            )
            completed_suspensions = await cursor.fetchall()

            # Mark suspensions as completed and send notifications
            for suspension_id, player_name, team_id, channel_id in completed_suspensions:
                # Mark as completed
                await db.execute(
                    "UPDATE suspensions SET status = 'completed' WHERE suspension_id = ?",
                    (suspension_id,)
                )

                # Notify team channel
                if team_id and channel_id:
                    channel = self.bot.get_channel(int(channel_id))
                    if channel:
                        await channel.send(
                            f"✅ **Suspension Update**\n"
                            f"**{player_name}**'s suspension has been lifted and they are available for selection!"
                        )

            if completed_suspensions:
                await db.commit()

            response = f"✅ Advanced to **{next_round_name}** of Season {season_number}"
            if recovered_players:
                response += f"\n\n🏥 {len(recovered_players)} player(s) recovered from injury"
            if completed_suspensions:
                response += f"\n🚫 {len(completed_suspensions)} suspension(s) completed"

            await interaction.response.send_message(response)

    @app_commands.command(name="editseason", description="[ADMIN] Edit a season's settings")
    @app_commands.describe(
        season_number="Season number to edit",
        regular_rounds="New number of regular season rounds"
    )
    async def edit_season(
        self,
        interaction: discord.Interaction,
        season_number: int,
        regular_rounds: int
    ):
        async with aiosqlite.connect(DB_PATH) as db:
            # Find the season
            cursor = await db.execute(
                "SELECT season_id, status FROM seasons WHERE season_number = ?",
                (season_number,)
            )
            season = await cursor.fetchone()

            if not season:
                await interaction.response.send_message(
                    f"❌ Season {season_number} not found!",
                    ephemeral=True
                )
                return

            season_id, status = season

            # Calculate new total rounds
            total_rounds = regular_rounds + len(FINALS_ROUNDS)

            # Update the season
            await db.execute(
                """UPDATE seasons
                   SET regular_rounds = ?, total_rounds = ?
                   WHERE season_id = ?""",
                (regular_rounds, total_rounds, season_id)
            )
            await db.commit()

            await interaction.response.send_message(
                f"✅ Updated **Season {season_number}** to {regular_rounds} rounds"
            )

    @app_commands.command(name="setround", description="[ADMIN] Skip to a specific round")
    @app_commands.describe(round_number="Round number to skip to")
    async def set_round(self, interaction: discord.Interaction, round_number: int):
        async with aiosqlite.connect(DB_PATH) as db:
            # Get active season
            cursor = await db.execute(
                """SELECT season_id, season_number, regular_rounds, total_rounds
                   FROM seasons WHERE status = 'active' LIMIT 1"""
            )
            season = await cursor.fetchone()

            if not season:
                await interaction.response.send_message(
                    "❌ No active season! Start a season first with `/startseason`.",
                    ephemeral=True
                )
                return

            season_id, season_number, regular_rounds, total_rounds = season

            # Validate round number
            if round_number < 1 or round_number > total_rounds:
                await interaction.response.send_message(
                    f"❌ Round number must be between 1 and {total_rounds}!",
                    ephemeral=True
                )
                return

            # Set the round
            round_name = get_round_name(round_number, regular_rounds)

            await db.execute(
                """UPDATE seasons
                   SET current_round = ?, round_name = ?
                   WHERE season_id = ?""",
                (round_number, round_name, season_id)
            )
            await db.commit()

            await interaction.response.send_message(
                f"✅ Skipped to **{round_name}** of Season {season_number}"
            )

    @app_commands.command(name="endseason", description="[ADMIN] End the current season and create offseason")
    @app_commands.describe(
        next_season_rounds="Number of rounds for next season (default: same as current season)"
    )
    async def end_season(self, interaction: discord.Interaction, next_season_rounds: int = None):
        async with aiosqlite.connect(DB_PATH) as db:
            # Get active season
            cursor = await db.execute(
                """SELECT season_id, season_number, regular_rounds FROM seasons
                   WHERE status = 'active' LIMIT 1"""
            )
            season = await cursor.fetchone()

            if not season:
                await interaction.response.send_message(
                    "❌ No active season to end!",
                    ephemeral=True
                )
                return

            season_id, season_number, current_regular_rounds = season

            # Use current season's rounds if not specified
            if next_season_rounds is None:
                next_season_rounds = current_regular_rounds

            # End the current season
            await db.execute(
                """UPDATE seasons
                   SET status = 'completed', round_name = 'Season Complete'
                   WHERE season_id = ?""",
                (season_id,)
            )

            # Check if next season already exists
            next_season_num = season_number + 1
            cursor = await db.execute(
                "SELECT season_id FROM seasons WHERE season_number = ?",
                (next_season_num,)
            )
            existing = await cursor.fetchone()

            if existing:
                await db.commit()
                await interaction.response.send_message(
                    f"✅ **Season {season_number}** has ended!\n"
                    f"⚠️ Season {next_season_num} already exists. Use `/startseason` when ready.",
                    ephemeral=True
                )
                return

            # Create next season in offseason status
            total_rounds = next_season_rounds + len(FINALS_ROUNDS)
            await db.execute(
                """INSERT INTO seasons (season_number, current_round, regular_rounds, total_rounds, round_name, status)
                   VALUES (?, 0, ?, ?, 'Offseason', 'offseason')""",
                (next_season_num, next_season_rounds, total_rounds)
            )
            await db.commit()

            await interaction.response.send_message(
                f"✅ **Season {season_number}** has ended!\n"
                f"✅ **Season {next_season_num}** created in offseason ({next_season_rounds} rounds)\n\n"
                f"Use `/startseason` when ready to begin Season {next_season_num}."
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

            await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(SeasonCommands(bot))
