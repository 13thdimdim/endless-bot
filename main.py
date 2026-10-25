from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
from threading import Thread

import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask
from waitress import serve

# ==================== KEEP ALIVE SERVER ====================
app = Flask('')

@app.route('/', methods=['GET', 'HEAD'])
def home():
    return "NETHRIC Bot is Online and Alive!", 200

def run_server():
    serve(app, host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run_server)
    t.daemon = True
    t.start()

LOGGER = logging.getLogger("nethric_bot")
HEX_COLOR_PATTERN = re.compile(r"^#?[0-9a-fA-F]{6}$")

def int_env(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    val = int(raw)
    return max(val, minimum)

@dataclass(frozen=True)
class BotSettings:
    token: str
    database_path: Path
    booster_initial_credits: int
    role_creation_cost: int
    guild_id: Optional[int]

    @classmethod
    def from_environment(cls) -> "BotSettings":
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token:
            raise RuntimeError("DISCORD_TOKEN is missing.")
        raw_guild = os.getenv("GUILD_ID", "").strip()
        guild_id = int(raw_guild) if raw_guild else None
        return cls(
            token=token,
            database_path=Path(os.getenv("DATABASE_PATH", "data/credits.sqlite3")),
            booster_initial_credits=int_env("BOOSTER_INITIAL_CREDITS", 1, minimum=1),
            role_creation_cost=int_env("ROLE_CREATION_COST", 1, minimum=1),
            guild_id=guild_id,
        )

# ==================== DATABASE STORES ====================

class CreditLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS guild_user_credits (
                    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                    credits INTEGER NOT NULL CHECK (credits >= 0),
                    PRIMARY KEY (guild_id, user_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS booster_credit_claims (
                    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, user_id)
                )
            """)

    def _ensure_user(self, conn: sqlite3.Connection, guild_id: int, user_id: int) -> None:
        conn.execute(
            "INSERT INTO guild_user_credits (guild_id, user_id, credits) VALUES (?, ?, 0) ON CONFLICT(guild_id, user_id) DO NOTHING",
            (guild_id, user_id)
        )

    def balance(self, guild_id: int, user_id: int) -> int:
        with self._connect() as conn:
            self._ensure_user(conn, guild_id, user_id)
            row = conn.execute("SELECT credits FROM guild_user_credits WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)).fetchone()
            return int(row["credits"])

    def add(self, guild_id: int, user_id: int, amount: int) -> int:
        with self._connect() as conn:
            self._ensure_user(conn, guild_id, user_id)
            conn.execute("UPDATE guild_user_credits SET credits = credits + ? WHERE guild_id = ? AND user_id = ?", (amount, guild_id, user_id))
            return self.balance(guild_id, user_id)

    def spend(self, guild_id: int, user_id: int, amount: int) -> Optional[int]:
        with self._connect() as conn:
            self._ensure_user(conn, guild_id, user_id)
            cursor = conn.execute("UPDATE guild_user_credits SET credits = credits - ? WHERE guild_id = ? AND user_id = ? AND credits >= ?", (amount, guild_id, user_id, amount))
            if cursor.rowcount != 1:
                return None
            return self.balance(guild_id, user_id)

    def has_claimed_booster(self, guild_id: int, user_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM booster_credit_claims WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)).fetchone()
            return row is not None

    def mark_booster_claimed(self, guild_id: int, user_id: int) -> None:
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO booster_credit_claims (guild_id, user_id) VALUES (?, ?)", (guild_id, user_id))

@dataclass(frozen=True)
class ReactionRoleItem:
    emoji: str
    role_id: int
    role_name: str

class ReactionRoleStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reaction_role_configs (
                    config_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL DEFAULT 0, name TEXT NOT NULL,
                    content TEXT NOT NULL, image_url TEXT, created_by INTEGER NOT NULL
                )
            """)
            try:
                conn.execute("ALTER TABLE reaction_role_configs ADD COLUMN image_url TEXT")
            except sqlite3.OperationalError:
                pass

            conn.execute("""
                CREATE TABLE IF NOT EXISTS reaction_role_items (
                    config_id INTEGER NOT NULL, emoji TEXT NOT NULL,
                    role_id INTEGER NOT NULL, role_name TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (config_id, emoji)
                )
            """)

    def create_draft(self, *, guild_id: int, channel_id: int, name: str, roles: list[ReactionRoleItem], content: str, image_url: Optional[str], created_by: int) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO reaction_role_configs (guild_id, channel_id, name, content, image_url, created_by) VALUES (?, ?, ?, ?, ?, ?)",
                (guild_id, channel_id, name, content, image_url, created_by)
            )
            config_id = int(cursor.lastrowid)
            conn.executemany(
                "INSERT INTO reaction_role_items (config_id, emoji, role_id, role_name, position) VALUES (?, ?, ?, ?, ?)",
                [(config_id, r.emoji, r.role_id, r.role_name, idx) for idx, r in enumerate(roles)]
            )
            return config_id

    def publish(self, config_id: int, message_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE reaction_role_configs SET message_id = ? WHERE config_id = ?", (message_id, config_id))

    def get_by_message(self, message_id: int):
        with self._connect() as conn:
            return conn.execute("SELECT * FROM reaction_role_configs WHERE message_id = ?", (message_id,)).fetchone()

    def get_role_by_emoji(self, message_id: int, emoji: str):
        with self._connect() as conn:
            cfg = self.get_by_message(message_id)
            if not cfg:
                return None
            row = conn.execute("SELECT role_id FROM reaction_role_items WHERE config_id = ? AND emoji = ?", (cfg["config_id"], emoji)).fetchone()
            return row["role_id"] if row else None

    def published(self):
        with self._connect() as conn:
            return conn.execute("SELECT * FROM reaction_role_configs WHERE message_id > 0").fetchall()

    def items(self, config_id: int) -> list[ReactionRoleItem]:
        with self._connect() as conn:
            rows = conn.execute("SELECT emoji, role_id, role_name FROM reaction_role_items WHERE config_id = ? ORDER BY position", (config_id,)).fetchall()
            return [ReactionRoleItem(r["emoji"], r["role_id"], r["role_name"]) for r in rows]

class TicketStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ticket_panels (
                    panel_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL DEFAULT 0, title TEXT NOT NULL,
                    description TEXT NOT NULL, choices TEXT NOT NULL,
                    log_open_id INTEGER NOT NULL, log_closed_id INTEGER NOT NULL, created_by INTEGER NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS active_tickets (
                    channel_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL, ticket_name TEXT NOT NULL,
                    ticket_type TEXT NOT NULL, opened_at TEXT NOT NULL,
                    log_open_id INTEGER NOT NULL, log_closed_id INTEGER NOT NULL, claimed_by TEXT DEFAULT 'None'
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS welcome_settings (
                    guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, message_text TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS booster_settings (
                    guild_id INTEGER PRIMARY KEY,
                    channel_id INTEGER NOT NULL,
                    role_male_id INTEGER,
                    role_female_id INTEGER,
                    msg_male TEXT NOT NULL,
                    msg_female TEXT NOT NULL,
                    msg_default TEXT NOT NULL
                )
            """)
            try:
                conn.execute("ALTER TABLE ticket_panels ADD COLUMN choices TEXT DEFAULT 'Create Ticket'")
            except sqlite3.OperationalError:
                pass

    def create_panel(self, guild_id: int, channel_id: int, title: str, description: str, choices: str, log_open_id: int, log_closed_id: int, created_by: int) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO ticket_panels (guild_id, channel_id, title, description, choices, log_open_id, log_closed_id, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (guild_id, channel_id, title, description, choices, log_open_id, log_closed_id, created_by)
            )
            return int(cursor.lastrowid)

    def publish(self, panel_id: int, message_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE ticket_panels SET message_id = ? WHERE panel_id = ?", (message_id, panel_id))

    def add_active_ticket(self, channel_id: int, guild_id: int, user_id: int, ticket_name: str, ticket_type: str, opened_at: str, log_open_id: int, log_closed_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO active_tickets VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'None')",
                (channel_id, guild_id, user_id, ticket_name, ticket_type, opened_at, log_open_id, log_closed_id)
            )

    def get_active_ticket(self, channel_id: int):
        with self._connect() as conn:
            return conn.execute("SELECT * FROM active_tickets WHERE channel_id = ?", (channel_id,)).fetchone()

    def update_claimed_by(self, channel_id: int, claimed_by: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE active_tickets SET claimed_by = ? WHERE channel_id = ?", (claimed_by, channel_id))

    def remove_active_ticket(self, channel_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM active_tickets WHERE channel_id = ?", (channel_id,))

    def set_welcome(self, guild_id: int, channel_id: int, message_text: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO welcome_settings VALUES (?, ?, ?) ON CONFLICT(guild_id) DO UPDATE SET channel_id = excluded.channel_id, message_text = excluded.message_text",
                (guild_id, channel_id, message_text)
            )

    def get_welcome(self, guild_id: int):
        with self._connect() as conn:
            return conn.execute("SELECT * FROM welcome_settings WHERE guild_id = ?", (guild_id,)).fetchone()

    def set_booster_log(self, guild_id: int, channel_id: int, role_male_id: Optional[int], role_female_id: Optional[int], msg_male: str, msg_female: str, msg_default: str) -> None:
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO booster_settings (guild_id, channel_id, role_male_id, role_female_id, msg_male, msg_female, msg_default)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    role_male_id = excluded.role_male_id,
                    role_female_id = excluded.role_female_id,
                    msg_male = excluded.msg_male,
                    msg_female = excluded.msg_female,
                    msg_default = excluded.msg_default
            """, (guild_id, channel_id, role_male_id, role_female_id, msg_male, msg_female, msg_default))

    def get_booster_log(self, guild_id: int):
        with self._connect() as conn:
            return conn.execute("SELECT * FROM booster_settings WHERE guild_id = ?", (guild_id,)).fetchone()

# ==================== TICKET VIEWS & MODALS ====================

class TicketCloseModal(discord.ui.Modal, title="Close Ticket"):
    reason = discord.ui.TextInput(
        label="Reason",
        placeholder="The reason for closing the ticket.",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=500
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            return

        bot: RoleCreditBot = interaction.client  # type: ignore
        ticket = bot.ticket_store.get_active_ticket(interaction.channel.id)

        await interaction.response.send_message("🔒 Closing ticket...", ephemeral=False)
        await asyncio.sleep(2)

        close_date = datetime.now().strftime("%B %d, %Y %I:%M %p")

        if ticket:
            log_ch = interaction.guild.get_channel(int(ticket["log_closed_id"]))
            if isinstance(log_ch, discord.TextChannel):
                # Embed Log Penutupan Sesuai Layout Terbaru
                embed = discord.Embed(title="Ticket Closed", color=discord.Color.gold())
                embed.add_field(name="Ticket Name", value=ticket["ticket_name"], inline=True)
                embed.add_field(name="Ticket Author", value=f"<@{ticket['user_id']}>", inline=True)
                embed.add_field(name="Closed By", value=interaction.user.mention, inline=True)
                embed.add_field(name="Open Date", value=ticket["opened_at"], inline=True)
                embed.add_field(name="Close Date", value=close_date, inline=True)
                embed.add_field(name="Ticket Close Reason", value=str(self.reason), inline=False)
                await log_ch.send(embed=embed)
            
            bot.ticket_store.remove_active_ticket(interaction.channel.id)

        try:
            await interaction.channel.delete()
        except Exception:
            pass

class TicketControlView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="Close Ticket", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="ticket:close"))
        self.add_item(discord.ui.Button(label="Claim Ticket", style=discord.ButtonStyle.secondary, emoji="📑", custom_id="ticket:claim"))

class TicketDropdown(discord.ui.Select):
    def __init__(self, choices_list: list[str]) -> None:
        options = []
        for choice in choices_list:
            clean = choice.strip()
            if not clean:
                continue
            parts = clean.split(" ", 1)
            emoji, label = (parts[0], parts[1]) if len(parts) == 2 and not parts[0].isalnum() else ("🎟️", clean)
            options.append(discord.SelectOption(label=label[:100], value=clean[:100], emoji=emoji))
        if not options:
            options.append(discord.SelectOption(label="Create Ticket", value="Create Ticket", emoji="🎟️"))
        super().__init__(placeholder="PILIH TICKET DISINI", min_values=1, max_values=1, options=options[:25], custom_id="ticket:select")

    async def callback(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        
        bot: RoleCreditBot = interaction.client  # type: ignore
        with bot.ticket_store._connect() as conn:
            row = conn.execute("SELECT * FROM ticket_panels WHERE message_id = ?", (interaction.message.id,)).fetchone()
        if not row:
            await interaction.response.send_message("❌ Panel tiket tidak ditemukan.", ephemeral=True)
            return

        type_display = self.values[0]
        selected_emoji = "🎟️"
        for opt in self.options:
            if opt.value == type_display:
                selected_emoji = opt.emoji.name if opt.emoji else "🎟️"
                break

        guild = interaction.guild
        member = interaction.user
        created_at = datetime.now().strftime("%B %d, %Y %I:%M %p")

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)
        }

        random_id = abs(hash(str(member.id) + str(datetime.now()))) % 9000 + 1000
        ticket_name = f"ticket-{random_id}"

        await interaction.response.defer(ephemeral=True)

        try:
            category = interaction.channel.category if isinstance(interaction.channel, discord.TextChannel) else None
            channel = await guild.create_text_channel(
                name=ticket_name,
                category=category,
                overwrites=overwrites,
                topic=f"Opened {created_at} by @{member.name}"
            )

            bot.ticket_store.add_active_ticket(channel.id, guild.id, member.id, ticket_name, type_display, created_at, row["log_open_id"], row["log_closed_id"])

            # Kirim Log Ke Channel Log Opened (Format Baru)
            log_ch = guild.get_channel(int(row["log_open_id"]))
            if isinstance(log_ch, discord.TextChannel):
                embed = discord.Embed(title="Ticket Opened", color=discord.Color.gold())
                embed.add_field(name="Ticket Name", value=ticket_name, inline=True)
                embed.add_field(name="Created By", value=member.mention, inline=True)
                embed.add_field(name="Opened Date", value=created_at, inline=True)
                embed.add_field(name="Ticket Type", value=f"{selected_emoji} {type_display}", inline=False)
                await log_ch.send(embed=embed)

            # Kirim Pesan Pembuka Di Channel Tiket
            msg_embed = discord.Embed(
                title="Ticket Opened",
                description=f"{member.mention} has created a new {selected_emoji} **{type_display}** ticket.\n📌 {bot.user.name} | `/close`",
                color=discord.Color.gold()
            )
            await channel.send(content=member.mention, embed=msg_embed, view=TicketControlView())
            await interaction.followup.send(f"✅ Tiket kamu berhasil dibuat di {channel.mention}!", ephemeral=True)
        except Exception as e:
            LOGGER.error(f"Error creating ticket: {e}")
            await interaction.followup.send("Gagal membuat tiket. Pastikan bot memiliki izin Manage Channels.", ephemeral=True)

class TicketPanelView(discord.ui.View):
    def __init__(self, choices: list[str]) -> None:
        super().__init__(timeout=None)
        self.add_item(TicketDropdown(choices))

async def handle_persistent_buttons(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
        return

    bot: RoleCreditBot = interaction.client  # type: ignore
    custom_id = interaction.data.get("custom_id")

    if custom_id == "ticket:close":
        await interaction.response.send_modal(TicketCloseModal())

    elif custom_id == "ticket:claim":
        if not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message("❌ Tombol ini hanya dapat digunakan oleh Admin/Staff!", ephemeral=True)
            return

        ticket = bot.ticket_store.get_active_ticket(interaction.channel.id)
        if not ticket:
            return

        if ticket["claimed_by"] != "None":
            await interaction.response.send_message("Tiket ini sudah diklaim sebelumnya.", ephemeral=True)
            return

        current_time = datetime.now().strftime("%B %d, %Y %I:%M %p")
        bot.ticket_store.update_claimed_by(interaction.channel.id, interaction.user.mention)

        # Disable tombol claim agar tidak diklik dua kali
        message = interaction.message
        if message and message.components:
            new_view = discord.ui.View.from_message(message)
            for child in new_view.children:
                if child.custom_id == "ticket:claim":
                    child.disabled = True
            await message.edit(view=new_view)

        # Update topik channel
        try:
            old_topic = interaction.channel.topic or ""
            new_topic = f"{old_topic} • Claimed by @{interaction.user.name} at {current_time}"
            await interaction.channel.edit(topic=new_topic)
        except Exception:
            pass

        embed = discord.Embed(
            title="Ticket Claimed",
            description=f"This ticket has been claimed by {interaction.user.mention}.",
            color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed)

# ==================== MODALS & VIEWS LAINNYA ====================

class RoleCreationModal(discord.ui.Modal, title="Create a custom role"):
    role_name = discord.ui.TextInput(label="Role name", max_length=50, required=True)
    color_hex = discord.ui.TextInput(label="Color hex", min_length=6, max_length=7, required=True)

    def __init__(self, bot: "RoleCreditBot") -> None:
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        member = interaction.user
        is_admin = member.guild_permissions.administrator
        is_booster = member.premium_since is not None
        if not is_admin and not is_booster:
            await interaction.response.send_message("Khusus Server Booster / Administrator.", ephemeral=True)
            return

        hex_val = str(self.color_hex).strip().removeprefix("#")
        color = discord.Color(int(hex_val, 16))

        await interaction.response.defer(ephemeral=True)
        if not is_admin:
            remaining = self.bot.ledger.spend(interaction.guild.id, member.id, self.bot.settings.role_creation_cost)
            if remaining is None:
                await interaction.followup.send("Credit tidak cukup!", ephemeral=True)
                return

        role = await interaction.guild.create_role(name=str(self.role_name).strip(), colour=color)
        await member.add_roles(role)
        await interaction.followup.send(f"Berhasil membuat role **{role.name}**!", ephemeral=True)

class RoleSetupView(discord.ui.View):
    def __init__(self, bot: "RoleCreditBot") -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Create a role", style=discord.ButtonStyle.primary, custom_id="role-credit:create")
    async def create_role(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(RoleCreationModal(self.bot))

    @discord.ui.button(label="Check credits", style=discord.ButtonStyle.secondary, custom_id="role-credit:balance")
    async def check_credits(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        balance = self.bot.ledger.balance(interaction.guild.id, interaction.user.id)
        await interaction.response.send_message(f"Sisa credit kamu: **{balance}**", ephemeral=True)

# ==================== BOT CORE ====================

class RoleCreditBot(commands.Bot):
    def __init__(self, settings: BotSettings) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.guilds = True
        intents.reactions = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, status=discord.Status.dnd)
        self.settings = settings
        self.ledger = CreditLedger(settings.database_path)
        self.reaction_roles = ReactionRoleStore(settings.database_path)
        self.ticket_store = TicketStore(settings.database_path)

    async def setup_hook(self) -> None:
        self.add_view(RoleSetupView(self))
        self.add_view(TicketControlView())
        self.add_listener(handle_persistent_buttons, "on_interaction")

        with self.ticket_store._connect() as conn:
            for p in conn.execute("SELECT choices FROM ticket_panels WHERE message_id > 0").fetchall():
                choices_list = [c.strip() for c in p["choices"].split(",") if c.strip()]
                self.add_view(TicketPanelView(choices_list))

        if self.settings.guild_id:
            guild_obj = discord.Object(id=self.settings.guild_id)
            self.tree.copy_global_to(guild=guild_obj)
            await self.tree.sync(guild=guild_obj)
        else:
            await self.tree.sync()

    async def on_ready(self) -> None:
        LOGGER.info("Logged in as %s", self.user)

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.user_id == self.user.id or not payload.guild_id:
            return
        emoji_str = str(payload.emoji)
        role_id = self.reaction_roles.get_role_by_emoji(payload.message_id, emoji_str)
        if role_id:
            guild = self.get_guild(payload.guild_id)
            if guild:
                member = guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
                role = guild.get_role(role_id)
                if member and role:
                    try:
                        await member.add_roles(role)
                    except Exception:
                        pass

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.user_id == self.user.id or not payload.guild_id:
            return
        emoji_str = str(payload.emoji)
        role_id = self.reaction_roles.get_role_by_emoji(payload.message_id, emoji_str)
        if role_id:
            guild = self.get_guild(payload.guild_id)
            if guild:
                member = guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
                role = guild.get_role(role_id)
                if member and role:
                    try:
                        await member.remove_roles(role)
                    except Exception:
                        pass

    async def on_member_join(self, member: discord.Member) -> None:
        row = self.ticket_store.get_welcome(member.guild.id)
        if not row:
            return
        ch = member.guild.get_channel(int(row["channel_id"]))
        if isinstance(ch, discord.TextChannel):
            embed = discord.Embed(description=str(row["message_text"]).replace("{user}", member.mention), colour=discord.Colour.orange())
            embed.set_thumbnail(url=member.display_avatar.url)
            try:
                await ch.send(embed=embed)
            except Exception:
                pass

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.premium_since is None and after.premium_since is not None:
            guild_id = after.guild.id
            user_id = after.id

            if not self.ledger.has_claimed_booster(guild_id, user_id):
                self.ledger.add(guild_id, user_id, self.settings.booster_initial_credits)
                self.ledger.mark_booster_claimed(guild_id, user_id)

                row = self.ticket_store.get_booster_log(guild_id)
                if row:
                    ch = after.guild.get_channel(int(row["channel_id"]))
                    if isinstance(ch, discord.TextChannel):
                        role_male_id = row["role_male_id"]
                        role_female_id = row["role_female_id"]

                        has_male_role = role_male_id and any(r.id == role_male_id for r in after.roles)
                        has_female_role = role_female_id and any(r.id == role_female_id for r in after.roles)

                        if has_male_role and row["msg_male"]:
                            template = row["msg_male"]
                        elif has_female_role and row["msg_female"]:
                            template = row["msg_female"]
                        else:
                            template = row["msg_default"] or "Terima kasih {user} sudah boost server! Dapat {credits} credit."

                        msg_content = template.replace("{user}", after.mention).replace("{credits}", str(self.settings.booster_initial_credits))
                        embed = discord.Embed(description=msg_content, colour=discord.Colour.magenta())
                        embed.set_thumbnail(url=after.display_avatar.url)
                        try:
                            await ch.send(embed=embed)
                        except Exception:
                            pass

def main() -> None:
    keep_alive()
    logging.basicConfig(level=logging.INFO)
    settings = BotSettings.from_environment()
    bot = RoleCreditBot(settings)

    @bot.tree.command(name="setup-panel", description="Setup Custom Role panel")
    @app_commands.default_permissions(administrator=True)
    async def setup_panel(interaction: discord.Interaction) -> None:
        embed = discord.Embed(title="Custom Role Creator", description="Klik tombol di bawah untuk membuat role.", colour=discord.Colour.dark_embed())
        await interaction.response.send_message(embed=embed, view=RoleSetupView(bot), ephemeral=False)

    @bot.tree.command(name="grant-credits", description="Berikan credit")
    @app_commands.default_permissions(administrator=True)
    async def grant_credits(interaction: discord.Interaction, member: discord.Member, amount: int) -> None:
        new_bal = bot.ledger.add(interaction.guild.id, member.id, amount)
        await interaction.response.send_message(f"✅ Berhasil menambah {amount} credit ke {member.mention}. Saldo: {new_bal}", ephemeral=True)

    @bot.tree.command(name="ticket-panel", description="Buat panel tiket bantuan")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(title="Judul panel", description="Deskripsi panel", choices="Pilihan tiket dipisah koma", log_open="Channel log dibuka", log_closed="Channel log ditutup")
    async def ticket_panel(interaction: discord.Interaction, title: str, description: str, choices: str, log_open: discord.TextChannel, log_closed: discord.TextChannel) -> None:
        choices_list = [c.strip() for c in choices.split(",") if c.strip()]
        if not choices_list:
            choices_list = ["🎟️ Create Ticket"]

        panel_id = bot.ticket_store.create_panel(interaction.guild.id, interaction.channel.id, title, description, ",".join(choices_list), log_open.id, log_closed.id, interaction.user.id)

        embed = discord.Embed(title=title, description=description, color=discord.Color.dark_embed())
        view = TicketPanelView(choices_list)
        msg = await interaction.channel.send(embed=embed, view=view)

        bot.ticket_store.publish(panel_id, msg.id)
        bot.add_view(view, message_id=msg.id)
        await interaction.response.send_message("✅ Panel tiket berhasil dibuat!", ephemeral=True)

    @bot.tree.command(name="set-welcome", description="Atur pesan sambutan member baru")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(channel="Channel kirim pesan", message="Pesan sambutan ({user})")
    async def set_welcome(interaction: discord.Interaction, channel: discord.TextChannel, message: str) -> None:
        bot.ticket_store.set_welcome(interaction.guild.id, channel.id, message)
        await interaction.response.send_message(f"✅ Pesan sambutan diatur ke {channel.mention}", ephemeral=True)

    @bot.tree.command(name="set-booster-log", description="Atur channel & 3 pesan custom booster berdasarkan role")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        channel="Channel kirim notifikasi",
        role_male="Role pertama (misal: Cowok)",
        role_female="Role kedua (misal: Cewek)",
        msg_male="Pesan jika punya role pertama",
        msg_female="Pesan jika punya role kedua",
        msg_default="Pesan default/umum"
    )
    async def set_booster_log(
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        role_male: Optional[discord.Role] = None,
        role_female: Optional[discord.Role] = None,
        msg_male: str = "Makasih ganteng {user} udah boost server! Dapat {credits} credit.",
        msg_female: str = "Makasih cantik {user} udah boost server! Dapat {credits} credit.",
        msg_default: str = "Terima kasih {user} sudah boost server! Dapat {credits} credit."
    ) -> None:
        bot.ticket_store.set_booster_log(
            interaction.guild.id,
            channel.id,
            role_male.id if role_male else None,
            role_female.id if role_female else None,
            msg_male,
            msg_female,
            msg_default
        )
        await interaction.response.send_message(f"✅ Konfigurasi Booster Log berhasil disimpan ke {channel.mention}!", ephemeral=True)

    @bot.tree.command(name="edit-booster-log", description="Edit pesan/role kustom booster log")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        channel="Ubah channel tujuan",
        role_male="Ubah role pertama",
        role_female="Ubah role kedua",
        msg_male="Ubah pesan role pertama",
        msg_female="Ubah pesan role kedua",
        msg_default="Ubah pesan default"
    )
    async def edit_booster_log(
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
        role_male: Optional[discord.Role] = None,
        role_female: Optional[discord.Role] = None,
        msg_male: Optional[str] = None,
        msg_female: Optional[str] = None,
        msg_default: Optional[str] = None
    ) -> None:
        current = bot.ticket_store.get_booster_log(interaction.guild.id)
        if not current:
            await interaction.response.send_message("❌ Belum ada konfigurasi booster log.", ephemeral=True)
            return

        ch_id = channel.id if channel else current["channel_id"]
        r_male_id = role_male.id if role_male else current["role_male_id"]
        r_female_id = role_female.id if role_female else current["role_female_id"]
        m_male = msg_male if msg_male is not None else current["msg_male"]
        m_female = msg_female if msg_female is not None else current["msg_female"]
        m_default = msg_default if msg_default is not None else current["msg_default"]

        bot.ticket_store.set_booster_log(interaction.guild.id, ch_id, r_male_id, r_female_id, m_male, m_female, m_default)
        await interaction.response.send_message("✅ Konfigurasi Booster Log berhasil diperbarui!", ephemeral=True)

    @bot.tree.command(name="test-booster-log", description="Simulasi kirim 3 pesan booster log")
    @app_commands.default_permissions(administrator=True)
    async def test_booster_log(interaction: discord.Interaction) -> None:
        row = bot.ticket_store.get_booster_log(interaction.guild.id)
        if not row:
            await interaction.response.send_message("❌ Belum ada konfigurasi booster log.", ephemeral=True)
            return
        ch = interaction.guild.get_channel(int(row["channel_id"]))
        if not isinstance(ch, discord.TextChannel):
            await interaction.response.send_message("❌ Channel tujuan tidak valid.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        dummy_credits = str(bot.settings.booster_initial_credits)

        for template, title in [(row["msg_male"], "Male"), (row["msg_female"], "Female"), (row["msg_default"], "Default")]:
            text = template.replace("{user}", interaction.user.mention).replace("{credits}", dummy_credits)
            emb = discord.Embed(title=f"[TEST] {title}", description=text, colour=discord.Colour.magenta())
            emb.set_thumbnail(url=interaction.user.display_avatar.url)
            await ch.send(embed=emb)

        await interaction.followup.send(f"✅ Simulasi 3 pesan booster dikirim ke {ch.mention}!", ephemeral=True)

    @bot.tree.command(name="test-welcome", description="Test simulasi pesan welcome")
    @app_commands.default_permissions(administrator=True)
    async def test_welcome(interaction: discord.Interaction) -> None:
        row = bot.ticket_store.get_welcome(interaction.guild.id)
        if not row:
            await interaction.response.send_message("❌ Belum ada pesan welcome.", ephemeral=True)
            return
        ch = interaction.guild.get_channel(int(row["channel_id"]))
        if not isinstance(ch, discord.TextChannel):
            await interaction.response.send_message("❌ Channel welcome tidak ditemukan.", ephemeral=True)
            return

        embed = discord.Embed(description=str(row["message_text"]).replace("{user}", interaction.user.mention), colour=discord.Colour.orange())
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        await ch.send(embed=embed)
        await interaction.response.send_message(f"✅ Simulasi welcome dikirim ke {ch.mention}!", ephemeral=True)

    @bot.tree.command(name="reaction-role-create", description="Buat reaction role klasik dengan counter emoji")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        channel="Channel tempat panel dikirim",
        name="Judul panel reaction role",
        content="Deskripsi / daftar role & emoji",
        image_file="Upload file foto/GIF banner di bawah (opsional)",
        emoji1="Emoji pertama", role1="Role pertama",
        emoji2="Emoji kedua", role2="Role kedua",
        emoji3="Emoji ketiga", role3="Role ketiga",
        emoji4="Emoji keempat", role4="Role keempat",
        emoji5="Emoji kelima", role5="Role kelima"
    )
    async def reaction_role_create(
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        name: str,
        content: str,
        image_file: Optional[discord.Attachment] = None,
        emoji1: Optional[str] = None, role1: Optional[discord.Role] = None,
        emoji2: Optional[str] = None, role2: Optional[discord.Role] = None,
        emoji3: Optional[str] = None, role3: Optional[discord.Role] = None,
        emoji4: Optional[str] = None, role4: Optional[discord.Role] = None,
        emoji5: Optional[str] = None, role5: Optional[discord.Role] = None
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        pairs = [(emoji1, role1), (emoji2, role2), (emoji3, role3), (emoji4, role4), (emoji5, role5)]
        valid_pairs = [(e.strip(), r) for e, r in pairs if e and r]

        if not valid_pairs:
            await interaction.followup.send("❌ Masukkan setidaknya 1 pasangan emoji dan role!", ephemeral=True)
            return

        items = [ReactionRoleItem(emoji=e, role_id=r.id, role_name=r.name) for e, r in valid_pairs]

        image_url = None
        file_to_send = None
        if image_file:
            file_to_send = await image_file.to_file()
            image_url = f"attachment://{image_file.filename}"

        cfg_id = bot.reaction_roles.create_draft(
            guild_id=interaction.guild.id,
            channel_id=channel.id,
            name=name,
            roles=items,
            content=content,
            image_url=image_url,
            created_by=interaction.user.id
        )

        embed = discord.Embed(title=name, description=content, colour=discord.Colour.dark_embed())
        if image_url:
            embed.set_image(url=image_url)

        if file_to_send:
            msg = await channel.send(embed=embed, file=file_to_send)
        else:
            msg = await channel.send(embed=embed)

        for item in items:
            try:
                custom_emoji_match = re.search(r"<a?:([a-zA-Z0-9\_]+):([0-9]+)>", item.emoji)
                if custom_emoji_match:
                    emoji_obj = discord.PartialEmoji(name=custom_emoji_match.group(1), id=int(custom_emoji_match.group(2)))
                    await msg.add_reaction(emoji_obj)
                else:
                    await msg.add_reaction(item.emoji)
            except Exception as ex:
                LOGGER.error(f"Gagal menambahkan reaksi {item.emoji}: {ex}")

        bot.reaction_roles.publish(cfg_id, msg.id)
        await interaction.followup.send(f"✅ Panel Reaction Role klasik dengan **{len(items)} role** berhasil dikirim ke {channel.mention}!", ephemeral=True)

    @bot.tree.command(name="say", description="Kirim pesan melalui bot ke channel (bisa dengan lampiran foto)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        message="Pesan yang ingin dikirimkan bot",
        channel="Channel tujuan (opsional)",
        image_file="Upload file foto/gambar/GIF (opsional)"
    )
    async def say(
        interaction: discord.Interaction,
        message: str,
        channel: Optional[discord.TextChannel] = None,
        image_file: Optional[discord.Attachment] = None
    ) -> None:
        target_channel = channel or interaction.channel
        if not isinstance(target_channel, discord.TextChannel):
            await interaction.response.send_message("Channel tidak valid.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        if image_file:
            file_obj = await image_file.to_file()
            await target_channel.send(content=message, file=file_obj)
        else:
            await target_channel.send(content=message)

        await interaction.followup.send(f"✅ Pesan berhasil dikirim ke {target_channel.mention}", ephemeral=True)

    @app_commands.guild_only()
    @bot.tree.command(name="say-edit", description="Edit pesan yang pernah dikirim bot melalui /say")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(message_id="ID pesan bot yang mau diedit", new_text="Isi pesan yang baru", channel="Channel tempat pesan berada")
    async def say_edit(interaction: discord.Interaction, message_id: str, new_text: str, channel: discord.TextChannel) -> None:
        try:
            msg_obj = await channel.fetch_message(int(message_id))
            if msg_obj.author != bot.user:
                await interaction.response.send_message("Pesan tersebut bukan dikirim oleh bot ini.", ephemeral=True)
                return
            await msg_obj.edit(content=new_text)
            await interaction.response.send_message("✅ Pesan berhasil diperbarui!", ephemeral=True)
        except Exception:
            await interaction.response.send_message("Gagal mengedit pesan. Pastikan ID pesan dan channel-nya benar.", ephemeral=True)

    bot.run(settings.token)

if __name__ == "__main__":
    main()
