import re
import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from config import DB_PATH, ADMIN_ROLE_ID
from commands.season_commands import get_round_name
from utils import is_admin_user, get_team_emoji_str


async def build_injury_suspension_list(bot, db, current_round, total_rounds, filter_team_id=None,
                                        season_id=None, regular_rounds=None, new_this_round=None):
    """Shared combined injuries+suspensions list builder, returned as a list
    of display-ready lines (headers, entries, blank separator) - the same
    format post_injury_list_to_channel renders, factored out so
    round-summary embeds (season_commands.py's post_round_summaries,
    triggered from /matchsimulation's sim buttons) can show a team's own
    current list without duplicating this query/formatting. Sorted by team
    first, then weeks/games remaining, so a full-league list reads grouped
    by team rather than interleaved by return date. Returns [] if there's
    nothing active for the filter.

    Once the finals are underway (current_round > regular_rounds), teams
    already eliminated from premiership contention are excluded entirely -
    their injuries/suspensions no longer matter to anyone still watching the
    finals race. Requires BOTH season_id and regular_rounds to apply this
    (both None by default, e.g. for a caller with no season loaded at all);
    a caller that already knows it's mid-finals should always pass them.

    new_this_round, if given, is the round_number an injury's injury_round
    (or a suspension's suspension_round) must equal to be bolded as "new" -
    everything else renders in plain text. Deliberately a separate
    parameter rather than derived from current_round: callers disagree on
    what current_round even MEANS at the moment they call this (e.g.
    post_injury_list_to_channel reads it fresh from the DB AFTER
    advance_to_next_round has already incremented it, so "the round that
    just finished" is current_round - 1 there, while post_round_summaries
    calls in BEFORE the increment, where current_round already IS that
    round) - safer for each caller to just say explicitly which round
    counts as "new" than to bake in one of those two off-by-one
    assumptions here. None (the default) bolds nothing."""
    eliminated_team_ids = set()
    if season_id is not None and regular_rounds is not None and current_round > regular_rounds:
        from commands.season_commands import get_eliminated_finals_team_ids
        eliminated_team_ids = await get_eliminated_finals_team_ids(db, season_id)

    if filter_team_id:
        cursor = await db.execute(
            """SELECT p.name, p.overall_rating, i.injury_type, i.return_round, t.team_name, t.emoji_id, p.team_id, i.injury_round
               FROM injuries i
               JOIN players p ON i.player_id = p.player_id
               LEFT JOIN teams t ON p.team_id = t.team_id
               WHERE i.status = 'injured' AND p.team_id = ?
               ORDER BY t.team_name ASC, i.return_round ASC, p.name ASC""",
            (filter_team_id,)
        )
    else:
        cursor = await db.execute(
            """SELECT p.name, p.overall_rating, i.injury_type, i.return_round, t.team_name, t.emoji_id, p.team_id, i.injury_round
               FROM injuries i
               JOIN players p ON i.player_id = p.player_id
               LEFT JOIN teams t ON p.team_id = t.team_id
               WHERE i.status = 'injured'
               ORDER BY t.team_name ASC, i.return_round ASC, p.name ASC"""
        )
    injuries = [row for row in await cursor.fetchall() if row[6] not in eliminated_team_ids]

    if filter_team_id:
        cursor = await db.execute(
            """SELECT p.name, p.overall_rating, s.suspension_reason, s.games_remaining, t.team_name, t.emoji_id, p.team_id, s.suspension_round
               FROM suspensions s
               JOIN players p ON s.player_id = p.player_id
               LEFT JOIN teams t ON p.team_id = t.team_id
               WHERE s.status = 'suspended' AND p.team_id = ?
               ORDER BY t.team_name ASC, s.games_remaining ASC, p.name ASC""",
            (filter_team_id,)
        )
    else:
        cursor = await db.execute(
            """SELECT p.name, p.overall_rating, s.suspension_reason, s.games_remaining, t.team_name, t.emoji_id, p.team_id, s.suspension_round
               FROM suspensions s
               JOIN players p ON s.player_id = p.player_id
               LEFT JOIN teams t ON p.team_id = t.team_id
               WHERE s.status = 'suspended'
               ORDER BY t.team_name ASC, s.games_remaining ASC, p.name ASC"""
        )
    suspensions = [row for row in await cursor.fetchall() if row[6] not in eliminated_team_ids]

    combined_list = []

    if injuries:
        combined_list.append("**🚑 Injuries:**")
        for name, overall_rating, injury_type, return_round, team_name, emoji_id, team_id, injury_round in injuries:
            # Emoji dropped for a team-scoped list (round summaries) - every
            # line is already that one team, so it's redundant there. Kept
            # for the league-wide list (/injurylist) where it's the only
            # thing distinguishing which team each entry belongs to.
            team_display = get_team_emoji_str(bot, emoji_id) if team_name and not filter_team_id else ""
            if return_round is None:
                # Recovery length not yet determined - the round it
                # happened in hasn't finished being advanced past yet (see
                # season_commands.py's _roll_pending_injury_recoveries).
                status = "- TBC"
            else:
                weeks_left = return_round - current_round
                if weeks_left <= 0:
                    status = "✅ Recovered"
                else:
                    week_text = "week" if weeks_left == 1 else "weeks"
                    season_indicator = " (SEASON)" if return_round > total_rounds else ""
                    status = f"- {weeks_left} {week_text}{season_indicator}"
            line = f"{team_display}{name} ({overall_rating}) - {injury_type} {status}"
            if injury_round == new_this_round:
                line = f"**{line}**"
            combined_list.append(line)

    if suspensions:
        if injuries:
            combined_list.append("")
        combined_list.append("**🚫 Suspensions:**")
        for name, overall_rating, suspension_reason, games_remaining, team_name, emoji_id, team_id, suspension_round in suspensions:
            team_display = get_team_emoji_str(bot, emoji_id) if team_name and not filter_team_id else ""
            if games_remaining is None:
                # Suspension length not yet determined - the round the
                # report happened in hasn't finished being advanced past
                # yet (see season_commands.py's _roll_pending_report_suspensions).
                status = "- TBC"
            elif games_remaining <= 0:
                status = "✅ Available"
            else:
                # No "(SEASON)" indicator here unlike injuries - games_remaining
                # only ticks down on rounds the team actually plays, so whether
                # it spills into next season depends on how many fixtures are
                # left, not a round-number comparison against total_rounds.
                game_text = "game" if games_remaining == 1 else "games"
                status = f"- {games_remaining} {game_text}"
            # Impact grading (" - low/medium/high impact") drives the
            # games-range roll (see match_sim.py's REPORT_REASONS /
            # season_commands.py's _REPORT_GAMES_RANGE_BY_CHARGE) but isn't
            # shown here - the list reads "striking", not "striking - low
            # impact".
            display_reason = re.sub(r' - (low|medium|high) impact$', '', suspension_reason)
            line = f"{team_display}{name} ({overall_rating}) - {display_reason} {status}"
            if suspension_round == new_this_round:
                line = f"**{line}**"
            combined_list.append(line)

    return combined_list


def _chunk_lines_into_descriptions(lines, max_length=4000):
    """Splits `lines` into groups that each join (with "\\n") under
    max_length chars - never splitting a single line in half, only ever
    breaking between whole lines. max_length leaves headroom below
    Discord's actual 4096-char embed description cap. Used to post a long
    combined injury+suspension list as several separate embeds (each with
    its own plain description) instead of splitting into multiple FIELDS
    within one embed - a field boundary renders as visible extra spacing
    in Discord wherever it happens to land (driven purely by character
    count, so it could fall in the middle of a team's block), which read
    as random gaps between lines. A whole extra embed's own natural
    spacing, by contrast, is expected/normal Discord rendering, and only
    ever appears where a real length limit forced a split."""
    chunks = []
    current_chunk = []
    current_length = 0

    for line in lines:
        line_length = len(line) + 1  # +1 for the joining newline
        if current_chunk and current_length + line_length > max_length:
            chunks.append(current_chunk)
            current_chunk = []
            current_length = 0
        current_chunk.append(line)
        current_length += line_length

    if current_chunk:
        chunks.append(current_chunk)

    return ["\n".join(chunk) for chunk in chunks]


class InjuryCommands(commands.Cog):
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

    async def injured_player_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for currently injured players only"""
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                """SELECT p.player_id, p.name, p.position, p.age, p.overall_rating, t.team_name
                   FROM players p
                   LEFT JOIN teams t ON p.team_id = t.team_id
                   INNER JOIN injuries i ON p.player_id = i.player_id
                   WHERE i.status = 'injured'
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
        """Check if user has admin permissions - every command in this cog is admin-only."""
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

    @app_commands.command(name="addinjury", description="[ADMIN] Add an injury to a player")
    @app_commands.describe(
        player_name="Player name",
        injury_type="Type of injury",
        recovery_rounds="Number of rounds until recovery"
    )
    @app_commands.autocomplete(player_name=player_name_autocomplete)
    async def add_injury(
        self,
        interaction: discord.Interaction,
        player_name: str,
        injury_type: str,
        recovery_rounds: int
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

            # Check if player is already injured
            cursor = await db.execute(
                """SELECT injury_id FROM injuries
                   WHERE player_id = ? AND status = 'injured'""",
                (player_id,)
            )
            existing = await cursor.fetchone()

            if existing:
                await interaction.response.send_message(
                    f"❌ **{p_name}** is already injured! Use `/editinjury` to modify it.",
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

            # Calculate return round - +1 beyond the round the injury happened
            # in plus the recovery weeks, since the injury round itself is
            # already missed (the player got hurt mid-match) on top of the
            # stated recovery time, not counted as part of it. E.g. a 2-week
            # injury in Round 6 should miss Rounds 7 and 8 (Round 6 itself
            # already happened - the player was hurt DURING it, so it isn't
            # something they can additionally "miss") and return in Round 9 -
            # not return_round = 6 + 2 = 8 (which would only give 1 full
            # round of actual recovery, 7, since Round 6 is already over).
            return_round = current_round + recovery_rounds + 1

            # Add injury
            await db.execute(
                """INSERT INTO injuries (player_id, injury_type, injury_round, recovery_rounds, return_round, status)
                   VALUES (?, ?, ?, ?, ?, 'injured')""",
                (player_id, injury_type, current_round, recovery_rounds, return_round)
            )
            await db.commit()

            # Get total rounds and regular_rounds to check if season-ending
            cursor = await db.execute(
                "SELECT total_rounds, regular_rounds FROM seasons WHERE status = 'active' LIMIT 1"
            )
            season_info = await cursor.fetchone()
            total_rounds = season_info[0] if season_info else 0
            regular_rounds = season_info[1] if season_info else 24

            # Format expected return
            if return_round > total_rounds:
                expected_return = "SEASON"
            else:
                expected_return = get_round_name(return_round, regular_rounds)

            # Send response
            await interaction.response.send_message(
                f"🚑 **{p_name}** has been injured!\n"
                f"• Injury: {injury_type}\n"
                f"• Recovery: {recovery_rounds} round{'s' if recovery_rounds != 1 else ''}\n"
                f"• Expected return: {expected_return}",
                ephemeral=True
            )

    async def post_injury_list_to_channel(self, db, channel_id: int):
        """Helper function to post the injury list to a specified channel"""
        # Get current round, total rounds, and regular_rounds
        cursor = await db.execute(
            "SELECT season_id, current_round, total_rounds, regular_rounds FROM seasons WHERE status = 'active' LIMIT 1"
        )
        season_info = await cursor.fetchone()
        season_id = season_info[0] if season_info else None
        current_round = season_info[1] if season_info else 0
        total_rounds = season_info[2] if season_info else 0
        regular_rounds = season_info[3] if season_info else 24

        # This posts right after advance_to_next_round has already
        # incremented current_round - so the round whose injuries/
        # suspensions should be bolded as "new" is the one that just
        # finished, current_round - 1, not current_round itself.
        combined_list = await build_injury_suspension_list(
            self.bot, db, current_round, total_rounds,
            season_id=season_id, regular_rounds=regular_rounds, new_this_round=current_round - 1,
        )

        if not combined_list:
            # No injuries or suspensions to post
            return

        # Get the round name
        round_display = get_round_name(current_round, regular_rounds) if current_round > 0 else "Offseason"

        # A single description is capped at 4096 chars by Discord - with
        # enough concurrent injuries/suspensions across the league that's
        # exceedable (each line runs ~30-60 chars). Split into several
        # embeds (each its own plain description), never multiple FIELDS
        # within one embed - a field boundary renders as visible extra
        # spacing wherever it happens to land (character-count-driven, so
        # it could land mid-team), which read as random gaps between
        # lines. Only the first embed gets the title; Discord sends up to
        # 10 embeds in one message, comfortably covering any realistic
        # league size.
        descriptions = _chunk_lines_into_descriptions(combined_list, max_length=4000)
        embeds = []
        for i, description in enumerate(descriptions):
            embeds.append(discord.Embed(
                title=f"Injury & Suspension List - {round_display}" if i == 0 else None,
                description=description,
                color=discord.Color.red(),
            ))

        # Post to channel
        channel = self.bot.get_channel(channel_id)
        if channel:
            await channel.send(embeds=embeds)

    @app_commands.command(name="editinjury", description="[ADMIN] Edit a player's injury")
    @app_commands.describe(
        player_name="Player name",
        new_injury_type="New injury type (optional)",
        new_recovery_rounds="New recovery rounds (optional)"
    )
    @app_commands.autocomplete(player_name=injured_player_autocomplete)
    async def edit_injury(
        self,
        interaction: discord.Interaction,
        player_name: str,
        new_injury_type: str = None,
        new_recovery_rounds: int = None
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

            # Find active injury
            cursor = await db.execute(
                """SELECT injury_id, injury_type, injury_round, recovery_rounds, return_round
                   FROM injuries
                   WHERE player_id = ? AND status = 'injured'""",
                (player_id,)
            )
            injury = await cursor.fetchone()

            if not injury:
                await interaction.response.send_message(
                    f"❌ **{p_name}** has no active injury!",
                    ephemeral=True
                )
                return

            injury_id, old_injury_type, injury_round, old_recovery, old_return_round = injury

            # Get current round
            cursor = await db.execute(
                "SELECT current_round FROM seasons WHERE status = 'active' LIMIT 1"
            )
            season_info = await cursor.fetchone()
            current_round = season_info[0] if season_info else 0

            # Calculate current recovery time remaining - old_return_round
            # is NULL while recovery length is still TBC (see
            # season_commands.py's _roll_pending_injury_recoveries)
            old_recovery_remaining = None if old_return_round is None else old_return_round - current_round

            # Update fields
            updates = []
            values = []
            changes = []

            if new_injury_type:
                updates.append("injury_type = ?")
                values.append(new_injury_type)
                changes.append(f"Injury: {old_injury_type} → {new_injury_type}")

            if new_recovery_rounds:
                # Calculate return round from current round, not injury round.
                # Deliberately NO +1 here unlike a fresh injury's return_round
                # calc (see add_injury) - editing "to 2 weeks" during the
                # CURRENT round means miss this round plus 1 more, back the
                # round after (current_round + 2), since the admin is
                # resetting the clock starting now, not simulating a fresh
                # injury that also separately eats its own occurrence round.
                new_return_round = current_round + new_recovery_rounds
                updates.append("recovery_rounds = ?, return_round = ?")
                values.extend([new_recovery_rounds, new_return_round])
                old_display = "TBC" if old_recovery_remaining is None else f"{old_recovery_remaining} {'week' if old_recovery_remaining == 1 else 'weeks'}"
                new_week_text = "week" if new_recovery_rounds == 1 else "weeks"
                changes.append(f"Recovery: {old_display} → {new_recovery_rounds} {new_week_text}")

            if not updates:
                await interaction.response.send_message(
                    "❌ No updates specified!",
                    ephemeral=True
                )
                return

            # Perform update
            values.append(injury_id)
            query = f"UPDATE injuries SET {', '.join(updates)} WHERE injury_id = ?"

            await db.execute(query, values)
            await db.commit()

            response = f"✅ Updated injury for **{p_name}**\n\n"
            response += "\n".join(changes)

            await interaction.response.send_message(response, ephemeral=True)

    @app_commands.command(name="removeinjury", description="[ADMIN] Remove a player's injury")
    @app_commands.describe(player_name="Player name")
    @app_commands.autocomplete(player_name=injured_player_autocomplete)
    async def remove_injury(self, interaction: discord.Interaction, player_name: str):
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

            # Find and remove active injury
            cursor = await db.execute(
                """SELECT injury_id FROM injuries
                   WHERE player_id = ? AND status = 'injured'""",
                (player_id,)
            )
            injury = await cursor.fetchone()

            if not injury:
                await interaction.response.send_message(
                    f"❌ **{p_name}** has no active injury!",
                    ephemeral=True
                )
                return

            # Recovered - remove the injury record
            await db.execute(
                "DELETE FROM injuries WHERE injury_id = ?",
                (injury[0],)
            )
            await db.commit()

            await interaction.response.send_message(
                f"✅ **{p_name}** has recovered from injury!",
                ephemeral=True
            )


async def setup(bot):
    await bot.add_cog(InjuryCommands(bot))
