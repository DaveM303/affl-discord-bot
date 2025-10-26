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

        # Open trade menu
        view = TradeOfferMenu(team_id, team_name, interaction.user.id, self.bot)
        await interaction.response.send_message(
            f"**Create Trade Offer** - {team_name}\n\nSelect a team to trade with:",
            view=view,
            ephemeral=True
        )


class TradeOfferMenu(discord.ui.View):
    """Main menu for creating a trade offer"""
    def __init__(self, initiating_team_id, initiating_team_name, user_id, bot, is_counter_offer=False, original_trade_id=None, receiving_team_id=None):
        super().__init__(timeout=600)
        self.initiating_team_id = initiating_team_id
        self.initiating_team_name = initiating_team_name
        self.user_id = user_id
        self.bot = bot
        self.is_counter_offer = is_counter_offer
        self.original_trade_id = original_trade_id
        self.receiving_team_id = receiving_team_id
        self.initiating_players = []  # List of player IDs
        self.receiving_players = []   # List of player IDs

        # Add team select button (disabled for counter-offers)
        if not is_counter_offer:
            self.add_item(TeamSelectButton(self))
        else:
            # For counter-offers, show the team but greyed out
            team_select = TeamSelectButton(self)
            team_select.disabled = True
            team_select.label = f"Trading with: (set)"
            self.add_item(team_select)

        # Add player selection buttons
        self.add_item(AddOfferingPlayerButton(self))
        self.add_item(AddReceivingPlayerButton(self))

        # Add send offer button
        self.add_item(SendOfferButton(self))

    async def update_message(self, interaction: discord.Interaction):
        """Update the trade offer message"""
        # Get player names
        initiating_player_names = []
        receiving_player_names = []

        async with aiosqlite.connect(DB_PATH) as db:
            if self.initiating_players:
                placeholders = ','.join('?' * len(self.initiating_players))
                cursor = await db.execute(
                    f"SELECT name, position, overall_rating FROM players WHERE player_id IN ({placeholders})",
                    self.initiating_players
                )
                initiating_player_names = [f"{name} ({pos}, {ovr} OVR)" for name, pos, ovr in await cursor.fetchall()]

            if self.receiving_players:
                placeholders = ','.join('?' * len(self.receiving_players))
                cursor = await db.execute(
                    f"SELECT name, position, overall_rating FROM players WHERE player_id IN ({placeholders})",
                    self.receiving_players
                )
                receiving_player_names = [f"{name} ({pos}, {ovr} OVR)" for name, pos, ovr in await cursor.fetchall()]

            # Get receiving team name
            receiving_team_name = "Not selected"
            if self.receiving_team_id:
                cursor = await db.execute(
                    "SELECT team_name FROM teams WHERE team_id = ?",
                    (self.receiving_team_id,)
                )
                result = await cursor.fetchone()
                if result:
                    receiving_team_name = result[0]

        embed = discord.Embed(
            title="📋 Trade Offer" if not self.is_counter_offer else "📋 Counter Offer",
            color=discord.Color.blue()
        )

        embed.add_field(
            name=f"Trading with:",
            value=receiving_team_name,
            inline=False
        )

        embed.add_field(
            name=f"**{self.initiating_team_name}** sends:",
            value="\n".join(initiating_player_names) if initiating_player_names else "*No players selected*",
            inline=True
        )

        embed.add_field(
            name=f"**{receiving_team_name}** sends:",
            value="\n".join(receiving_player_names) if receiving_player_names else "*No players selected*",
            inline=True
        )

        embed.set_footer(text="Use the buttons below to build your trade offer")

        await interaction.response.edit_message(embed=embed, view=self)


class TeamSelectButton(discord.ui.Button):
    def __init__(self, parent_view):
        super().__init__(label="Select Team", style=discord.ButtonStyle.primary, row=0)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        # Get all teams except the initiating team
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT team_id, team_name FROM teams WHERE team_id != ? ORDER BY team_name",
                (self.parent_view.initiating_team_id,)
            )
            teams = await cursor.fetchall()

        if not teams:
            await interaction.response.send_message("❌ No other teams found!", ephemeral=True)
            return

        # Create select menu for teams
        select = TeamSelectDropdown(teams, self.parent_view)
        view = discord.ui.View(timeout=60)
        view.add_item(select)

        await interaction.response.send_message(
            "Select a team to trade with:",
            view=view,
            ephemeral=True
        )


class TeamSelectDropdown(discord.ui.Select):
    def __init__(self, teams, parent_view):
        self.parent_view = parent_view
        options = [
            discord.SelectOption(label=team_name, value=str(team_id))
            for team_id, team_name in teams[:25]  # Discord limit
        ]
        super().__init__(placeholder="Choose a team...", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.receiving_team_id = int(self.values[0])
        await interaction.response.send_message("✅ Team selected!", ephemeral=True)


class AddOfferingPlayerButton(discord.ui.Button):
    def __init__(self, parent_view):
        super().__init__(label="Add Your Players", style=discord.ButtonStyle.green, row=1)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        # Get players from initiating team
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT player_id, name, position, overall_rating FROM players WHERE team_id = ? ORDER BY name",
                (self.parent_view.initiating_team_id,)
            )
            players = await cursor.fetchall()

        if not players:
            await interaction.response.send_message("❌ Your team has no players!", ephemeral=True)
            return

        # Create select menu
        select = PlayerSelectDropdown(players, self.parent_view, is_offering=True)
        view = discord.ui.View(timeout=60)
        view.add_item(select)

        await interaction.response.send_message(
            "Select players to trade away (you can select multiple):",
            view=view,
            ephemeral=True
        )


class AddReceivingPlayerButton(discord.ui.Button):
    def __init__(self, parent_view):
        super().__init__(label="Add Their Players", style=discord.ButtonStyle.green, row=1)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        if not self.parent_view.receiving_team_id:
            await interaction.response.send_message("❌ Please select a team first!", ephemeral=True)
            return

        # Get players from receiving team
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT player_id, name, position, overall_rating FROM players WHERE team_id = ? ORDER BY name",
                (self.parent_view.receiving_team_id,)
            )
            players = await cursor.fetchall()

        if not players:
            await interaction.response.send_message("❌ That team has no players!", ephemeral=True)
            return

        # Create select menu
        select = PlayerSelectDropdown(players, self.parent_view, is_offering=False)
        view = discord.ui.View(timeout=60)
        view.add_item(select)

        await interaction.response.send_message(
            "Select players you want to receive (you can select multiple):",
            view=view,
            ephemeral=True
        )


class PlayerSelectDropdown(discord.ui.Select):
    def __init__(self, players, parent_view, is_offering):
        self.parent_view = parent_view
        self.is_offering = is_offering
        options = [
            discord.SelectOption(
                label=f"{name} ({pos}, {ovr} OVR)",
                value=str(player_id)
            )
            for player_id, name, pos, ovr in players[:25]  # Discord limit
        ]
        super().__init__(placeholder="Choose players...", options=options, max_values=min(len(options), 25), row=0)

    async def callback(self, interaction: discord.Interaction):
        selected_ids = [int(v) for v in self.values]

        if self.is_offering:
            self.parent_view.initiating_players = selected_ids
        else:
            self.parent_view.receiving_players = selected_ids

        await interaction.response.send_message(
            f"✅ Selected {len(selected_ids)} player(s)!",
            ephemeral=True
        )


class SendOfferButton(discord.ui.Button):
    def __init__(self, parent_view):
        super().__init__(label="Send Offer", style=discord.ButtonStyle.primary, row=2)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        # Validate trade
        if not self.parent_view.receiving_team_id:
            await interaction.response.send_message("❌ Please select a team to trade with!", ephemeral=True)
            return

        if not self.parent_view.initiating_players and not self.parent_view.receiving_players:
            await interaction.response.send_message("❌ Please add at least one player to the trade!", ephemeral=True)
            return

        # Store trade in database
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                """INSERT INTO trades (initiating_team_id, receiving_team_id, initiating_players,
                                      receiving_players, status, created_by_user_id, original_trade_id)
                   VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
                (
                    self.parent_view.initiating_team_id,
                    self.parent_view.receiving_team_id,
                    json.dumps(self.parent_view.initiating_players),
                    json.dumps(self.parent_view.receiving_players),
                    str(self.parent_view.user_id),
                    self.parent_view.original_trade_id
                )
            )
            trade_id = cursor.lastrowid

            # Get team info
            cursor = await db.execute(
                """SELECT t1.team_name, t1.channel_id, t1.role_id, t2.team_name
                   FROM teams t1
                   JOIN teams t2 ON t2.team_id = ?
                   WHERE t1.team_id = ?""",
                (self.parent_view.initiating_team_id, self.parent_view.receiving_team_id)
            )
            result = await cursor.fetchone()
            initiating_team_name, receiving_channel_id, receiving_role_id, receiving_team_name = result

            # Get player names
            initiating_player_names = []
            receiving_player_names = []

            if self.parent_view.initiating_players:
                placeholders = ','.join('?' * len(self.parent_view.initiating_players))
                cursor = await db.execute(
                    f"SELECT name, position, overall_rating FROM players WHERE player_id IN ({placeholders})",
                    self.parent_view.initiating_players
                )
                initiating_player_names = [f"**{name}** ({pos}, {ovr} OVR)" for name, pos, ovr in await cursor.fetchall()]

            if self.parent_view.receiving_players:
                placeholders = ','.join('?' * len(self.parent_view.receiving_players))
                cursor = await db.execute(
                    f"SELECT name, position, overall_rating FROM players WHERE player_id IN ({placeholders})",
                    self.parent_view.receiving_players
                )
                receiving_player_names = [f"**{name}** ({pos}, {ovr} OVR)" for name, pos, ovr in await cursor.fetchall()]

            await db.commit()

        # Send to receiving team channel
        if receiving_channel_id:
            channel = self.parent_view.bot.get_channel(int(receiving_channel_id))
            if channel:
                embed = discord.Embed(
                    title="📨 New Trade Offer!" if not self.parent_view.is_counter_offer else "📨 Counter Offer!",
                    color=discord.Color.gold(),
                    description=f"**{initiating_team_name}** has sent you a trade offer!"
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

                # Add response buttons
                view = TradeResponseView(trade_id, self.parent_view.bot)

                # Ping the team role
                role_mention = f"<@&{receiving_role_id}>" if receiving_role_id else ""
                await channel.send(role_mention, embed=embed, view=view)

        # Cancel original trade if this is a counter-offer
        if self.parent_view.is_counter_offer and self.parent_view.original_trade_id:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE trades SET status = 'countered' WHERE trade_id = ?",
                    (self.parent_view.original_trade_id,)
                )
                await db.commit()

        await interaction.response.edit_message(
            content="✅ **Trade offer sent!**",
            embed=None,
            view=None
        )


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

            receiving_team_id, initiating_team_id, status, _, _ = result

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
        view = TradeOfferMenu(
            receiving_team_id,
            role_result[1],
            interaction.user.id,
            self.bot,
            is_counter_offer=True,
            original_trade_id=self.trade_id,
            receiving_team_id=initiating_team_id
        )

        # Swap the players (what they were offering is now what we're asking for)
        view.initiating_players = receiving_players
        view.receiving_players = initiating_players

        await interaction.response.send_message(
            f"**Counter Offer** - {role_result[1]}\n\nEdit the trade offer below:",
            view=view,
            ephemeral=True
        )

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
