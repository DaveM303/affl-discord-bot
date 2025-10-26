import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
import json
from config import DB_PATH, ADMIN_ROLE_ID

class TradeCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def is_admin(self, interaction: discord.Interaction) -> bool:
        """Check if user has admin permissions"""
        if interaction.guild.owner_id == interaction.user.id:
            return True

        if ADMIN_ROLE_ID:
            member = interaction.guild.get_member(interaction.user.id) or interaction.user
            if member:
                admin_role_id = int(ADMIN_ROLE_ID) if isinstance(ADMIN_ROLE_ID, str) else ADMIN_ROLE_ID
                if any(role.id == admin_role_id for role in member.roles):
                    return True

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

        return False

    async def get_user_team(self, user_id, guild):
        """Get the team associated with a user based on their role"""
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT team_id, team_name, role_id FROM teams WHERE role_id IS NOT NULL")
            teams = await cursor.fetchall()

        user = guild.get_member(user_id)
        if not user:
            return None, None

        for team_id, team_name, role_id in teams:
            role = guild.get_role(int(role_id))
            if role and role in user.roles:
                return team_id, team_name

        return None, None

    @app_commands.command(name="tradeperiod", description="[ADMIN] Start or end the trade period")
    @app_commands.describe(action="Start or end the trade period")
    @app_commands.choices(action=[
        app_commands.Choice(name="Start", value="start"),
        app_commands.Choice(name="End", value="end"),
    ])
    async def trade_period(self, interaction: discord.Interaction, action: str):
        if not await self.is_admin(interaction):
            await interaction.response.send_message(
                "❌ You need admin permissions to use this command.",
                ephemeral=True
            )
            return

        async with aiosqlite.connect(DB_PATH) as db:
            if action == "start":
                await db.execute(
                    "INSERT OR REPLACE INTO settings (setting_key, setting_value) VALUES (?, ?)",
                    ("trade_period_active", "1")
                )
                await db.commit()
                await interaction.response.send_message("✅ **Trade period has been opened!** Coaches can now submit trade offers.")
            else:
                await db.execute(
                    "INSERT OR REPLACE INTO settings (setting_key, setting_value) VALUES (?, ?)",
                    ("trade_period_active", "0")
                )
                await db.commit()
                await interaction.response.send_message("✅ **Trade period has been closed!** Coaches can no longer submit trade offers.")

    async def team_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for team names (exclude user's own team)"""
        # Get user's team
        user_team_id, _ = await self.get_user_team(interaction.user.id, interaction.guild)

        async with aiosqlite.connect(DB_PATH) as db:
            if user_team_id:
                cursor = await db.execute(
                    "SELECT team_name FROM teams WHERE team_id != ? ORDER BY team_name",
                    (user_team_id,)
                )
            else:
                cursor = await db.execute("SELECT team_name FROM teams ORDER BY team_name")
            teams = await cursor.fetchall()

        # Filter teams based on what the user has typed
        choices = []
        for (team_name,) in teams:
            if current.lower() in team_name.lower():
                choices.append(app_commands.Choice(name=team_name, value=team_name))

        # Return up to 25 choices (Discord limit)
        return choices[:25]

    @app_commands.command(name="tradeoffer", description="Create a trade offer with another team")
    @app_commands.describe(team="Team you want to trade with")
    @app_commands.autocomplete(team=team_autocomplete)
    async def trade_offer(self, interaction: discord.Interaction, team: str):
        # Check if trade period is active
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT setting_value FROM settings WHERE setting_key = 'trade_period_active'"
            )
            result = await cursor.fetchone()

        if not result or result[0] != "1":
            await interaction.response.send_message(
                "❌ The trade period is not currently active!",
                ephemeral=True
            )
            return

        # Get user's team
        team_id, team_name = await self.get_user_team(interaction.user.id, interaction.guild)
        if not team_id:
            await interaction.response.send_message(
                "❌ You don't have a team role!",
                ephemeral=True
            )
            return

        # Get receiving team info
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT team_id, team_name FROM teams WHERE team_name = ?",
                (team,)
            )
            receiving_team = await cursor.fetchone()

        if not receiving_team:
            await interaction.response.send_message(
                f"❌ Team '{team}' not found. Please select from autocomplete suggestions.",
                ephemeral=True
            )
            return

        receiving_team_id, receiving_team_name = receiving_team

        if receiving_team_id == team_id:
            await interaction.response.send_message(
                "❌ You can't trade with yourself!",
                ephemeral=True
            )
            return

        # Create trade menu with receiving team pre-selected
        view = TradeOfferView(
            team_id,
            team_name,
            interaction.user.id,
            self.bot,
            interaction.guild,
            receiving_team_id=receiving_team_id
        )
        await view.initialize()
        embed = view.create_embed()

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="trademenu", description="View and manage your team's trade offers")
    async def trade_menu(self, interaction: discord.Interaction):
        # Get user's team
        team_id, team_name = await self.get_user_team(interaction.user.id, interaction.guild)
        if not team_id:
            await interaction.response.send_message(
                "❌ You don't have a team role!",
                ephemeral=True
            )
            return

        # Create trade menu
        view = TradeMenuView(team_id, team_name, self.bot, interaction.guild)
        embed = await view.create_embed()

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class TradeMenuView(discord.ui.View):
    """Central hub for viewing and managing trades"""
    def __init__(self, team_id, team_name, bot, guild):
        super().__init__(timeout=600)
        self.team_id = team_id
        self.team_name = team_name
        self.bot = bot
        self.guild = guild

    async def create_embed(self):
        """Create the main trade menu embed"""
        embed = discord.Embed(
            title="📊 Trade Management",
            description="━━━━━━━━━━━━━━━━━━",
            color=discord.Color.blue()
        )

        async with aiosqlite.connect(DB_PATH) as db:
            # Get incoming offers count and details
            cursor = await db.execute(
                """SELECT COUNT(*),
                   SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN status = 'accepted' THEN 1 ELSE 0 END)
                   FROM trades
                   WHERE receiving_team_id = ? AND status IN ('pending', 'accepted')""",
                (self.team_id,)
            )
            incoming_result = await cursor.fetchone()
            incoming_total = incoming_result[0] or 0
            incoming_pending = incoming_result[1] or 0
            incoming_accepted = incoming_result[2] or 0

            # Get outgoing offers count and details
            cursor = await db.execute(
                """SELECT COUNT(*),
                   SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN status = 'accepted' THEN 1 ELSE 0 END)
                   FROM trades
                   WHERE initiating_team_id = ? AND status IN ('pending', 'accepted')""",
                (self.team_id,)
            )
            outgoing_result = await cursor.fetchone()
            outgoing_total = outgoing_result[0] or 0
            outgoing_pending = outgoing_result[1] or 0
            outgoing_accepted = outgoing_result[2] or 0

            # Get recent incoming offers (up to 3)
            cursor = await db.execute(
                """SELECT t.team_name, t.emoji_id, tr.status
                   FROM trades tr
                   JOIN teams t ON tr.initiating_team_id = t.team_id
                   WHERE tr.receiving_team_id = ? AND tr.status IN ('pending', 'accepted')
                   ORDER BY tr.created_at DESC
                   LIMIT 3""",
                (self.team_id,)
            )
            incoming_offers = await cursor.fetchall()

            # Get recent outgoing offers (up to 3)
            cursor = await db.execute(
                """SELECT t.team_name, t.emoji_id, tr.status
                   FROM trades tr
                   JOIN teams t ON tr.receiving_team_id = t.team_id
                   WHERE tr.initiating_team_id = ? AND tr.status IN ('pending', 'accepted', 'declined')
                   ORDER BY tr.created_at DESC
                   LIMIT 3""",
                (self.team_id,)
            )
            outgoing_offers = await cursor.fetchall()

        # Add summary
        summary = f"**Incoming:** {incoming_total} active"
        if incoming_pending:
            summary += f" ({incoming_pending} 🟡 pending"
            if incoming_accepted:
                summary += f", {incoming_accepted} 🟢 awaiting approval)"
            else:
                summary += ")"
        elif incoming_accepted:
            summary += f" ({incoming_accepted} 🟢 awaiting approval)"

        summary += f"\n**Outgoing:** {outgoing_total} active"
        if outgoing_pending:
            summary += f" ({outgoing_pending} 🟡 pending"
            if outgoing_accepted:
                summary += f", {outgoing_accepted} 🟢 awaiting approval)"
            else:
                summary += ")"
        elif outgoing_accepted:
            summary += f" ({outgoing_accepted} 🟢 awaiting approval)"

        embed.add_field(name="Overview", value=summary, inline=False)

        # Add recent incoming offers preview
        if incoming_offers:
            incoming_text = []
            for team_name, emoji_id, status in incoming_offers:
                emoji = self.bot.get_emoji(int(emoji_id)) if emoji_id else None
                emoji_str = f"{emoji} " if emoji else ""
                status_icon = "🟡" if status == "pending" else "🟢"
                status_text = "Pending" if status == "pending" else "Awaiting Approval"
                incoming_text.append(f"{status_icon} {emoji_str}**{team_name}** → {status_text}")
            embed.add_field(
                name="Recent Incoming Offers",
                value="\n".join(incoming_text),
                inline=False
            )

        # Add recent outgoing offers preview
        if outgoing_offers:
            outgoing_text = []
            for team_name, emoji_id, status in outgoing_offers:
                emoji = self.bot.get_emoji(int(emoji_id)) if emoji_id else None
                emoji_str = f"{emoji} " if emoji else ""
                if status == "pending":
                    status_icon = "🟡"
                    status_text = "Pending"
                elif status == "accepted":
                    status_icon = "🟢"
                    status_text = "Awaiting Approval"
                else:  # declined
                    status_icon = "🔴"
                    status_text = "Declined"
                outgoing_text.append(f"{status_icon} {emoji_str}**{team_name}** → {status_text}")
            embed.add_field(
                name="Recent Outgoing Offers",
                value="\n".join(outgoing_text),
                inline=False
            )

        return embed

    @discord.ui.button(label="View Incoming Offers", style=discord.ButtonStyle.primary, row=0)
    async def view_incoming_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.show_incoming_offers(interaction)

    @discord.ui.button(label="View Outgoing Offers", style=discord.ButtonStyle.primary, row=0)
    async def view_outgoing_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.show_outgoing_offers(interaction)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, row=1)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = await self.create_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def show_incoming_offers(self, interaction: discord.Interaction):
        """Show detailed list of incoming offers"""
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                """SELECT tr.trade_id, t1.team_name, t1.emoji_id, tr.initiating_players, tr.receiving_players, tr.status
                   FROM trades tr
                   JOIN teams t1 ON tr.initiating_team_id = t1.team_id
                   WHERE tr.receiving_team_id = ? AND tr.status IN ('pending', 'accepted')
                   ORDER BY tr.created_at DESC""",
                (self.team_id,)
            )
            offers = await cursor.fetchall()

            if not offers:
                await interaction.response.send_message("📭 You have no active incoming trade offers.", ephemeral=True)
                return

            # Build embed with all incoming offers
            embed = discord.Embed(
                title="📥 Incoming Trade Offers",
                color=discord.Color.blue()
            )

            for trade_id, team_name, emoji_id, init_players_json, recv_players_json, status in offers:
                # Get emojis
                team_emoji = self.bot.get_emoji(int(emoji_id)) if emoji_id else None
                team_emoji_str = f"{team_emoji} " if team_emoji else ""

                # Status indicator
                if status == "pending":
                    status_icon = "🟡"
                    status_text = "Pending"
                elif status == "accepted":
                    status_icon = "🟢"
                    status_text = "Awaiting Approval"

                # Get player names
                init_players = json.loads(init_players_json) if init_players_json else []
                recv_players = json.loads(recv_players_json) if recv_players_json else []

                init_names = []
                recv_names = []

                if init_players:
                    placeholders = ','.join('?' * len(init_players))
                    cursor = await db.execute(
                        f"SELECT name FROM players WHERE player_id IN ({placeholders})",
                        init_players
                    )
                    init_names = [name for (name,) in await cursor.fetchall()]

                if recv_players:
                    placeholders = ','.join('?' * len(recv_players))
                    cursor = await db.execute(
                        f"SELECT name FROM players WHERE player_id IN ({placeholders})",
                        recv_players
                    )
                    recv_names = [name for (name,) in await cursor.fetchall()]

                # Format offer
                you_get = ", ".join(init_names) if init_names else "Nothing"
                they_get = ", ".join(recv_names) if recv_names else "Nothing"

                embed.add_field(
                    name=f"{status_icon} {team_emoji_str}**{team_name}** • {status_text}",
                    value=f"**You receive:** {you_get}\n**They receive:** {they_get}",
                    inline=False
                )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def show_outgoing_offers(self, interaction: discord.Interaction):
        """Show detailed list of outgoing offers"""
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                """SELECT tr.trade_id, t2.team_name, t2.emoji_id, tr.initiating_players, tr.receiving_players, tr.status
                   FROM trades tr
                   JOIN teams t2 ON tr.receiving_team_id = t2.team_id
                   WHERE tr.initiating_team_id = ? AND tr.status IN ('pending', 'accepted', 'declined')
                   ORDER BY tr.created_at DESC""",
                (self.team_id,)
            )
            offers = await cursor.fetchall()

            if not offers:
                await interaction.response.send_message("📭 You have no active outgoing trade offers.", ephemeral=True)
                return

            # Build embed with all outgoing offers
            embed = discord.Embed(
                title="📤 Outgoing Trade Offers",
                color=discord.Color.orange()
            )

            for trade_id, team_name, emoji_id, init_players_json, recv_players_json, status in offers:
                # Get emojis
                team_emoji = self.bot.get_emoji(int(emoji_id)) if emoji_id else None
                team_emoji_str = f"{team_emoji} " if team_emoji else ""

                # Status indicator
                if status == "pending":
                    status_icon = "🟡"
                    status_text = "Pending"
                elif status == "accepted":
                    status_icon = "🟢"
                    status_text = "Awaiting Approval"
                else:  # declined
                    status_icon = "🔴"
                    status_text = "Declined"

                # Get player names
                init_players = json.loads(init_players_json) if init_players_json else []
                recv_players = json.loads(recv_players_json) if recv_players_json else []

                init_names = []
                recv_names = []

                if init_players:
                    placeholders = ','.join('?' * len(init_players))
                    cursor = await db.execute(
                        f"SELECT name FROM players WHERE player_id IN ({placeholders})",
                        init_players
                    )
                    init_names = [name for (name,) in await cursor.fetchall()]

                if recv_players:
                    placeholders = ','.join('?' * len(recv_players))
                    cursor = await db.execute(
                        f"SELECT name FROM players WHERE player_id IN ({placeholders})",
                        recv_players
                    )
                    recv_names = [name for (name,) in await cursor.fetchall()]

                # Format offer
                you_give = ", ".join(init_names) if init_names else "Nothing"
                you_get = ", ".join(recv_names) if recv_names else "Nothing"

                embed.add_field(
                    name=f"{status_icon} {team_emoji_str}**{team_name}** • {status_text}",
                    value=f"**You offer:** {you_give}\n**You receive:** {you_get}",
                    inline=False
                )

        await interaction.response.send_message(embed=embed, ephemeral=True)


class TradeOfferView(discord.ui.View):
    """Streamlined trade offer interface - everything updates in one message"""
    def __init__(self, initiating_team_id, initiating_team_name, user_id, bot, guild, is_counter_offer=False, original_trade_id=None, receiving_team_id=None):
        super().__init__(timeout=600)
        self.initiating_team_id = initiating_team_id
        self.initiating_team_name = initiating_team_name
        self.user_id = user_id
        self.bot = bot
        self.guild = guild
        self.is_counter_offer = is_counter_offer
        self.original_trade_id = original_trade_id
        self.receiving_team_id = receiving_team_id
        self.receiving_team_name = None
        self.initiating_emoji = None
        self.receiving_emoji = None
        self.initiating_emoji_obj = None
        self.receiving_emoji_obj = None
        self.initiating_players = []  # List of player IDs
        self.receiving_players = []   # List of player IDs
        self.initiating_roster = []
        self.receiving_roster = []
        self.initiating_page = 0  # Current page for initiating team roster
        self.receiving_page = 0   # Current page for receiving team roster

    def get_emoji(self, emoji_id, as_string=False):
        """Get emoji from ID

        Args:
            emoji_id: The emoji ID to fetch
            as_string: If True, returns emoji as string (for embeds). If False, returns emoji object (for select menus)
        """
        if not emoji_id:
            return None
        try:
            emoji = self.bot.get_emoji(int(emoji_id))
            if emoji:
                return str(emoji) if as_string else emoji
            return None
        except:
            return None

    async def initialize(self):
        """Load initial data"""
        async with aiosqlite.connect(DB_PATH) as db:
            # Get initiating team info (with emoji)
            cursor = await db.execute(
                "SELECT emoji_id FROM teams WHERE team_id = ?",
                (self.initiating_team_id,)
            )
            result = await cursor.fetchone()
            if result:
                self.initiating_emoji = self.get_emoji(result[0], as_string=True)
                self.initiating_emoji_obj = self.get_emoji(result[0], as_string=False)

            # Get initiating team roster (include age, order by OVR desc)
            cursor = await db.execute(
                "SELECT player_id, name, position, overall_rating, age FROM players WHERE team_id = ? ORDER BY overall_rating DESC",
                (self.initiating_team_id,)
            )
            self.initiating_roster = await cursor.fetchall()

            # If receiving team is set, get their roster and name
            if self.receiving_team_id:
                cursor = await db.execute(
                    "SELECT team_name, emoji_id FROM teams WHERE team_id = ?",
                    (self.receiving_team_id,)
                )
                result = await cursor.fetchone()
                if result:
                    self.receiving_team_name = result[0]
                    self.receiving_emoji = self.get_emoji(result[1], as_string=True)
                    self.receiving_emoji_obj = self.get_emoji(result[1], as_string=False)

                cursor = await db.execute(
                    "SELECT player_id, name, position, overall_rating, age FROM players WHERE team_id = ? ORDER BY overall_rating DESC",
                    (self.receiving_team_id,)
                )
                self.receiving_roster = await cursor.fetchall()

        # Add components
        self.add_components()

    def add_components(self):
        """Add all UI components"""
        self.clear_items()

        current_row = 0

        # Player selection dropdown for initiating team (row 0)
        if self.initiating_roster:
            offering_select = OfferingPlayerSelect(self, row=current_row)
            self.add_item(offering_select)
            current_row += 1

            # Pagination button for initiating team if needed (row 1)
            if len(self.initiating_roster) > 25:
                next_page_btn = discord.ui.Button(
                    label=f"Page {self.initiating_page + 1}/{(len(self.initiating_roster) - 1) // 25 + 1}",
                    style=discord.ButtonStyle.secondary,
                    row=current_row
                )
                next_page_btn.callback = self.next_initiating_page
                self.add_item(next_page_btn)
                current_row += 1

        # Player selection dropdown for receiving team (row 2 or 1)
        if self.receiving_team_id and self.receiving_roster:
            receiving_select = ReceivingPlayerSelect(self, row=current_row)
            self.add_item(receiving_select)
            current_row += 1

            # Pagination button for receiving team if needed (row 3 or 2)
            if len(self.receiving_roster) > 25:
                next_page_btn = discord.ui.Button(
                    label=f"Page {self.receiving_page + 1}/{(len(self.receiving_roster) - 1) // 25 + 1}",
                    style=discord.ButtonStyle.secondary,
                    row=current_row
                )
                next_page_btn.callback = self.next_receiving_page
                self.add_item(next_page_btn)
                current_row += 1

        # Action buttons (last row)
        clear_btn = discord.ui.Button(label="Clear All", style=discord.ButtonStyle.secondary, row=current_row)
        clear_btn.callback = self.clear_callback
        self.add_item(clear_btn)

        send_btn = discord.ui.Button(label="Send Offer", style=discord.ButtonStyle.success, row=current_row)
        send_btn.callback = self.send_callback
        self.add_item(send_btn)

        cancel_btn = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.danger, row=current_row)
        cancel_btn.callback = self.cancel_callback
        self.add_item(cancel_btn)

    def create_embed(self):
        """Create the trade offer embed"""
        embed = discord.Embed(
            title=":arrows_clockwise: Create Trade Offer" if not self.is_counter_offer else ":arrows_clockwise: Create Counter Offer",
            color=discord.Color.blue()
        )

        # Get player names
        offering_names = []
        receiving_names = []

        if self.initiating_players:
            for player_id in self.initiating_players:
                player = next((p for p in self.initiating_roster if p[0] == player_id), None)
                if player:
                    _, name, pos, ovr, age = player
                    offering_names.append(f"**{name}** ({pos}, {age}, {ovr})")

        if self.receiving_players:
            for player_id in self.receiving_players:
                player = next((p for p in self.receiving_roster if p[0] == player_id), None)
                if player:
                    _, name, pos, ovr, age = player
                    receiving_names.append(f"**{name}** ({pos}, {age}, {ovr})")

        # Show what each team receives (emoji only, or "Select team" if not selected)
        receiving_field_name = self.receiving_emoji if self.receiving_emoji else "Select team"
        initiating_field_name = self.initiating_emoji if self.initiating_emoji else self.initiating_team_name

        embed.add_field(
            name=f"**{receiving_field_name} receive:**",
            value="\n".join(offering_names) if offering_names else "*No players selected*",
            inline=True
        )

        embed.add_field(
            name=f"**{initiating_field_name} receive:**",
            value="\n".join(receiving_names) if receiving_names else "*No players selected*",
            inline=True
        )

        return embed

    async def update_view(self, interaction: discord.Interaction):
        """Update the view after changes"""
        # Refresh rosters if team changed
        if self.receiving_team_id and not self.receiving_roster:
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "SELECT player_id, name, position, overall_rating, age FROM players WHERE team_id = ? ORDER BY overall_rating DESC",
                    (self.receiving_team_id,)
                )
                self.receiving_roster = await cursor.fetchall()

        self.add_components()
        embed = self.create_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def next_initiating_page(self, interaction: discord.Interaction):
        """Go to next page of initiating team roster"""
        max_page = (len(self.initiating_roster) - 1) // 25
        self.initiating_page = (self.initiating_page + 1) % (max_page + 1)
        await self.update_view(interaction)

    async def next_receiving_page(self, interaction: discord.Interaction):
        """Go to next page of receiving team roster"""
        max_page = (len(self.receiving_roster) - 1) // 25
        self.receiving_page = (self.receiving_page + 1) % (max_page + 1)
        await self.update_view(interaction)

    async def clear_callback(self, interaction: discord.Interaction):
        """Clear all selections"""
        self.initiating_players = []
        self.receiving_players = []
        await self.update_view(interaction)

    async def cancel_callback(self, interaction: discord.Interaction):
        """Cancel the trade offer"""
        await interaction.response.edit_message(content="Trade offer cancelled.", embed=None, view=None)

    async def send_callback(self, interaction: discord.Interaction):
        """Send the trade offer"""
        # Validate
        if not self.receiving_team_id:
            await interaction.response.send_message("❌ Please select a team to trade with!", ephemeral=True)
            return

        if not self.initiating_players and not self.receiving_players:
            await interaction.response.send_message("❌ Please add at least one player to the trade!", ephemeral=True)
            return

        # Store trade in database
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                """INSERT INTO trades (initiating_team_id, receiving_team_id, initiating_players,
                                      receiving_players, status, created_by_user_id, original_trade_id)
                   VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
                (
                    self.initiating_team_id,
                    self.receiving_team_id,
                    json.dumps(self.initiating_players),
                    json.dumps(self.receiving_players),
                    str(self.user_id),
                    self.original_trade_id
                )
            )
            trade_id = cursor.lastrowid

            # Get receiving team info
            cursor = await db.execute(
                "SELECT channel_id, role_id FROM teams WHERE team_id = ?",
                (self.receiving_team_id,)
            )
            result = await cursor.fetchone()
            receiving_channel_id, receiving_role_id = result

            await db.commit()

        # Send to receiving team channel
        if receiving_channel_id:
            channel = self.bot.get_channel(int(receiving_channel_id))
            if channel:
                # Create title with emoji and team name
                initiating_emoji_str = f"{self.initiating_emoji} " if self.initiating_emoji else ""
                if not self.is_counter_offer:
                    title = f"{initiating_emoji_str}**{self.initiating_team_name}** have sent you a trade offer!"
                else:
                    title = f"{initiating_emoji_str}**{self.initiating_team_name}** have sent you a counter-offer!"

                embed = discord.Embed(
                    title=title,
                    color=discord.Color.gold()
                )

                # Get player names for embed
                offering_names = []
                receiving_names = []

                if self.initiating_players:
                    for player_id in self.initiating_players:
                        player = next((p for p in self.initiating_roster if p[0] == player_id), None)
                        if player:
                            _, name, pos, ovr, age = player
                            offering_names.append(f"**{name}** ({pos}, {age}, {ovr})")

                if self.receiving_players:
                    for player_id in self.receiving_players:
                        player = next((p for p in self.receiving_roster if p[0] == player_id), None)
                        if player:
                            _, name, pos, ovr, age = player
                            receiving_names.append(f"**{name}** ({pos}, {age}, {ovr})")

                receiving_emoji_str = f"{self.receiving_emoji} " if self.receiving_emoji else ""

                embed.add_field(
                    name=f"**{receiving_emoji_str}receive:**",
                    value="\n".join(offering_names) if offering_names else "*Nothing*",
                    inline=True
                )

                embed.add_field(
                    name=f"**{initiating_emoji_str}receive:**",
                    value="\n".join(receiving_names) if receiving_names else "*Nothing*",
                    inline=True
                )

                # Add response buttons
                view = TradeResponseView(trade_id, self.bot)

                # Ping the team role
                role_mention = f"<@&{receiving_role_id}>" if receiving_role_id else ""
                await channel.send(role_mention, embed=embed, view=view)

        # Cancel original trade if this is a counter-offer
        if self.is_counter_offer and self.original_trade_id:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE trades SET status = 'countered' WHERE trade_id = ?",
                    (self.original_trade_id,)
                )
                await db.commit()

        await interaction.response.send_message("✅ **Trade offer sent!**", ephemeral=True)


class OfferingPlayerSelect(discord.ui.Select):
    """Select menu for choosing players to offer"""
    def __init__(self, parent_view, row=0):
        self.parent_view = parent_view
        # Get current page of players
        start_idx = parent_view.initiating_page * 25
        end_idx = start_idx + 25
        page_players = parent_view.initiating_roster[start_idx:end_idx]

        options = [
            discord.SelectOption(
                label=f"{name} ({pos}, {age}, {ovr})",
                value=str(player_id),
                default=(player_id in parent_view.initiating_players)
            )
            for player_id, name, pos, ovr, age in page_players
        ]
        super().__init__(
            placeholder=f"{parent_view.initiating_team_name} sends...",
            options=options,
            min_values=0,
            max_values=len(options),
            row=row
        )

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.initiating_players = [int(v) for v in self.values]
        await self.parent_view.update_view(interaction)


class ReceivingPlayerSelect(discord.ui.Select):
    """Select menu for choosing players to receive"""
    def __init__(self, parent_view, row=1):
        self.parent_view = parent_view
        # Get current page of players
        start_idx = parent_view.receiving_page * 25
        end_idx = start_idx + 25
        page_players = parent_view.receiving_roster[start_idx:end_idx]

        options = [
            discord.SelectOption(
                label=f"{name} ({pos}, {age}, {ovr})",
                value=str(player_id),
                default=(player_id in parent_view.receiving_players)
            )
            for player_id, name, pos, ovr, age in page_players
        ]
        super().__init__(
            placeholder=f"{parent_view.receiving_team_name} sends...",
            options=options,
            min_values=0,
            max_values=len(options),
            row=row
        )

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.receiving_players = [int(v) for v in self.values]
        await self.parent_view.update_view(interaction)


class TradeResponseView(discord.ui.View):
    """View for responding to trade offers"""
    def __init__(self, trade_id, bot):
        super().__init__(timeout=None)  # No timeout for trade responses
        self.trade_id = trade_id
        self.bot = bot

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.green, custom_id="accept_trade")
    async def accept_trade(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Verify user has the team role
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                """SELECT receiving_team_id, initiating_team_id, status
                   FROM trades WHERE trade_id = ?""",
                (self.trade_id,)
            )
            result = await cursor.fetchone()

            if not result:
                await interaction.response.send_message("❌ Trade not found!", ephemeral=True)
                return

            receiving_team_id, initiating_team_id, status = result

            if status != 'pending':
                await interaction.response.send_message("❌ This trade is no longer active!", ephemeral=True)
                return

            # Check if user has the receiving team role
            cursor = await db.execute(
                "SELECT role_id FROM teams WHERE team_id = ?",
                (receiving_team_id,)
            )
            role_result = await cursor.fetchone()

        if role_result and role_result[0]:
            role = interaction.guild.get_role(int(role_result[0]))
            if not role or role not in interaction.user.roles:
                await interaction.response.send_message("❌ You don't have permission to accept this trade!", ephemeral=True)
                return

        # Update trade status
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """UPDATE trades SET status = 'accepted', responded_at = CURRENT_TIMESTAMP,
                   responded_by_user_id = ? WHERE trade_id = ?""",
                (str(interaction.user.id), self.trade_id)
            )
            await db.commit()

        # Send to moderator approval channel
        await self.send_to_moderators(interaction)

        # Disable buttons
        for item in self.children:
            item.disabled = True

        await interaction.response.edit_message(view=self)
        await interaction.followup.send("✅ Trade accepted! Sent to moderators for approval.", ephemeral=True)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.red, custom_id="decline_trade")
    async def decline_trade(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Verify user has the team role
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                """SELECT receiving_team_id, initiating_team_id, status, initiating_players, receiving_players
                   FROM trades WHERE trade_id = ?""",
                (self.trade_id,)
            )
            result = await cursor.fetchone()

            if not result:
                await interaction.response.send_message("❌ Trade not found!", ephemeral=True)
                return

            receiving_team_id, initiating_team_id, status, initiating_players_json, receiving_players_json = result

            if status != 'pending':
                await interaction.response.send_message("❌ This trade is no longer active!", ephemeral=True)
                return

            # Check if user has the receiving team role
            cursor = await db.execute(
                "SELECT role_id, team_name, channel_id FROM teams WHERE team_id = ?",
                (receiving_team_id,)
            )
            role_result = await cursor.fetchone()

        if role_result and role_result[0]:
            role = interaction.guild.get_role(int(role_result[0]))
            if not role or role not in interaction.user.roles:
                await interaction.response.send_message("❌ You don't have permission to decline this trade!", ephemeral=True)
                return

        # Update trade status
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """UPDATE trades SET status = 'declined', responded_at = CURRENT_TIMESTAMP,
                   responded_by_user_id = ? WHERE trade_id = ?""",
                (str(interaction.user.id), self.trade_id)
            )

            # Get initiating team info and emojis
            cursor = await db.execute(
                "SELECT channel_id, team_name, emoji_id FROM teams WHERE team_id = ?",
                (initiating_team_id,)
            )
            initiating_team = await cursor.fetchone()

            cursor = await db.execute(
                "SELECT emoji_id FROM teams WHERE team_id = ?",
                (receiving_team_id,)
            )
            receiving_emoji_result = await cursor.fetchone()

            # Get player details
            initiating_players = json.loads(initiating_players_json) if initiating_players_json else []
            receiving_players = json.loads(receiving_players_json) if receiving_players_json else []

            init_player_details = []
            recv_player_details = []

            if initiating_players:
                placeholders = ','.join('?' * len(initiating_players))
                cursor = await db.execute(
                    f"SELECT name, position, overall_rating, age FROM players WHERE player_id IN ({placeholders})",
                    initiating_players
                )
                init_player_details = [f"**{name}** ({pos}, {age}, {ovr})" for name, pos, ovr, age in await cursor.fetchall()]

            if receiving_players:
                placeholders = ','.join('?' * len(receiving_players))
                cursor = await db.execute(
                    f"SELECT name, position, overall_rating, age FROM players WHERE player_id IN ({placeholders})",
                    receiving_players
                )
                recv_player_details = [f"**{name}** ({pos}, {age}, {ovr})" for name, pos, ovr, age in await cursor.fetchall()]

            await db.commit()

        # Notify initiating team with trade details
        if initiating_team and initiating_team[0]:
            channel = self.bot.get_channel(int(initiating_team[0]))
            if channel:
                # Get emojis
                init_emoji = self.bot.get_emoji(int(initiating_team[2])) if initiating_team[2] else None
                recv_emoji = self.bot.get_emoji(int(receiving_emoji_result[0])) if receiving_emoji_result and receiving_emoji_result[0] else None
                init_emoji_str = f"{init_emoji} " if init_emoji else ""
                recv_emoji_str = f"{recv_emoji} " if recv_emoji else ""

                embed = discord.Embed(
                    title=f"❌ Your trade offer to **{role_result[1]}** was declined.",
                    color=discord.Color.red()
                )

                embed.add_field(
                    name=f"**{recv_emoji_str}receive:**",
                    value="\n".join(init_player_details) if init_player_details else "*Nothing*",
                    inline=True
                )

                embed.add_field(
                    name=f"**{init_emoji_str}receive:**",
                    value="\n".join(recv_player_details) if recv_player_details else "*Nothing*",
                    inline=True
                )

                await channel.send(embed=embed)

        # Disable buttons
        for item in self.children:
            item.disabled = True

        await interaction.response.edit_message(view=self)
        await interaction.followup.send("✅ Trade declined.", ephemeral=True)

    @discord.ui.button(label="Counter Offer", style=discord.ButtonStyle.blurple, custom_id="counter_trade")
    async def counter_trade(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Verify user has the team role
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                """SELECT receiving_team_id, initiating_team_id, status, initiating_players, receiving_players
                   FROM trades WHERE trade_id = ?""",
                (self.trade_id,)
            )
            result = await cursor.fetchone()

            if not result:
                await interaction.response.send_message("❌ Trade not found!", ephemeral=True)
                return

            receiving_team_id, initiating_team_id, status, initiating_players_json, receiving_players_json = result

            if status != 'pending':
                await interaction.response.send_message("❌ This trade is no longer active!", ephemeral=True)
                return

            # Check if user has the receiving team role
            cursor = await db.execute(
                "SELECT role_id, team_name FROM teams WHERE team_id = ?",
                (receiving_team_id,)
            )
            role_result = await cursor.fetchone()

        if role_result and role_result[0]:
            role = interaction.guild.get_role(int(role_result[0]))
            if not role or role not in interaction.user.roles:
                await interaction.response.send_message("❌ You don't have permission to counter this trade!", ephemeral=True)
                return

        # Load existing players
        initiating_players = json.loads(initiating_players_json) if initiating_players_json else []
        receiving_players = json.loads(receiving_players_json) if receiving_players_json else []

        # Open counter-offer menu (swap teams and players)
        view = TradeOfferView(
            receiving_team_id,
            role_result[1],
            interaction.user.id,
            self.bot,
            interaction.guild,
            is_counter_offer=True,
            original_trade_id=self.trade_id,
            receiving_team_id=initiating_team_id
        )

        # Swap the players (what they were offering is now what we're asking for)
        view.initiating_players = receiving_players
        view.receiving_players = initiating_players

        await view.initialize()
        embed = view.create_embed()

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def send_to_moderators(self, interaction: discord.Interaction):
        """Send accepted trade to moderators for approval"""
        async with aiosqlite.connect(DB_PATH) as db:
            # Get trade approval channel
            cursor = await db.execute(
                "SELECT setting_value FROM settings WHERE setting_key = 'trade_approval_channel_id'"
            )
            result = await cursor.fetchone()

            if not result or not result[0]:
                return  # No approval channel set

            approval_channel_id = int(result[0])

            # Get trade details
            cursor = await db.execute(
                """SELECT t.initiating_team_id, t.receiving_team_id, t.initiating_players, t.receiving_players,
                          t1.team_name, t1.emoji_id, t2.team_name, t2.emoji_id
                   FROM trades t
                   JOIN teams t1 ON t.initiating_team_id = t1.team_id
                   JOIN teams t2 ON t.receiving_team_id = t2.team_id
                   WHERE t.trade_id = ?""",
                (self.trade_id,)
            )
            trade_result = await cursor.fetchone()

            if not trade_result:
                return

            _, _, initiating_players_json, receiving_players_json, _, init_emoji_id, _, recv_emoji_id = trade_result

            # Get emojis
            init_emoji = self.bot.get_emoji(int(init_emoji_id)) if init_emoji_id else None
            recv_emoji = self.bot.get_emoji(int(recv_emoji_id)) if recv_emoji_id else None
            init_emoji_str = f"{init_emoji} " if init_emoji else ""
            recv_emoji_str = f"{recv_emoji} " if recv_emoji else ""

            # Get player names
            initiating_player_names = []
            receiving_player_names = []

            if initiating_players_json:
                initiating_players = json.loads(initiating_players_json)
                if initiating_players:
                    placeholders = ','.join('?' * len(initiating_players))
                    cursor = await db.execute(
                        f"SELECT name, position, overall_rating, age FROM players WHERE player_id IN ({placeholders})",
                        initiating_players
                    )
                    initiating_player_names = [f"**{name}** ({pos}, {age}, {ovr})" for name, pos, ovr, age in await cursor.fetchall()]

            if receiving_players_json:
                receiving_players = json.loads(receiving_players_json)
                if receiving_players:
                    placeholders = ','.join('?' * len(receiving_players))
                    cursor = await db.execute(
                        f"SELECT name, position, overall_rating, age FROM players WHERE player_id IN ({placeholders})",
                        receiving_players
                    )
                    receiving_player_names = [f"**{name}** ({pos}, {age}, {ovr})" for name, pos, ovr, age in await cursor.fetchall()]

        # Send to approval channel
        channel = self.bot.get_channel(approval_channel_id)
        if channel:
            embed = discord.Embed(
                title="⚖️ Trade Pending Moderator Approval",
                color=discord.Color.orange()
            )

            embed.add_field(
                name=f"**{recv_emoji_str}receive:**",
                value="\n".join(initiating_player_names) if initiating_player_names else "*Nothing*",
                inline=True
            )

            embed.add_field(
                name=f"**{init_emoji_str}receive:**",
                value="\n".join(receiving_player_names) if receiving_player_names else "*Nothing*",
                inline=True
            )

            view = ModeratorApprovalView(self.trade_id, self.bot)
            await channel.send(embed=embed, view=view)


class ModeratorApprovalView(discord.ui.View):
    """View for moderator approval/veto"""
    def __init__(self, trade_id, bot):
        super().__init__(timeout=None)
        self.trade_id = trade_id
        self.bot = bot

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.green, custom_id="approve_trade")
    async def approve_trade(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Check if user is admin
        is_admin = False
        if interaction.guild.owner_id == interaction.user.id:
            is_admin = True
        elif ADMIN_ROLE_ID:
            member = interaction.guild.get_member(interaction.user.id)
            if member:
                admin_role_id = int(ADMIN_ROLE_ID) if isinstance(ADMIN_ROLE_ID, str) else ADMIN_ROLE_ID
                if any(role.id == admin_role_id for role in member.roles):
                    is_admin = True

        if not is_admin:
            await interaction.response.send_message("❌ Only moderators can approve trades!", ephemeral=True)
            return

        # Execute the trade
        await self.execute_trade(interaction)

    @discord.ui.button(label="Veto", style=discord.ButtonStyle.red, custom_id="veto_trade")
    async def veto_trade(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Check if user is admin
        is_admin = False
        if interaction.guild.owner_id == interaction.user.id:
            is_admin = True
        elif ADMIN_ROLE_ID:
            member = interaction.guild.get_member(interaction.user.id)
            if member:
                admin_role_id = int(ADMIN_ROLE_ID) if isinstance(ADMIN_ROLE_ID, str) else ADMIN_ROLE_ID
                if any(role.id == admin_role_id for role in member.roles):
                    is_admin = True

        if not is_admin:
            await interaction.response.send_message("❌ Only moderators can veto trades!", ephemeral=True)
            return

        # Update trade status
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """UPDATE trades SET status = 'vetoed', approved_by_user_id = ?
                   WHERE trade_id = ?""",
                (str(interaction.user.id), self.trade_id)
            )

            # Get team info and trade details
            cursor = await db.execute(
                """SELECT t1.channel_id, t1.team_name, t1.emoji_id, t2.channel_id, t2.team_name, t2.emoji_id,
                          tr.initiating_players, tr.receiving_players
                   FROM trades tr
                   JOIN teams t1 ON tr.initiating_team_id = t1.team_id
                   JOIN teams t2 ON tr.receiving_team_id = t2.team_id
                   WHERE tr.trade_id = ?""",
                (self.trade_id,)
            )
            result = await cursor.fetchone()

            if result:
                init_channel_id, init_team_name, init_emoji_id, recv_channel_id, recv_team_name, recv_emoji_id, init_players_json, recv_players_json = result

                # Get player details
                initiating_players = json.loads(init_players_json) if init_players_json else []
                receiving_players = json.loads(recv_players_json) if recv_players_json else []

                init_player_details = []
                recv_player_details = []

                if initiating_players:
                    placeholders = ','.join('?' * len(initiating_players))
                    cursor = await db.execute(
                        f"SELECT name, position, overall_rating, age FROM players WHERE player_id IN ({placeholders})",
                        initiating_players
                    )
                    init_player_details = [f"**{name}** ({pos}, {age}, {ovr})" for name, pos, ovr, age in await cursor.fetchall()]

                if receiving_players:
                    placeholders = ','.join('?' * len(receiving_players))
                    cursor = await db.execute(
                        f"SELECT name, position, overall_rating, age FROM players WHERE player_id IN ({placeholders})",
                        receiving_players
                    )
                    recv_player_details = [f"**{name}** ({pos}, {age}, {ovr})" for name, pos, ovr, age in await cursor.fetchall()]

            await db.commit()

        if result:
            # Get emojis
            init_emoji = self.bot.get_emoji(int(init_emoji_id)) if init_emoji_id else None
            recv_emoji = self.bot.get_emoji(int(recv_emoji_id)) if recv_emoji_id else None
            init_emoji_str = f"{init_emoji} " if init_emoji else ""
            recv_emoji_str = f"{recv_emoji} " if recv_emoji else ""

            # Notify initiating team
            if init_channel_id:
                channel = self.bot.get_channel(int(init_channel_id))
                if channel:
                    embed = discord.Embed(
                        title=f"Your trade with **{recv_team_name}** was vetoed by the league commission.",
                        color=discord.Color.red()
                    )

                    embed.add_field(
                        name=f"**{recv_emoji_str}receive:**",
                        value="\n".join(init_player_details) if init_player_details else "*Nothing*",
                        inline=True
                    )

                    embed.add_field(
                        name=f"**{init_emoji_str}receive:**",
                        value="\n".join(recv_player_details) if recv_player_details else "*Nothing*",
                        inline=True
                    )

                    await channel.send(embed=embed)

            # Notify receiving team
            if recv_channel_id:
                channel = self.bot.get_channel(int(recv_channel_id))
                if channel:
                    embed = discord.Embed(
                        title=f"Your trade with **{init_team_name}** was vetoed by the league commission.",
                        color=discord.Color.red()
                    )

                    embed.add_field(
                        name=f"**{recv_emoji_str}receive:**",
                        value="\n".join(init_player_details) if init_player_details else "*Nothing*",
                        inline=True
                    )

                    embed.add_field(
                        name=f"**{init_emoji_str}receive:**",
                        value="\n".join(recv_player_details) if recv_player_details else "*Nothing*",
                        inline=True
                    )

                    await channel.send(embed=embed)

        # Disable buttons
        for item in self.children:
            item.disabled = True

        await interaction.response.edit_message(view=self)
        await interaction.followup.send("✅ Trade vetoed and teams notified.", ephemeral=True)

    async def execute_trade(self, interaction: discord.Interaction):
        """Execute the approved trade"""
        async with aiosqlite.connect(DB_PATH) as db:
            # Get trade details
            cursor = await db.execute(
                """SELECT initiating_team_id, receiving_team_id, initiating_players, receiving_players
                   FROM trades WHERE trade_id = ?""",
                (self.trade_id,)
            )
            result = await cursor.fetchone()

            if not result:
                await interaction.response.send_message("❌ Trade not found!", ephemeral=True)
                return

            init_team_id, recv_team_id, init_players_json, recv_players_json = result

            # Parse player lists
            init_players = json.loads(init_players_json) if init_players_json else []
            recv_players = json.loads(recv_players_json) if recv_players_json else []

            # Validate that all players are on the correct teams before executing trade
            if init_players:
                placeholders = ','.join('?' * len(init_players))
                cursor = await db.execute(
                    f"SELECT player_id FROM players WHERE player_id IN ({placeholders}) AND team_id = ?",
                    init_players + [init_team_id]
                )
                valid_init_players = [row[0] for row in await cursor.fetchall()]
                if len(valid_init_players) != len(init_players):
                    await interaction.response.send_message(
                        "❌ Trade cannot be executed: Some players from the initiating team are no longer on that team!",
                        ephemeral=True
                    )
                    return

            if recv_players:
                placeholders = ','.join('?' * len(recv_players))
                cursor = await db.execute(
                    f"SELECT player_id FROM players WHERE player_id IN ({placeholders}) AND team_id = ?",
                    recv_players + [recv_team_id]
                )
                valid_recv_players = [row[0] for row in await cursor.fetchall()]
                if len(valid_recv_players) != len(recv_players):
                    await interaction.response.send_message(
                        "❌ Trade cannot be executed: Some players from the receiving team are no longer on that team!",
                        ephemeral=True
                    )
                    return

            # Transfer players
            if init_players:
                placeholders = ','.join('?' * len(init_players))
                await db.execute(
                    f"UPDATE players SET team_id = ? WHERE player_id IN ({placeholders})",
                    [recv_team_id] + init_players
                )

            if recv_players:
                placeholders = ','.join('?' * len(recv_players))
                await db.execute(
                    f"UPDATE players SET team_id = ? WHERE player_id IN ({placeholders})",
                    [init_team_id] + recv_players
                )

            # Update trade status
            await db.execute(
                """UPDATE trades SET status = 'approved', approved_at = CURRENT_TIMESTAMP,
                   approved_by_user_id = ? WHERE trade_id = ?""",
                (str(interaction.user.id), self.trade_id)
            )

            # Get team and player info for notifications
            cursor = await db.execute(
                """SELECT t1.team_name, t1.channel_id, t1.emoji_id, t2.team_name, t2.channel_id, t2.emoji_id
                   FROM teams t1
                   JOIN teams t2 ON t2.team_id = ?
                   WHERE t1.team_id = ?""",
                (recv_team_id, init_team_id)
            )
            team_info = await cursor.fetchone()

            # Get player names
            init_player_names = []
            recv_player_names = []

            if init_players:
                placeholders = ','.join('?' * len(init_players))
                cursor = await db.execute(
                    f"SELECT name, position, overall_rating, age FROM players WHERE player_id IN ({placeholders})",
                    init_players
                )
                init_player_names = [f"**{name}** ({pos}, {age}, {ovr})" for name, pos, ovr, age in await cursor.fetchall()]

            if recv_players:
                placeholders = ','.join('?' * len(recv_players))
                cursor = await db.execute(
                    f"SELECT name, position, overall_rating, age FROM players WHERE player_id IN ({placeholders})",
                    recv_players
                )
                recv_player_names = [f"**{name}** ({pos}, {age}, {ovr})" for name, pos, ovr, age in await cursor.fetchall()]

            # Get trade log channel
            cursor = await db.execute(
                "SELECT setting_value FROM settings WHERE setting_key = 'trade_log_channel_id'"
            )
            log_result = await cursor.fetchone()

            await db.commit()

        if not team_info:
            await interaction.response.send_message("❌ Team info not found!", ephemeral=True)
            return

        _, init_channel_id, init_emoji_id, _, recv_channel_id, recv_emoji_id = team_info

        # Get emojis
        init_emoji = self.bot.get_emoji(int(init_emoji_id)) if init_emoji_id else None
        recv_emoji = self.bot.get_emoji(int(recv_emoji_id)) if recv_emoji_id else None
        init_emoji_str = f"{init_emoji} " if init_emoji else ""
        recv_emoji_str = f"{recv_emoji} " if recv_emoji else ""

        # Create trade announcement embed
        embed = discord.Embed(
            title="Trade approved!",
            color=discord.Color.green()
        )

        embed.add_field(
            name=f"**{init_emoji_str}receive:**",
            value="\n".join(recv_player_names) if recv_player_names else "*Nothing*",
            inline=True
        )

        embed.add_field(
            name=f"**{recv_emoji_str}receive:**",
            value="\n".join(init_player_names) if init_player_names else "*Nothing*",
            inline=True
        )

        # Send to trade log channel
        if log_result and log_result[0]:
            log_channel = self.bot.get_channel(int(log_result[0]))
            if log_channel:
                await log_channel.send(embed=embed)

        # Send to both team channels
        for channel_id in [init_channel_id, recv_channel_id]:
            if channel_id:
                channel = self.bot.get_channel(int(channel_id))
                if channel:
                    await channel.send(embed=embed)

        # Disable buttons
        for item in self.children:
            item.disabled = True

        await interaction.response.edit_message(content="✅ Trade approved and executed!", view=self)


async def setup(bot):
    await bot.add_cog(TradeCommands(bot))
