"""
One-time migration script to update the seasons table schema.
Run this once on Railway, then delete this file.
"""
import asyncio
import aiosqlite
from config import DB_PATH

async def migrate():
    async with aiosqlite.connect(DB_PATH) as db:
        # Drop old seasons table
        await db.execute("DROP TABLE IF EXISTS seasons")

        # Create new seasons table with correct schema
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
        print("✅ Migration complete! Seasons table updated.")

if __name__ == "__main__":
    asyncio.run(migrate())
