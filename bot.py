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
                FOREIGN KEY (team_id) REFERENCES teams(team_id)
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
        
        # Create Draft Picks table
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
                proposing_team_id INTEGER,
                receiving_team_id INTEGER,
                status TEXT DEFAULT 'proposed',
                proposed_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                details TEXT,
                FOREIGN KEY (proposing_team_id) REFERENCES teams(team_id),
                FOREIGN KEY (receiving_team_id) REFERENCES teams(team_id)
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