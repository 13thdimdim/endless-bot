from __future__ import annotations

import asyncio
import logging
import os
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
    return "Ticket Bot is Online and Alive!", 200

def run_server():
    serve(app, host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run_server)
    t.daemon = True
    t.start()

LOGGER = logging.getLogger("ticket_bot")

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
            database_path=Path(os.getenv("DATABASE_PATH", "data/tickets.sqlite3")),
            guild_id=guild_id,
        )

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

class TicketDropdown(discord.ui.Select):
    def __init__(self, choices_list: list[str]) -> None:
        options = []
        for choice in choices_list:
            clean = choice.strip()
            if not clean:
                continue
            parts = clean.split(" ", 1)
            emoji, label = (parts[0], parts[1]) if len(parts) == 2 and not parts[0].isalnum() else ("🎫", clean)
            options.append(discord.SelectOption(label=label[:100], value=label[:100].lower().replace(" ", "_"), emoji=emoji))
        if not options:
            options.append(discord.SelectOption(label="Support", value="support", emoji="🎫"))
        super().__init__(placeholder="PILIH TICKET DISINI", min_values=1, max_values=1, options=options[:25], custom_id="ticket:select")

    async def callback(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        bot: TicketBot = interaction.client  # type: ignore
        with bot.ticket_store._connect() as conn:
            row = conn.execute("SELECT * FROM ticket_panels WHERE message_id = ?", (interaction.message.id,)).fetchone()
        if not row:
            return
        
        type_display = self.values[0]
        for opt in self.options:
            if opt.value == type_display:
                type_display = opt.label
                break

        ticket_name = f"ticket-{abs(hash(str(interaction.user.id) + str(datetime.now()))) % 9000 + 1000}"
        await interaction.response.defer(ephemeral=True)

        try:
            overwrites = {
                interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                interaction.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
            }
            category = interaction.channel.category if isinstance(interaction.channel, discord.TextChannel) else None
            channel = await interaction.guild.create_text_channel(name=ticket_name, category=category, overwrites=overwrites)
            
            opened_at = datetime.now().strftime("%d %b %Y at %H:%M")
            bot.ticket_store.add_active_ticket(channel.id, interaction.guild.id, interaction.user.id, ticket_name, type_display, opened_at, row["log_open_id"], row["log_closed_id"])

            log_ch = interaction.guild.get_channel(int(row["log_open_id"]))
            if isinstance(log_ch, discord.TextChannel):
                embed = discord.Embed(title="Ticket Opened", colour=discord.Colour.gold())
                embed.add_field(name="Name", value=ticket_name, inline=False)
                embed.add_field(name="By", value=interaction.user.mention, inline=False)
                embed.add_field(name="Type", value=type_display, inline=False)
                await log_ch.send(embed=embed)

            embed = discord.Embed(title="Ticket Opened", description=f"{interaction.user.mention} membuka tiket {type_display}.", colour=discord.Colour.dark_embed())
            await channel.send(content=interaction.user.mention, embed=embed, view=TicketControlView())
            await interaction.followup.send(f"✅ Tiket dibuat: {channel.mention}", ephemeral=True)
        except Exception:
            await interaction.followup.send("Gagal membuat tiket. Pastikan bot memiliki izin Manage Channels.", ephemeral=True)

class TicketPanelView(discord.ui.View):
    def __init__(self, choices: list[str]) -> None:
        super().__init__(timeout=None)
        self.add_item(TicketDropdown(choices))

class TicketControlView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="Close Ticket", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="ticket:close"))
        self.add_item(discord.ui.Button(label="Claim Ticket", style=discord.ButtonStyle.secondary, emoji="📦", custom_id="ticket:claim"))

async def handle_persistent_buttons(interaction: discord.Interaction):
    if not interaction.data:
        return
    custom_id = interaction.data.get("custom_id")
    
    if custom_id == "ticket:close":
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            return
        bot: TicketBot = interaction.client  # type: ignore
        ticket = bot.ticket_store.get_active_ticket(interaction.channel.id)
        await interaction.response.send_message("🔒 Menutup tiket...", ephemeral=False)
        await asyncio.sleep(2)
        if ticket:
            log_ch = interaction.guild.get_channel(int(ticket["log_closed_id"]))
            if isinstance(log_ch, discord.TextChannel):
                embed = discord.Embed(title="Ticket Closed", colour=discord.Colour.red())
                embed.add_field(name="Name", value=ticket["ticket_name"], inline=False)
                embed.add_field(name="Closed By", value=interaction.user.mention, inline=False)
                await log_ch.send(embed=embed)
            bot.ticket_store.remove_active_ticket(interaction.channel.id)
        try:
            await interaction.channel.delete()
        except Exception:
            pass

    elif custom_id == "ticket:claim":
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            return
        bot: TicketBot = interaction.client  # type: ignore
        ticket = bot.ticket_store.get_active_ticket(interaction.channel.id)
        if not ticket:
            return
        if ticket["claimed_by"] != "None":
            await interaction.response.send_message(f"Sudah diklaim oleh {ticket['claimed_by']}", ephemeral=True)
            return
        bot.ticket_store.update_claimed_by(interaction.channel.id, interaction.user.mention)
        await interaction.response.send_message(f"📦 Diklaim oleh {interaction.user.mention}", ephemeral=False)

class TicketBot(commands.Bot):
    def __init__(self, settings: BotSettings) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.guilds = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, status=discord.Status.dnd)
        self.settings = settings
        self.ticket_store = TicketStore(settings.database_path)

    async def setup_hook(self) -> None:
        self.add_view(TicketControlView())
        self.add_listener(handle_persistent_buttons, "on_interaction")

        with self.ticket_store._connect() as conn:
            for p in conn.execute("SELECT choices FROM ticket_panels WHERE message_id > 0").fetchall():
                self.add_view(TicketPanelView([c.strip() for c in p["choices"].split(",") if c.strip()]))

        if self.settings.guild_id:
            guild_obj = discord.Object(id=self.settings.guild_id)
            self.tree.copy_global_to(guild=guild_obj)
            await self.tree.sync(guild=guild_obj)
        else:
            await self.tree.sync()

    async def on_ready(self) -> None:
        LOGGER.info("Logged in as %s", self.user)

def main() -> None:
    keep_alive()
    logging.basicConfig(level=logging.INFO)
    settings = BotSettings.from_environment()
    bot = TicketBot(settings)

    @bot.tree.command(name="ticket-panel", description="Buat panel tiket bantuan")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        title="Judul panel",
        description="Deskripsi panel",
        choices="Pilihan tiket dipisah koma (contoh: 🎫 Support, 🐛 Bug Report)",
        log_open="Channel log saat tiket dibuka",
        log_closed="Channel log saat tiket ditutup"
    )
    async def ticket_panel(
        interaction: discord.Interaction,
        title: str,
        description: str,
        choices: str,
        log_open: discord.TextChannel,
        log_closed: discord.TextChannel
    ) -> None:
        panel_id = bot.ticket_store.create_panel(
            interaction.guild.id,
            interaction.channel.id,
            title,
            description,
            choices,
            log_open.id,
            log_closed.id,
            interaction.user.id
        )
        choices_list = [c.strip() for c in choices.split(",") if c.strip()]
        
        content_text = f"**{title}**\n{description}"
        view = TicketPanelView(choices_list)
        msg = await interaction.channel.send(content=content_text, view=view)
        
        bot.ticket_store.publish(panel_id, msg.id)
        bot.add_view(view, message_id=msg.id)
        await interaction.response.send_message("✅ Panel tiket berhasil dibuat!", ephemeral=True)

    bot.run(settings.token)

if __name__ == "__main__":
    main()
