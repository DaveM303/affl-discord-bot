import os
from dotenv import load_dotenv

# Load environment variables from .env file
# Check if .env.test exists (for local testing), otherwise use .env (for production/Railway)
if os.path.exists('.env.test'):
    load_dotenv('.env.test')
    print("Loading test configuration from .env.test")
else:
    load_dotenv()
    print("Loading production configuration from .env")

# Bot Configuration
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", 0)) if os.getenv("GUILD_ID") else None

# Admin Configuration
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID")) if os.getenv("ADMIN_ROLE_ID") else None

# Database Configuration
DB_PATH = os.getenv("DB_PATH", "affl_bot.db")