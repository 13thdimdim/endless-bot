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

@dataclass(frozen=True)
class BotSettings:
    token: str
    database_path: Path
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
            database_path=Path(os.getenv("DATABASE_PATH", "/app/data/credits.sqlite3")),
            guild_id=guild_id,
        )

# ==================== DATABASE STORE ====================

@dataclass(frozen=True)
class ReactionRoleItem:
    emoji: str
    role_id: int
    role_name: str

class BotStore:
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
                CREATE TABLE IF NOT EXISTS welcome_settings (
                    guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, message_text TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS goodbye_settings (
                    guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, message_text TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reaction_role_configs (
                    config_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL DEFAULT 0, name TEXT NOT NULL,
                    content TEXT NOT NULL, image_url TEXT, created_by INTEGER NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reaction_role_items (
                    config_id INTEGER NOT NULL, emoji TEXT NOT NULL,
                    role_id INTEGER NOT NULL, role_name TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (config_id, emoji)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ticket_panels (
                    panel_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL DEFAULT 0, title TEXT NOT NULL,
                    description TEXT NOT NULL, choices TEXT NOT NULL, placeholder TEXT DEFAULT 'PILIH TICKET DISINI',
                    staff_role_id INTEGER, log_open_id INTEGER NOT NULL, log_closed_id INTEGER NOT NULL, created_by INTEGER NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS active_tickets (
                    channel_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL, ticket_name TEXT NOT NULL,
                    ticket_type TEXT NOT NULL, opened_at TEXT NOT NULL,
                    staff_role_id INTEGER, log_open_id INTEGER NOT NULL, log_closed_id INTEGER NOT NULL, claimed_by TEXT DEFAULT 'None'
                )
            """)
            conn.commit()

    def set_welcome(self, guild_id: int, channel_id: int, message_text: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO welcome_settings VALUES (?, ?, ?) ON CONFLICT(guild_id) DO UPDATE SET channel_id = excluded.channel_id, message_text = excluded.message_text",
                (guild_id, channel_id, message_text)
            )

    def get_welcome(self, guild_id: int):
        with self._connect() as conn:
            return conn.execute("SELECT * FROM welcome_settings WHERE guild_id = ?", (guild_id,)).fetchone()

    def set_goodbye(self, guild_id: int, channel_id: int, message_text: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO goodbye_settings VALUES (?, ?, ?) ON CONFLICT(guild_id) DO UPDATE SET channel_id = excluded.channel_id, message_text = excluded.message_text",
                (guild_id, channel_id, message_text)
            )

    def get_goodbye(self, guild_id: int):
        with self._connect() as conn:
            return conn.execute("SELECT * FROM goodbye_settings WHERE guild_id = ?", (guild_id,)).fetchone()

    def create_rr_draft(self, *, guild_id: int, channel_id: int, name: str, roles: list[ReactionRoleItem], content: str, image_url: Optional[str], created_by: int) -> int:
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

    def publish_rr(self, config_id: int, message_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE reaction_role_configs SET message_id = ? WHERE config_id = ?", (message_id, config_id))

    def get_rr_by_message(self, message_id: int):
        with self._connect() as conn:
            return conn.execute("SELECT * FROM reaction_role_configs WHERE message_id = ?", (message_id,)).fetchone()

    def get_rr_role_by_emoji(self, message_id: int, emoji: str):
        with self._connect() as conn:
            cfg = self.get_rr_by_message(message_id)
            if not cfg:
                return None
            row = conn.execute("SELECT role_id FROM reaction_role_items WHERE config_id = ? AND emoji = ?", (cfg["config_id"], emoji)).fetchone()
            return row["role_id"] if row else None

    def create_panel(self, guild_id: int, channel_id: int, title: str, description: str, choices: str, placeholder: str, staff_role_id: Optional[int], log_open_id: int, log_closed_id: int, created_by: int) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO ticket_panels (guild_id, channel_id, title, description, choices, placeholder, staff_role_id, log_open_id, log_closed_id, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (guild_id, channel_id, title, description, choices, placeholder, staff_role_id, log_open_id, log_closed_id, created_by)
            )
            return int(cursor.lastrowid)

    def publish_panel(self, panel_id: int, message_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE ticket_panels SET message_id = ? WHERE panel_id = ?", (message_id, panel_id))

    def add_active_ticket(self, channel_id: int, guild_id: int, user_id: int, ticket_name: str, ticket_type: str, opened_at: str, staff_role_id: Optional[int], log_open_id: int, log_closed_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO active_tickets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'None')",
                (channel_id, guild_id, user_id, ticket_name, ticket_type, opened_at, staff_role_id, log_open_id, log_closed_id)
            )

    def get_active_ticket(self, channel_id: int):
        with self._connect() as conn:
            return conn.execute("SELECT * FROM active_tickets WHERE channel_id = ?", (channel_id,)).fetchone()

    def get_active_ticket_by_user(self, guild_id: int, user_id: int):
        with self._connect() as conn:
            return conn.execute("SELECT * FROM active_tickets WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)).fetchone()

    def update_ticket_claim(self, channel_id: int, claimed_by: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE active_tickets SET claimed_by = ? WHERE channel_id = ?", (claimed_by, channel_id))

    def update_ticket_name(self, channel_id: int, new_name: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE active_tickets SET ticket_name = ? WHERE channel_id = ?", (new_name, channel_id))

    def remove_active_ticket(self, channel_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM active_tickets WHERE channel_id = ?", (channel_id,))

# ==================== TICKET VIEWS & MODALS ====================

class TicketCloseModal(discord.ui.Modal, title="Close Ticket"):
    reason = discord.ui.TextInput(
        label="Reason",
        placeholder="Alasan menutup tiket...",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=500
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            return

        bot: MinimalNethricBot = interaction.client  # type: ignore
        ticket = bot.store.get_active_ticket(interaction.channel.id)
        if not ticket:
            await interaction.response.send_message("❌ Data tiket tidak ditemukan.", ephemeral=True)
            return

        await interaction.response.send_message("🔒 Menutup tiket...", ephemeral=False)
        await asyncio.sleep(2)

        close_date = datetime.now().strftime("%B %d, %Y %I:%M %p")
        log_ch = interaction.guild.get_channel(int(ticket["log_closed_id"]))
        if isinstance(log_ch, discord.TextChannel):
            embed = discord.Embed(title="Ticket Closed", color=discord.Color.gold())
            embed.add_field(name="Ticket Name", value=ticket["ticket_name"], inline=True)
            embed.add_field(name="Ticket Author", value=f"<@{ticket['user_id']}>", inline=True)
            embed.add_field(name="Closed By", value=interaction.user.mention, inline=True)
            embed.add_field(name="Open Date", value=ticket["opened_at"], inline=True)
            embed.add_field(name="Close Date", value=close_date, inline=True)
            embed.add_field(name="Ticket Close Reason", value=str(self.reason), inline=False)
            await log_ch.send(embed=embed)

        bot.store.remove_active_ticket(interaction.channel.id)
        try:
            await interaction.channel.delete()
        except Exception:
            pass

class TicketRenameModal(discord.ui.Modal, title="Rename Ticket"):
    new_name = discord.ui.TextInput(
        label="New Ticket Name",
        placeholder="Masukkan nama baru channel...",
        style=discord.TextStyle.short,
        required=True,
        max_length=50
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            return

        bot: MinimalNethricBot = interaction.client  # type: ignore
        ticket = bot.store.get_active_ticket(interaction.channel.id)
        if not ticket:
            await interaction.response.send_message("❌ Data tiket tidak ditemukan.", ephemeral=True)
            return

        cleaned_name = re.sub(r'[^a-zA-Z0-9\-_]', '-', self.new_name.value.lower().strip())
        if not cleaned_name:
            await interaction.response.send_message("❌ Nama channel tidak valid.", ephemeral=True)
            return

        try:
            await interaction.channel.edit(name=cleaned_name)
            bot.store.update_ticket_name(interaction.channel.id, cleaned_name)
            await interaction.response.send_message(f"✅ Tiket direname menjadi **{cleaned_name}**", ephemeral=False)
        except Exception as e:
            await interaction.response.send_message(f"❌ Gagal rename: {e}", ephemeral=True)

class TicketControlView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="Close Ticket", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="ticket:close"))
        self.add_item(discord.ui.Button(label="Claim Ticket", style=discord.ButtonStyle.secondary, emoji="📑", custom_id="ticket:claim"))
        self.add_item(discord.ui.Button(label="Unclaim Ticket", style=discord.ButtonStyle.secondary, emoji="🔓", custom_id="ticket:unclaim"))
        self.add_item(discord.ui.Button(label="Rename Ticket", style=discord.ButtonStyle.primary, emoji="✏️", custom_id="ticket:rename"))

class TicketDropdown(discord.ui.Select):
    def __init__(self, choices_list: list[str], placeholder_text: str = "PILIH TICKET DISINI") -> None:
        options = []
        for choice in choices_list:
            clean = choice.strip()
            if not clean:
                continue
            parts = clean.split(" ", 1)
            if len(parts) == 2 and (not parts[0].isalnum() or parts[0].startswith("<")):
                emoji, label = parts[0], parts[1]
            else:
                emoji, label = "🎟️", clean
            options.append(discord.SelectOption(label=label[:100], value=clean[:100], emoji=emoji))

        if not options:
            options.append(discord.SelectOption(label="Create Ticket", value="Create Ticket", emoji="🎟️"))
        
        super().__init__(placeholder=placeholder_text[:100], min_values=1, max_values=1, options=options[:25], custom_id="ticket:select")

    async def callback(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        
        bot: MinimalNethricBot = interaction.client  # type: ignore
        guild = interaction.guild
        member = interaction.user

        # Anti-Spam Check
        existing_ticket = bot.store.get_active_ticket_by_user(guild.id, member.id)
        if existing_ticket:
            await interaction.response.send_message("❌ Ticket kamu yang sebelumnya masih aktif!", ephemeral=True)
            return

        with bot.store._connect() as conn:
            row = conn.execute("SELECT * FROM ticket_panels WHERE message_id = ?", (interaction.message.id,)).fetchone()
        if not row:
            await interaction.response.send_message("❌ Panel tiket tidak ditemukan.", ephemeral=True)
            return

        type_value = self.values[0]
        created_at = datetime.now().strftime("%B %d, %Y %I:%M %p")

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)
        }

        staff_role_id = row["staff_role_id"]
        if staff_role_id:
            staff_role = guild.get_role(staff_role_id)
            if staff_role:
                overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        random_id = abs(hash(str(member.id) + str(datetime.now()))) % 9000 + 1000
        ticket_name = f"ticket-{random_id}"

        try:
            channel = await guild.create_text_channel(
                name=ticket_name,
                overwrites=overwrites,
                topic=f"Opened {created_at} by @{member.name}"
            )

            bot.store.add_active_ticket(
                channel.id, guild.id, member.id, ticket_name, type_value, 
                created_at, staff_role_id, row["log_open_id"], row["log_closed_id"]
            )

            log_ch = guild.get_channel(int(row["log_open_id"]))
            if isinstance(log_ch, discord.TextChannel):
                embed = discord.Embed(title="Ticket Opened", color=discord.Color.gold())
                embed.add_field(name="Ticket Name", value=ticket_name, inline=True)
                embed.add_field(name="Created By", value=member.mention, inline=True)
                embed.add_field(name="Opened Date", value=created_at, inline=True)
                embed.add_field(name="Ticket Type", value=type_value, inline=False)
                await log_ch.send(embed=embed)

            msg_embed = discord.Embed(
                title="Ticket Opened",
                description=f"{member.mention} has created a new **{type_value}** ticket.\n📌 {bot.user.name} | Click close button below",
                color=discord.Color.gold()
            )
            await channel.send(content=member.mention, embed=msg_embed, view=TicketControlView())
            await interaction.response.send_message(f"✅ Tiket kamu berhasil dibuat di {channel.mention}!", ephemeral=True)

        except Exception as e:
            LOGGER.error(f"Error creating ticket: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(f"Gagal membuat tiket: {e}", ephemeral=True)

class TicketPanelView(discord.ui.View):
    def __init__(self, choices: list[str], placeholder: str) -> None:
        super().__init__(timeout=None)
        self.add_item(TicketDropdown(choices, placeholder))

async def handle_persistent_interactions(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
        return

    bot: MinimalNethricBot = interaction.client  # type: ignore
    custom_id = interaction.data.get("custom_id")
    ticket = bot.store.get_active_ticket(interaction.channel.id)
    if not ticket:
        return

    is_admin = interaction.user.guild_permissions.administrator
    staff_role_id = ticket["staff_role_id"]
    has_staff_role = staff_role_id and any(r.id == staff_role_id for r in interaction.user.roles)

    if not is_admin and not has_staff_role:
        await interaction.response.send_message("❌ Hanya admin atau staff khusus yang bisa melakukan tindakan ini.", ephemeral=True)
        return

    if custom_id == "ticket:close":
        await interaction.response.send_modal(TicketCloseModal())
    elif custom_id == "ticket:rename":
        await interaction.response.send_modal(TicketRenameModal())
    elif custom_id == "ticket:claim":
        if ticket["claimed_by"] != "None":
            await interaction.response.send_message("Tiket ini sudah diklaim.", ephemeral=True)
            return
        bot.store.update_ticket_claim(interaction.channel.id, interaction.user.mention)
        await interaction.response.send_message(f"✅ Tiket diklaim oleh {interaction.user.mention}", ephemeral=False)
    elif custom_id == "ticket:unclaim":
        if ticket["claimed_by"] == "None":
            await interaction.response.send_message("Tiket ini belum diklaim.", ephemeral=True)
            return
        bot.store.update_ticket_claim(interaction.channel.id, "None")
        await interaction.response.send_message(f"🔓 Status klaim tiket dibatalkan.", ephemeral=False)

# ==================== BOT CORE ====================

class MinimalNethricBot(commands.Bot):
    def __init__(self, settings: BotSettings) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.guilds = True
        intents.reactions = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, status=discord.Status.dnd)
        self.settings = settings
        self.store = BotStore(settings.database_path)

    async def setup_hook(self) -> None:
        self.add_view(TicketControlView())
        self.add_listener(handle_persistent_interactions, "on_interaction")

        with self.store._connect() as conn:
            for p in conn.execute("SELECT choices, placeholder FROM ticket_panels WHERE message_id > 0").fetchall():
                choices_list = [c.strip() for c in p["choices"].split(",") if c.strip()]
                placeholder_text = p["placeholder"] or "PILIH TICKET DISINI"
                self.add_view(TicketPanelView(choices_list, placeholder_text))

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
        role_id = self.store.get_rr_role_by_emoji(payload.message_id, emoji_str)
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
        role_id = self.store.get_rr_role_by_emoji(payload.message_id, emoji_str)
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
        row = self.store.get_welcome(member.guild.id)
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

    async def on_member_remove(self, member: discord.Member) -> None:
        row = self.store.get_goodbye(member.guild.id)
        if not row:
            return
        ch = member.guild.get_channel(int(row["channel_id"]))
        if isinstance(ch, discord.TextChannel):
            embed = discord.Embed(description=str(row["message_text"]).replace("{user}", member.mention), colour=discord.Colour.red())
            embed.set_thumbnail(url=member.display_avatar.url)
            try:
                await ch.send(embed=embed)
            except Exception:
                pass

def main() -> None:
    keep_alive()
    logging.basicConfig(level=logging.INFO)
    settings = BotSettings.from_environment()
    bot = MinimalNethricBot(settings)

    # --- COMMANDS ---

    @bot.tree.command(name="set-welcome", description="Atur pesan sambutan member baru")
    @app_commands.default_permissions(administrator=True)
    async def set_welcome(interaction: discord.Interaction, channel: discord.TextChannel, message: str) -> None:
        bot.store.set_welcome(interaction.guild.id, channel.id, message)
        await interaction.response.send_message(f"✅ Pesan sambutan diatur ke {channel.mention}", ephemeral=True)

    @bot.tree.command(name="test-welcome", description="Simulasi pesan welcome")
    @app_commands.default_permissions(administrator=True)
    async def test_welcome(interaction: discord.Interaction) -> None:
        row = bot.store.get_welcome(interaction.guild.id)
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

    @bot.tree.command(name="set-goodbye", description="Atur pesan perpisahan member keluar")
    @app_commands.default_permissions(administrator=True)
    async def set_goodbye(interaction: discord.Interaction, channel: discord.TextChannel, message: str) -> None:
        bot.store.set_goodbye(interaction.guild.id, channel.id, message)
        await interaction.response.send_message(f"✅ Pesan goodbye diatur ke {channel.mention}", ephemeral=True)

    @bot.tree.command(name="test-goodbye", description="Simulasi pesan goodbye")
    @app_commands.default_permissions(administrator=True)
    async def test_goodbye(interaction: discord.Interaction) -> None:
        row = bot.store.get_goodbye(interaction.guild.id)
        if not row:
            await interaction.response.send_message("❌ Belum ada pesan goodbye.", ephemeral=True)
            return
        ch = interaction.guild.get_channel(int(row["channel_id"]))
        if not isinstance(ch, discord.TextChannel):
            await interaction.response.send_message("❌ Channel goodbye tidak ditemukan.", ephemeral=True)
            return

        embed = discord.Embed(description=str(row["message_text"]).replace("{user}", interaction.user.mention), colour=discord.Colour.red())
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        await ch.send(embed=embed)
        await interaction.response.send_message(f"✅ Simulasi goodbye dikirim ke {ch.mention}!", ephemeral=True)

    @bot.tree.command(name="say", description="Kirim pesan melalui bot ke channel")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(message="Pesan bot", channel="Channel tujuan (opsional)", image_file="Lampiran gambar (opsional)")
    async def say(interaction: discord.Interaction, message: str, channel: Optional[discord.TextChannel] = None, image_file: Optional[discord.Attachment] = None) -> None:
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
        await interaction.followup.send(f"✅ Pesan dikirim ke {target_channel.mention}", ephemeral=True)

    @app_commands.guild_only()
    @bot.tree.command(name="say-edit", description="Edit pesan yang dikirim bot melalui /say")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(message_id="ID pesan", new_text="Isi pesan baru", channel="Channel tempat pesan")
    async def say_edit(interaction: discord.Interaction, message_id: str, new_text: str, channel: discord.TextChannel) -> None:
        try:
            msg_obj = await channel.fetch_message(int(message_id))
            if msg_obj.author != bot.user:
                await interaction.response.send_message("Pesan tersebut bukan dari bot ini.", ephemeral=True)
                return
            await msg_obj.edit(content=new_text)
            await interaction.response.send_message("✅ Pesan diperbarui!", ephemeral=True)
        except Exception:
            await interaction.response.send_message("Gagal mengedit pesan.", ephemeral=True)

    @bot.tree.command(name="ticket-panel", description="Buat panel tiket bantuan")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        title="Judul panel", description="Deskripsi", choices="Contoh: 👤 Member, 🎟️ Support (pisahkan koma)",
        placeholder="Placeholder dropdown", staff_role="Role khusus staff (opsional)",
        log_open="Channel log buka", log_closed="Channel log tutup"
    )
    async def ticket_panel(
        interaction: discord.Interaction, title: str, description: str, choices: str, 
        placeholder: str = "PILIH TICKET DISINI",
        staff_role: Optional[discord.Role] = None,
        log_open: discord.TextChannel = None, # type: ignore
        log_closed: discord.TextChannel = None # type: ignore
    ) -> None:
        choices_list = [c.strip() for c in choices.split(",") if c.strip()]
        if not choices_list:
            choices_list = ["🎟️ Create Ticket"]

        staff_role_id = staff_role.id if staff_role else None

        panel_id = bot.store.create_panel(
            interaction.guild.id, interaction.channel.id, title, description, 
            ",".join(choices_list), placeholder, staff_role_id, log_open.id, log_closed.id, interaction.user.id
        )

        embed = discord.Embed(title=title, description=description, color=discord.Color.dark_embed())
        view = TicketPanelView(choices_list, placeholder)
        msg = await interaction.channel.send(embed=embed, view=view)

        bot.store.publish_panel(panel_id, msg.id)
        bot.add_view(view, message_id=msg.id)
        await interaction.response.send_message("✅ Panel tiket berhasil dibuat!", ephemeral=True)

    bot.run(settings.token)

if __name__ == "__main__":
    main()
