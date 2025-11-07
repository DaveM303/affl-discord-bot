"""Utility functions for the AFFL Discord Bot"""

import aiosqlite
from config import DB_PATH


async def get_current_year(db):
    """
    Get the current calendar year based on current season and season_1_year setting.

    Args:
        db: Active aiosqlite database connection

    Returns:
        int: Current calendar year
    """
    # Get current season
    cursor = await db.execute(
        """SELECT season_number FROM seasons
           ORDER BY
               CASE status
                   WHEN 'active' THEN 1
                   WHEN 'offseason' THEN 2
                   ELSE 3
               END,
               season_number DESC
           LIMIT 1"""
    )
    season_result = await cursor.fetchone()
    if not season_result:
        return None
    current_season = season_result[0]

    # Get season_1_year setting
    cursor = await db.execute(
        "SELECT setting_value FROM settings WHERE setting_key = 'season_1_year'"
    )
    setting_result = await cursor.fetchone()
    if not setting_result:
        # Fallback: assume year equals season number
        return current_season

    season_1_year = int(setting_result[0])
    current_year = season_1_year + (current_season - 1)
    return current_year


def age_calculation_sql(current_year):
    """
    Returns SQL expression to calculate age from birth_year.

    Args:
        current_year: Current calendar year (int or SQL parameter placeholder)

    Returns:
        str: SQL expression like "? - birth_year" or "2025 - birth_year"
    """
    if isinstance(current_year, int):
        return f"{current_year} - p.birth_year"
    else:
        return "? - p.birth_year"
