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

    @app_commands.command(name="tradeoffer", description="Create a trade offer with another team")
    async def trade_offer(self, interaction: discord.Interaction):
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

        # Create trade menu
        view = TradeOfferView(team_id, team_name, interaction.user.id, self.bot, interaction.guild)
        await view.initialize()
        embed = view.create_embed()

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


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
        self.initiating_players = []  # List of player IDs
        self.receiving_players = []   # List of player IDs
        self.all_teams = []
        self.initiating_roster = []
        self.receiving_roster = []

    async def initialize(self):
        """Load initial data"""
        async with aiosqlite.connect(DB_PATH) as db:
            # Get all teams except initiating team
            cursor = await db.execute(
                "SELECT team_id, team_name FROM teams WHERE team_id != ? ORDER BY team_name",
                (self.initiating_team_id,)
            )
            self.all_teams = await cursor.fetchall()

            # Get initiating team roster
            cursor = await db.execute(
                "SELECT player_id, name, position, overall_rating FROM players WHERE team_id = ? ORDER BY name",
                (self.initiating_team_id,)
            )
            self.initiating_roster = await cursor.fetchall()

            # If receiving team is set, get their roster and name
            if self.receiving_team_id:
                cursor = await db.execute(
                    "SELECT team_name FROM teams WHERE team_id = ?",
                    (self.receiving_team_id,)
                )
                result = await cursor.fetchone()
                if result:
                    self.receiving_team_name = result[0]

                cursor = await db.execute(
                    "SELECT player_id, name, position, overall_rating FROM players WHERE team_id = ? ORDER BY name",
                    (self.receiving_team_id,)
                )
                self.receiving_roster = await cursor.fetchall()

        # Add components
        self.add_components()

    def add_components(self):
        """Add all UI components"""
        self.clear_items()

        # Team select (row 0)
        if not self.is_counter_offer and self.all_teams:
            team_select = TeamSelectMenu(self)
            self.add_item(team_select)
        elif self.is_counter_offer:
            # Show disabled button for counter-offers
            disabled_btn = discord.ui.Button(
                label=f"Trading with: {self.receiving_team_name or 'Unknown'}",
                style=discord.ButtonStyle.secondary,
                disabled=True,
                row=0
            )
            self.add_item(disabled_btn)

        # Player selection dropdowns (row 1-2)
        if self.initiating_roster:
            offering_select = OfferingPlayerSelect(self)
            self.add_item(offering_select)

        if self.receiving_team_id and self.receiving_roster:
            receiving_select = ReceivingPlayerSelect(self)
            self.add_item(receiving_select)

        # Action buttons (row 3)
        clear_btn = discord.ui.Button(label="Clear All", style=discord.ButtonStyle.secondary, row=3)
        clear_btn.callback = self.clear_callback
        self.add_item(clear_btn)

        send_btn = discord.ui.Button(label="Send Offer", style=discord.ButtonStyle.success, row=3)
        send_btn.callback = self.send_callback
        self.add_item(send_btn)

        cancel_btn = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.danger, row=3)
        cancel_btn.callback = self.cancel_callback
        self.add_item(cancel_btn)

    def create_embed(self):
        """Create the trade offer embed"""
        embed = discord.Embed(
            title="📋 Create Trade Offer" if not self.is_counter_offer else "📋 Create Counter Offer",
            color=discord.Color.blue()
        )

        # Trading with
        trading_with = self.receiving_team_name or "*Not selected - use dropdown above*"
        embed.add_field(name="Trading With", value=trading_with, inline=False)

        # Get player names
        offering_names = []
        receiving_names = []

        if self.initiating_players:
            for player_id in self.initiating_players:
                player = next((p for p in self.initiating_roster if p[0] == player_id), None)
                if player:
                    _, name, pos, ovr = player
                    offering_names.append(f"**{name}** ({pos}, {ovr} OVR)")

        if self.receiving_players:
            for player_id in self.receiving_players:
                player = next((p for p in self.receiving_roster if p[0] == player_id), None)
                if player:
                    _, name, pos, ovr = player
                    receiving_names.append(f"**{name}** ({pos}, {ovr} OVR)")

        # Show what each team sends
        embed.add_field(
            name=f"**{self.initiating_team_name}** sends:",
            value="\n".join(offering_names) if offering_names else "*No players selected*",
            inline=True
        )

        embed.add_field(
            name=f"**{trading_with}** sends:",
            value="\n".join(receiving_names) if receiving_names else "*No players selected*",
            inline=True
        )

        embed.set_footer(text="Use the dropdowns to select teams and players, then click Send Offer")

        return embed

    async def update_view(self, interaction: discord.Interaction):
        """Update the view after changes"""
        # Refresh rosters if team changed
        if self.receiving_team_id and not self.receiving_roster:
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "SELECT player_id, name, position, overall_rating FROM players WHERE team_id = ? ORDER BY name",
                    (self.receiving_team_id,)
                )
                self.receiving_roster = await cursor.fetchall()

        self.add_components()
        embed = self.create_embed()
        await interaction.response.edit_message(embed=embed, view=self)

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

        await interaction.response.defer()

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
                embed = discord.Embed(
                    title="📨 New Trade Offer!" if not self.is_counter_offer else "📨 Counter Offer!",
                    color=discord.Color.gold()
                )

                # Get player names for embed
                offering_names = []
                receiving_names = []

                if self.initiating_players:
                    for player_id in self.initiating_players:
                        player = next((p for p in self.initiating_roster if p[0] == player_id), None)
                        if player:
                            _, name, pos, ovr = player
                            offering_names.append(f"**{name}** ({pos}, {ovr} OVR)")

                if self.receiving_players:
                    for player_id in self.receiving_players:
                        player = next((p for p in self.receiving_roster if p[0] == player_id), None)
                        if player:
                            _, name, pos, ovr = player
                            receiving_names.append(f"**{name}** ({pos}, {ovr} OVR)")

                embed.add_field(
                    name=f"**{self.initiating_team_name}** sends:",
                    value="\n".join(offering_names) if offering_names else "*Nothing*",
                    inline=True
                )

                embed.add_field(
                    name=f"**{self.receiving_team_name}** sends:",
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

        await interaction.edit_original_response(content="✅ **Trade offer sent!**", embed=None, view=None)


class TeamSelectMenu(discord.ui.Select):
    """Select menu for choosing a team to trade with"""
    def __init__(self, parent_view):
        self.parent_view = parent_view
        options = [
            discord.SelectOption(label=team_name, value=str(team_id))
            for team_id, team_name in parent_view.all_teams[:25]  # Discord limit
        ]
        super().__init__(placeholder="Select a team to trade with...", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.receiving_team_id = int(self.values[0])
        self.parent_view.receiving_team_name = next(
            (name for tid, name in self.parent_view.all_teams if tid == self.parent_view.receiving_team_id),
            None
        )
        # Clear receiving players when changing teams
        self.parent_view.receiving_players = []
        self.parent_view.receiving_roster = []
        await self.parent_view.update_view(interaction)


class OfferingPlayerSelect(discord.ui.Select):
    """Select menu for choosing players to offer"""
    def __init__(self, parent_view):
        self.parent_view = parent_view
        options = [
            discord.SelectOption(
                label=f"{name} ({pos}, {ovr} OVR)",
                value=str(player_id),
                default=(player_id in parent_view.initiating_players)
            )
            for player_id, name, pos, ovr in parent_view.initiating_roster[:25]  # Discord limit
        ]
        super().__init__(
            placeholder="Select your players to trade away...",
            options=options,
            min_values=0,
            max_values=len(options),
            row=1
        )

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.initiating_players = [int(v) for v in self.values]
        await self.parent_view.update_view(interaction)


class ReceivingPlayerSelect(discord.ui.Select):
    """Select menu for choosing players to receive"""
    def __init__(self, parent_view):
        self.parent_view = parent_view
        options = [
            discord.SelectOption(
                label=f"{name} ({pos}, {ovr} OVR)",
                value=str(player_id),
                default=(player_id in parent_view.receiving_players)
            )
            for player_id, name, pos, ovr in parent_view.receiving_roster[:25]  # Discord limit
        ]
        super().__init__(
            placeholder="Select players you want to receive...",
            options=options,
            min_values=0,
            max_values=len(options),
            row=2
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

            # Get initiating team channel to notify them
            cursor = await db.execute(
                "SELECT channel_id, team_name FROM teams WHERE team_id = ?",
                (initiating_team_id,)
            )
            initiating_team = await cursor.fetchone()
            await db.commit()

        # Notify initiating team
        if initiating_team and initiating_team[0]:
            channel = self.bot.get_channel(int(initiating_team[0]))
            if channel:
                await channel.send(f"❌ Your trade offer to **{role_result[1]}** was declined.")

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
                          t1.team_name, t2.team_name
                   FROM trades t
                   JOIN teams t1 ON t.initiating_team_id = t1.team_id
                   JOIN teams t2 ON t.receiving_team_id = t2.team_id
                   WHERE t.trade_id = ?""",
                (self.trade_id,)
            )
            trade_result = await cursor.fetchone()

            if not trade_result:
                return

            _, _, initiating_players_json, receiving_players_json, initiating_team_name, receiving_team_name = trade_result

            # Get player names
            initiating_player_names = []
            receiving_player_names = []

            if initiating_players_json:
                initiating_players = json.loads(initiating_players_json)
                if initiating_players:
                    placeholders = ','.join('?' * len(initiating_players))
                    cursor = await db.execute(
                        f"SELECT name, position, overall_rating FROM players WHERE player_id IN ({placeholders})",
                        initiating_players
                    )
                    initiating_player_names = [f"**{name}** ({pos}, {ovr} OVR)" for name, pos, ovr in await cursor.fetchall()]

            if receiving_players_json:
                receiving_players = json.loads(receiving_players_json)
                if receiving_players:
                    placeholders = ','.join('?' * len(receiving_players))
                    cursor = await db.execute(
                        f"SELECT name, position, overall_rating FROM players WHERE player_id IN ({placeholders})",
                        receiving_players
                    )
                    receiving_player_names = [f"**{name}** ({pos}, {ovr} OVR)" for name, pos, ovr in await cursor.fetchall()]

        # Send to approval channel
        channel = self.bot.get_channel(approval_channel_id)
        if channel:
            embed = discord.Embed(
                title="⚖️ Trade Pending Moderator Approval",
                color=discord.Color.orange()
            )

            embed.add_field(
                name=f"**{initiating_team_name}** sends:",
                value="\n".join(initiating_player_names) if initiating_player_names else "*Nothing*",
                inline=True
            )

            embed.add_field(
                name=f"**{receiving_team_name}** sends:",
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

            # Get team channels
            cursor = await db.execute(
                """SELECT t1.channel_id, t2.channel_id, t1.team_name, t2.team_name
                   FROM trades tr
                   JOIN teams t1 ON tr.initiating_team_id = t1.team_id
                   JOIN teams t2 ON tr.receiving_team_id = t2.team_id
                   WHERE tr.trade_id = ?""",
                (self.trade_id,)
            )
            result = await cursor.fetchone()
            await db.commit()

        if result:
            init_channel_id, recv_channel_id, init_team_name, recv_team_name = result

            # Notify both teams
            for channel_id in [init_channel_id, recv_channel_id]:
                if channel_id:
                    channel = self.bot.get_channel(int(channel_id))
                    if channel:
                        await channel.send(
                            f"❌ **Trade Vetoed**\n\nYour trade with the other team has been vetoed by the moderators."
                        )

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
                """SELECT t1.team_name, t1.channel_id, t2.team_name, t2.channel_id
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
                    f"SELECT name, position, overall_rating FROM players WHERE player_id IN ({placeholders})",
                    init_players
                )
                init_player_names = [f"**{name}** ({pos}, {ovr} OVR)" for name, pos, ovr in await cursor.fetchall()]

            if recv_players:
                placeholders = ','.join('?' * len(recv_players))
                cursor = await db.execute(
                    f"SELECT name, position, overall_rating FROM players WHERE player_id IN ({placeholders})",
                    recv_players
                )
                recv_player_names = [f"**{name}** ({pos}, {ovr} OVR)" for name, pos, ovr in await cursor.fetchall()]

            # Get trade log channel
            cursor = await db.execute(
                "SELECT setting_value FROM settings WHERE setting_key = 'trade_log_channel_id'"
            )
            log_result = await cursor.fetchone()

            await db.commit()

        if not team_info:
            await interaction.response.send_message("❌ Team info not found!", ephemeral=True)
            return

        init_team_name, init_channel_id, recv_team_name, recv_channel_id = team_info

        # Create trade announcement embed
        embed = discord.Embed(
            title="✅ Trade Completed!",
            color=discord.Color.green()
        )

        embed.add_field(
            name=f"**{init_team_name}** receives:",
            value="\n".join(recv_player_names) if recv_player_names else "*Nothing*",
            inline=True
        )

        embed.add_field(
            name=f"**{recv_team_name}** receives:",
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
