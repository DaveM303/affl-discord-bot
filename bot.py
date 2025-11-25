import discord
from discord.ext import commands
import aiosqlite
from config import DISCORD_BOT_TOKEN, GUILD_ID, DB_PATH

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

# Initialize database
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # Create Players table
        await db.execute('''
            CREATE TABLE IF NOT EXISTS players (
                player_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                position TEXT NOT NULL,
                overall_rating INTEGER,
                age INTEGER,
                team_id INTEGER,
                contract_expiry INTEGER,
                FOREIGN KEY (team_id) REFERENCES teams(team_id),
                FOREIGN KEY (contract_expiry) REFERENCES seasons(season_number)
            )
        ''')
        
        # Create Teams table
        await db.execute('''
            CREATE TABLE IF NOT EXISTS teams (
                team_id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_name TEXT NOT NULL UNIQUE,
                role_id TEXT,
                emoji_id TEXT,
                channel_id TEXT
            )
        ''')

        # Create Settings table for global bot settings
        await db.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT
            )
        ''')
        
        # Create Seasons table
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
        
        # Create Matches table
        await db.execute('''
            CREATE TABLE IF NOT EXISTS matches (
                match_id INTEGER PRIMARY KEY AUTOINCREMENT,
                season_id INTEGER,
                round_number INTEGER,
                home_team_id INTEGER,
                away_team_id INTEGER,
                home_score INTEGER DEFAULT 0,
                away_score INTEGER DEFAULT 0,
                simulated BOOLEAN DEFAULT 0,
                FOREIGN KEY (season_id) REFERENCES seasons(season_id),
                FOREIGN KEY (home_team_id) REFERENCES teams(team_id),
                FOREIGN KEY (away_team_id) REFERENCES teams(team_id)
            )
        ''')
        
        # Create Drafts table
        await db.execute('''
            CREATE TABLE IF NOT EXISTS drafts (
                draft_id INTEGER PRIMARY KEY AUTOINCREMENT,
                draft_name TEXT UNIQUE NOT NULL,
                season_number INTEGER NOT NULL,
                status TEXT DEFAULT 'future',
                rounds INTEGER DEFAULT 4,
                rookie_contract_years INTEGER DEFAULT 3,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ladder_set_at TIMESTAMP NULL,
                started_at TIMESTAMP NULL,
                completed_at TIMESTAMP NULL,
                current_pick_number INTEGER DEFAULT 0,
                FOREIGN KEY (season_number) REFERENCES seasons(season_number)
            )
        ''')

        # Create Draft Picks table
        await db.execute('''
            CREATE TABLE IF NOT EXISTS draft_picks (
                pick_id INTEGER PRIMARY KEY AUTOINCREMENT,
                draft_id INTEGER NOT NULL,
                draft_name TEXT NOT NULL,
                season_number INTEGER NOT NULL,
                round_number INTEGER,
                pick_number INTEGER,
                pick_origin TEXT,
                original_team_id INTEGER,
                current_team_id INTEGER,
                player_selected_id INTEGER,
                passed INTEGER DEFAULT 0,
                picked_at TIMESTAMP NULL,
                FOREIGN KEY (draft_id) REFERENCES drafts(draft_id),
                FOREIGN KEY (season_number) REFERENCES seasons(season_number),
                FOREIGN KEY (original_team_id) REFERENCES teams(team_id),
                FOREIGN KEY (current_team_id) REFERENCES teams(team_id),
                FOREIGN KEY (player_selected_id) REFERENCES players(player_id)
            )
        ''')
        
        # Create Lineups table
        await db.execute('''
            CREATE TABLE IF NOT EXISTS lineups (
                lineup_id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id INTEGER,
                player_id INTEGER,
                slot_number INTEGER,
                position_name TEXT,
                UNIQUE(team_id, slot_number),
                UNIQUE(team_id, position_name),
                FOREIGN KEY (team_id) REFERENCES teams(team_id),
                FOREIGN KEY (player_id) REFERENCES players(player_id)
            )
        ''')
        
        # Create Trades table
        await db.execute('''
            CREATE TABLE IF NOT EXISTS trades (
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

        # Create Injuries table
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

        # Create Suspensions table
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

        # Create Draft Value Index table (points value for each draft pick)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS draft_value_index (
                pick_number INTEGER PRIMARY KEY,
                points_value INTEGER NOT NULL
            )
        ''')

        # Create Submitted Lineups table (for tracking lineup submissions per round)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS submitted_lineups (
                submission_id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id INTEGER NOT NULL,
                season_id INTEGER NOT NULL,
                round_number INTEGER NOT NULL,
                player_ids TEXT NOT NULL,
                submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (team_id) REFERENCES teams(team_id),
                FOREIGN KEY (season_id) REFERENCES seasons(season_id),
                UNIQUE(team_id, season_id, round_number)
            )
        ''')

        # Create Contract Config table (age-based contract lengths for free agents)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS contract_config (
                config_id INTEGER PRIMARY KEY AUTOINCREMENT,
                min_age INTEGER NOT NULL,
                max_age INTEGER,
                contract_years INTEGER NOT NULL,
                UNIQUE(min_age, max_age)
            )
        ''')

        # Insert default contract config
        await db.execute('''
            INSERT OR IGNORE INTO contract_config (min_age, max_age, contract_years) VALUES
            (0, 20, 3),
            (21, 23, 5),
            (24, 26, 4),
            (27, 30, 3),
            (31, NULL, 2)
        ''')

        # Insert default draft value index (AFL-style points system)
        # First round picks have highest value, diminishing returns after that
        await db.execute('''
            INSERT OR IGNORE INTO draft_value_index (pick_number, points_value) VALUES
            (1, 3000), (2, 2517), (3, 2234), (4, 2034), (5, 1878),
            (6, 1751), (7, 1644), (8, 1551), (9, 1469), (10, 1395),
            (11, 1329), (12, 1268), (13, 1212), (14, 1161), (15, 1112),
            (16, 1067), (17, 1025), (18, 985), (19, 948), (20, 912),
            (21, 878), (22, 845), (23, 815), (24, 785), (25, 756),
            (26, 729), (27, 703), (28, 677), (29, 653), (30, 629),
            (31, 606), (32, 584), (33, 563), (34, 542), (35, 522),
            (36, 502), (37, 483), (38, 465), (39, 446), (40, 429),
            (41, 412), (42, 395), (43, 378), (44, 362), (45, 347),
            (46, 331), (47, 316), (48, 302), (49, 287), (50, 273),
            (51, 259), (52, 246), (53, 233), (54, 220), (55, 207),
            (56, 194), (57, 182), (58, 170), (59, 158), (60, 146),
            (61, 135), (62, 123), (63, 112), (64, 101), (65, 90),
            (66, 80), (67, 69), (68, 59), (69, 49), (70, 39),
            (71, 29), (72, 19), (73, 9), (74, 9), (75, 9),
            (76, 9), (77, 9), (78, 9), (79, 9), (80, 9),
            (81, 9), (82, 9), (83, 9), (84, 9), (85, 9),
            (86, 9), (87, 9), (88, 9), (89, 9), (90, 9)
        ''')

        # Create Compensation Chart table (free agency compensation bands)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS compensation_chart (
                chart_id INTEGER PRIMARY KEY AUTOINCREMENT,
                min_age INTEGER NOT NULL,
                max_age INTEGER,
                min_ovr INTEGER NOT NULL,
                max_ovr INTEGER,
                compensation_band INTEGER NOT NULL,
                UNIQUE(min_age, max_age, min_ovr, max_ovr)
            )
        ''')

        # Create Free Agency Periods table
        await db.execute('''
            CREATE TABLE IF NOT EXISTS free_agency_periods (
                period_id INTEGER PRIMARY KEY AUTOINCREMENT,
                season_number INTEGER NOT NULL,
                status TEXT DEFAULT 'bidding',
                auction_points INTEGER DEFAULT 300,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                bidding_ended_at TIMESTAMP,
                matching_ended_at TIMESTAMP,
                FOREIGN KEY (season_number) REFERENCES seasons(season_number),
                UNIQUE(season_number)
            )
        ''')

        # Create Free Agency Bids table
        await db.execute('''
            CREATE TABLE IF NOT EXISTS free_agency_bids (
                bid_id INTEGER PRIMARY KEY AUTOINCREMENT,
                period_id INTEGER NOT NULL,
                team_id INTEGER NOT NULL,
                player_id INTEGER NOT NULL,
                bid_amount INTEGER NOT NULL,
                status TEXT DEFAULT 'active',
                placed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (period_id) REFERENCES free_agency_periods(period_id),
                FOREIGN KEY (team_id) REFERENCES teams(team_id),
                FOREIGN KEY (player_id) REFERENCES players(player_id),
                UNIQUE(period_id, team_id, player_id)
            )
        ''')

        # Create Free Agency Results table (for tracking winning bids and matches)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS free_agency_results (
                result_id INTEGER PRIMARY KEY AUTOINCREMENT,
                period_id INTEGER NOT NULL,
                player_id INTEGER NOT NULL,
                original_team_id INTEGER NOT NULL,
                winning_team_id INTEGER,
                winning_bid INTEGER,
                matched BOOLEAN DEFAULT 0,
                compensation_band INTEGER,
                compensation_pick_id INTEGER,
                FOREIGN KEY (period_id) REFERENCES free_agency_periods(period_id),
                FOREIGN KEY (player_id) REFERENCES players(player_id),
                FOREIGN KEY (original_team_id) REFERENCES teams(team_id),
                FOREIGN KEY (winning_team_id) REFERENCES teams(team_id),
                FOREIGN KEY (compensation_pick_id) REFERENCES draft_picks(pick_id),
                UNIQUE(period_id, player_id)
            )
        ''')

        # Create Draft Pool team if it doesn't exist
        cursor = await db.execute("SELECT team_id FROM teams WHERE team_name = 'Draft Pool'")
        if not await cursor.fetchone():
            await db.execute(
                """INSERT INTO teams (team_name, role_id, channel_id, emoji_id)
                   VALUES ('Draft Pool', NULL, NULL, NULL)"""
            )
            print("Created 'Draft Pool' team")

        # Add plays_like column to players table if it doesn't exist
        cursor = await db.execute("PRAGMA table_info(players)")
        columns = await cursor.fetchall()
        column_names = [column[1] for column in columns]

        if 'plays_like' not in column_names:
            await db.execute("ALTER TABLE players ADD COLUMN plays_like TEXT")
            print("Added 'plays_like' column to players table")

        await db.commit()
        print("Database initialized successfully!")

@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')
    await init_db()
    
    # Load command modules
    await bot.load_extension('commands.player_commands')
    await bot.load_extension('commands.admin_commands')
    await bot.load_extension('commands.lineup_commands')
    await bot.load_extension('commands.season_commands')
    await bot.load_extension('commands.injury_commands')
    await bot.load_extension('commands.suspension_commands')
    await bot.load_extension('commands.trade_commands')
    await bot.load_extension('commands.draft_commands')
    await bot.load_extension('commands.free_agency_commands')
    
    try:
        if GUILD_ID:
            # Sync to specific guild (faster for development)
            guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"Synced {len(synced)} command(s) to guild {GUILD_ID}")
        else:
            # Sync globally (slower, can take up to 1 hour)
            synced = await bot.tree.sync()
            print(f"Synced {len(synced)} command(s) globally")
    except Exception as e:
        print(f"Error syncing commands: {e}")

# Run the bot
if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)