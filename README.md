# AFFL Discord Bot

AFL Fantasy League Discord Bot for managing teams, players, lineups, and league operations.

## Features

- Player management and search
- Team roster management
- Interactive lineup builder (22-player AFL format)
- Admin commands for league management
- Excel import/export for bulk operations
- Position-based player filtering
- Team emoji support

## Local Development Setup

### Prerequisites
- Python 3.10 or higher
- Discord Bot Token

### Installation

1. Clone the repository
2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Create a `.env` file based on `.env.example`:
```bash
cp .env.example .env
```

4. Edit `.env` and add your credentials:
```env
DISCORD_BOT_TOKEN=your_bot_token_here
GUILD_ID=your_guild_id_here
ADMIN_ROLE_ID=your_admin_role_id_here
DB_PATH=afl_fantasy.db
```

5. Run the bot:
```bash
python bot.py
```

## Railway Deployment

### Step 1: Prepare Your Repository

1. Initialize git (if not already done):
```bash
git init
git add .
git commit -m "Initial commit"
```

2. Push to GitHub (create a new repository on GitHub first):
```bash
git remote add origin https://github.com/yourusername/affl-discord-bot.git
git branch -M main
git push -u origin main
```

### Step 2: Deploy to Railway

1. Go to [Railway.app](https://railway.app/) and sign up/login
2. Click **"New Project"**
3. Select **"Deploy from GitHub repo"**
4. Select your repository
5. Railway will automatically detect it's a Python app

### Step 3: Set Environment Variables

In your Railway project dashboard:

1. Click on your service
2. Go to **"Variables"** tab
3. Add the following variables:
   - `DISCORD_BOT_TOKEN` = `your_bot_token`
   - `GUILD_ID` = `1001462338400038972`
   - `ADMIN_ROLE_ID` = `1023916522902671442`
   - `DB_PATH` = `afl_fantasy.db`

4. Click **"Deploy"** or restart the service

### Step 4: Add Persistent Storage (Important!)

Your database needs persistent storage:

1. In Railway dashboard, go to your service
2. Click **"Settings"** tab
3. Scroll to **"Volumes"**
4. Click **"Add Volume"**
5. Mount path: `/app/data`
6. Then update the `DB_PATH` environment variable to: `/app/data/afl_fantasy.db`

### Step 5: Verify Deployment

1. Check the **"Deployments"** tab for build status
2. Check the **"Logs"** to see if the bot connected successfully
3. You should see: `{BotName} has connected to Discord!`

## Commands

### Public Commands
- `/player` - Look up player information
- `/roster` - View team rosters (defaults to your team)
- `/searchplayers` - Advanced player search with filters
- `/teamlist` - View all league teams
- `/viewlineup` - View team lineups
- `/setlineup` - Set your team's 22-player lineup
- `/clearlineup` - Clear your lineup

### Admin Commands
- `/addteam` - Add a new team
- `/removeteam` - Remove a team
- `/addplayer` - Add a new player
- `/removeplayer` - Remove a player
- `/updateplayer` - Update player stats
- `/signplayer` - Sign a free agent to a team
- `/releaseplayer` - Release a player to free agency
- `/exportdata` - Export all data to Excel
- `/importdata` - Import data from Excel

## Database

The bot uses SQLite with the following tables:
- `players` - Player information
- `teams` - Team information with Discord role integration
- `lineups` - 22-player AFL lineup management
- `seasons` - Season tracking
- `matches` - Match results
- `draft_picks` - Draft system
- `trades` - Trade proposals

## Troubleshooting

### Bot not responding to commands
- Check that the bot has proper permissions in your Discord server
- Verify environment variables are set correctly
- Check Railway logs for errors

### Database issues
- Make sure you've added a Volume in Railway
- Verify `DB_PATH` points to the volume mount path

### Commands not showing up
- The bot syncs commands on startup
- Check logs for "Synced X command(s)" message
- If using GUILD_ID, commands appear instantly; without it, can take up to 1 hour

## Support

For issues or questions, check the Railway logs first, then verify your environment variables are correct.
