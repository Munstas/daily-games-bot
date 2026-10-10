"""
Discord bot that reads shared results from daily games and keeps a scoreboard.
Games: Fermi, Krillion, MapTap, Clues by Sam.

Slash commands: /today, /scoreboard [period], /records, /import [limit], /settings
"""
import logging
import os
import re
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]
# Optional first-run convenience: configures the channel of the server that has none yet.
# After that, every server manages its own channel with /settings channel.
CHANNEL_ID = int(os.getenv("CHANNEL_ID") or 0) or None


def parse_guild_ids(value):
    """'123, 456' -> [123, 456]. Ids can be separated by commas, semicolons or spaces."""
    return [int(part) for part in re.split(r"[,;\s]+", value or "") if part]


GUILD_IDS = parse_guild_ids(os.getenv("GUILD_ID"))  # optional: servers where slash commands appear instantly
DB_PATH = os.getenv("DB_PATH", "daily.db")
DEFAULT_TZ_NAME = os.getenv("TIMEZONE") or "UTC"  # used by servers that haven't set their own
ZoneInfo(DEFAULT_TZ_NAME)  # fail fast on a typo
BACKFILL_LIMIT = int(os.getenv("BACKFILL_LIMIT") or 500)  # messages scanned on startup

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("dailybot")

GAMES = {
    "fermi": "Fermi",
    "krillion": "Krillion",
    "maptap": "MapTap",
    "cluesbysam": "Clues by Sam",
}


# ----------------------------------------------------------------------------
# Parsers: each one returns a GameResult or None.
# `sort_key` is always "lower = better", so the rest of the code stays generic.
# ----------------------------------------------------------------------------
@dataclass
class GameResult:
    game: str
    puzzle: str      # key for the day's puzzle (puzzle number or ISO date)
    sort_key: float  # lower = better
    display: str     # how the result is shown to users


def _parse_date(month, day, now, year=None):
    """Turn 'Oct' + '2' (+ optional year) into a date. Without a year, use the message's."""
    try:
        d = datetime.strptime(f"{month[:3].title()} {int(day)} {year or now.year}", "%b %d %Y").date()
        if year is None and d > now.date() + timedelta(days=30):
            d = d.replace(year=d.year - 1)  # e.g. "December 31" shared in January
        return d
    except ValueError:
        return None


def _format_time(seconds):
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def parse_fermi(text, now):
    m_id = re.search(r"Fermi\s*[·•]?\s*No\.?\s*(\d+)", text, re.I)
    m_score = re.search(r"(\d+(?:[.,]\d+)?)\s*[×x]\s*score", text, re.I)
    if not (m_id and m_score):
        return None
    score = float(m_score.group(1).replace(",", "."))
    return GameResult("fermi", m_id.group(1), score, f"{score:.2f}×")  # lower is better


def parse_krillion(text, now):
    m = re.search(r"Krillion\s*#(\d+)[^\n]*\n\s*(\d+)", text, re.I)
    if not m:
        return None
    score = int(m.group(2))
    return GameResult("krillion", m.group(1), -score, str(score))  # higher is better


def parse_maptap(text, now):
    m_date = re.search(r"maptap\.gg\S*\s+([A-Za-z]+)\s+(\d{1,2})", text, re.I)
    m_score = re.search(r"final score:?\s*(\d+)", text, re.I)
    if not (m_date and m_score):
        return None
    d = _parse_date(m_date.group(1), m_date.group(2), now)
    if not d:
        return None
    score = int(m_score.group(1))
    return GameResult("maptap", d.isoformat(), -score, str(score))  # higher is better


SQUARES = "🟩🟨🟡🟠"  # every emoji the game can use in its grid
NON_GREEN = "🟨🟡🟠"    # anything that isn't a clean green square (🟡/🟠 = one/two hints used)


def parse_clues(text, now):
    # Two share formats: exact time ("in 05:09") or a bucket ("in less than 11 minutes")
    m = re.search(
        r"#CluesBySam,\s*([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?\s+(\d{4})\s*"
        r"\((\w+)\)\s*,?\s*in\s+"
        r"(?:less\s+than\s+(\d+|an?|one)\s+minutes?|(?:(\d+):)?(\d+):(\d{2}))",
        text, re.I,
    )
    if not m:
        return None
    d = _parse_date(m.group(1), m.group(2), now, int(m.group(3)))
    if not d:
        return None
    if m.group(5):  # "less than 11 minutes" means 10:xx; the bucket's lower bound is used,
        minutes = int(m.group(5)) if m.group(5).isdigit() else 1  # so two players in the same bucket tie
        seconds = (minutes - 1) * 60
        time_text = f"{minutes - 1:02d}:xx"
    else:
        seconds = int(m.group(6) or 0) * 3600 + int(m.group(7)) * 60 + int(m.group(8))
        time_text = _format_time(seconds)

    # Only look at the grid right after the header
    block = re.search(rf"(?:^[{SQUARES}]+[ \t]*$\n?)+", text[m.end():], re.M)
    if not block:
        return None
    mistakes = sum(block.group(0).count(c) for c in NON_GREEN)

    # Fewest non-green squares first, then fastest time
    sort_key = mistakes * 1_000_000 + seconds
    detail = "all green" if mistakes == 0 else f"{mistakes} non-green"
    return GameResult("cluesbysam", d.isoformat(), sort_key, f"{time_text} · {detail}")


PARSERS = [parse_fermi, parse_krillion, parse_maptap, parse_clues]


def extract_results(text, now):
    found = []
    for parser in PARSERS:
        try:
            result = parser(text, now)
        except Exception:
            log.exception("Error in parser %s", parser.__name__)
            continue
        if result:
            found.append(result)
    return found


# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------
@contextmanager
def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        with con:
            yield con
    finally:
        con.close()


def init_db():
    with connect() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS results (
                guild_id INTEGER NOT NULL,
                game     TEXT    NOT NULL,
                puzzle   TEXT    NOT NULL,
                user_id  INTEGER NOT NULL,
                name     TEXT    NOT NULL,
                sort_key REAL    NOT NULL,
                display  TEXT    NOT NULL,
                msg_id   INTEGER NOT NULL,
                day      TEXT    NOT NULL,
                PRIMARY KEY (guild_id, game, puzzle, user_id)
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_msg ON results(msg_id)")
        con.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id          INTEGER PRIMARY KEY,
                channel_id        INTEGER,
                timezone          TEXT,
                day_rollover_hour INTEGER NOT NULL DEFAULT 0,
                reactions         INTEGER NOT NULL DEFAULT 1
            )
        """)


# ----------------------------------------------------------------------------
# Server settings: one row per server. Everything has a default except the channel.
# ----------------------------------------------------------------------------
@dataclass
class GuildSettings:
    guild_id: int
    channel_id: Optional[int] = None  # the one required setting: where results are read from
    timezone: Optional[str] = None  # IANA name; None = use the default
    day_rollover_hour: int = 0  # hour at which a new game day starts (0 = midnight)
    reactions: bool = True  # react with ✅ / 🔁 to messages

    @property
    def tz(self):
        try:
            return ZoneInfo(self.timezone or DEFAULT_TZ_NAME)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            return ZoneInfo(DEFAULT_TZ_NAME)


SETTINGS_FIELDS = ("channel_id", "timezone", "day_rollover_hour", "reactions")


def get_settings(guild_id):
    with connect() as con:
        row = con.execute("SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,)).fetchone()
    if row is None:
        return GuildSettings(guild_id)
    return GuildSettings(guild_id, row["channel_id"], row["timezone"],
                         row["day_rollover_hour"], bool(row["reactions"]))


def update_settings(guild_id, **fields):
    unknown = set(fields) - set(SETTINGS_FIELDS)
    if unknown:
        raise ValueError(f"Unknown setting(s): {', '.join(sorted(unknown))}")
    if not fields:
        return
    with connect() as con:
        con.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (guild_id,))
        assignments = ", ".join(f"{name} = ?" for name in fields)  # names are whitelisted above
        con.execute(f"UPDATE guild_settings SET {assignments} WHERE guild_id = ?", (*fields.values(), guild_id))


def configured_channels():
    """[(guild_id, channel_id)] for every server that has finished setup."""
    with connect() as con:
        rows = con.execute("SELECT guild_id, channel_id FROM guild_settings WHERE channel_id IS NOT NULL").fetchall()
    return [(r["guild_id"], r["channel_id"]) for r in rows]


def seed_channel(guild_id, channel_id):
    """Set the channel only if the server has none yet. Returns True if it was set."""
    if get_settings(guild_id).channel_id is not None:
        return False
    update_settings(guild_id, channel_id=channel_id)
    return True


def game_day(local_time, rollover_hour=0):
    """The game day a (timezone-aware, local) moment belongs to.
    With rollover_hour=2, 01:30 still counts as the previous day."""
    return (local_time - timedelta(hours=rollover_hour)).date()


def current_day(settings):
    return game_day(datetime.now(settings.tz), settings.day_rollover_hour)


def parse_timezone(name):
    """The IANA name if it is a valid timezone, otherwise None."""
    try:
        return ZoneInfo(name.strip()).key
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


@lru_cache(maxsize=1)
def timezone_names():
    return sorted(available_timezones())


REQUIRED_PERMISSIONS = {
    "view_channel": "View Channel",
    "send_messages": "Send Messages",
    "embed_links": "Embed Links",
    "read_message_history": "Read Message History",
    "add_reactions": "Add Reactions",
}


def missing_permissions(perms):
    """Names of the permissions the bot needs in the results channel but doesn't have."""
    return [label for attr, label in REQUIRED_PERMISSIONS.items() if not getattr(perms, attr)]


def save_message(msg):
    """Store the results found in a message. Returns (new, duplicates)."""
    settings = get_settings(msg.guild.id)
    now = msg.created_at.astimezone(settings.tz)
    day = game_day(now, settings.day_rollover_hour).isoformat()
    new = dup = 0
    with connect() as con:
        for r in extract_results(msg.content, now):
            try:
                con.execute(
                    "INSERT INTO results VALUES (?,?,?,?,?,?,?,?,?)",
                    (msg.guild.id, r.game, r.puzzle, msg.author.id,
                     msg.author.display_name, r.sort_key, r.display, msg.id,
                     day),
                )
                new += 1
            except sqlite3.IntegrityError:
                dup += 1
    return new, dup


def delete_messages(msg_ids):
    """Remove the results stored for any of these messages (one transaction)."""
    with connect() as con:
        con.executemany("DELETE FROM results WHERE msg_id = ?", [(i,) for i in msg_ids])


def delete_message(msg_id):
    delete_messages([msg_id])


def fetch_rows(guild_id):
    with connect() as con:
        return con.execute(
            "SELECT * FROM results WHERE guild_id = ? ORDER BY day, rowid", (guild_id,)
        ).fetchall()


# ----------------------------------------------------------------------------
# Scoring
# ----------------------------------------------------------------------------
def group_rows(rows):
    groups = defaultdict(list)
    for r in rows:
        groups[(r["game"], r["puzzle"])].append(r)
    return groups


def score_group(group):
    """Pairwise comparison: 1 point per opponent beaten, 0.5 per tie."""
    points = {r["user_id"]: 0.0 for r in group}
    for a in group:
        for b in group:
            if a is b:
                continue
            if a["sort_key"] < b["sort_key"]:
                points[a["user_id"]] += 1
            elif a["sort_key"] == b["sort_key"]:
                points[a["user_id"]] += 0.5
    return points


def rank_group(group):
    """[(position, row)], with ties sharing the same position."""
    group = sorted(group, key=lambda r: r["sort_key"])
    ranked, position, previous = [], 0, None
    for i, r in enumerate(group):
        if r["sort_key"] != previous:
            position, previous = i, r["sort_key"]
        ranked.append((position, r))
    return ranked


def medal(i):
    return ["🥇", "🥈", "🥉"][i] if i < 3 else f"{i + 1}."


def label(r):
    return r["puzzle"] if "-" in r["puzzle"] else f"#{r['puzzle']}"


def period_start(period, today):
    if period == "today":
        return today.isoformat()
    if period == "week":
        return (today - timedelta(days=today.weekday())).isoformat()
    if period == "month":
        return today.replace(day=1).isoformat()
    return None  # all time


def build_table(rows, since):
    table = {}
    names = {r["user_id"]: r["name"] for r in rows}  # latest name used wins
    for group in group_rows(rows).values():
        if since and max(r["day"] for r in group) < since:
            continue
        for uid, pts in score_group(group).items():
            entry = table.setdefault(uid, {"points": 0.0, "max": 0})
            entry["points"] += pts
            entry["max"] += len(group) - 1
    return table, names


# ----------------------------------------------------------------------------
# Bot
# ----------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True  # must also be enabled in the Developer Portal


class DailyBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if GUILD_IDS:  # instant sync, only in these servers
            for guild_id in GUILD_IDS:
                guild = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
        else:  # global sync: every server the bot is in (can take a while to show up)
            await self.tree.sync()


bot = DailyBot()


def is_valid_channel(msg):
    """Only the server's configured channel is read. No channel set means nothing is read."""
    if msg.guild is None:
        return False
    configured = get_settings(msg.guild.id).channel_id
    return configured is not None and msg.channel.id == configured


async def react(msg, emoji):
    try:
        await msg.add_reaction(emoji)
    except discord.HTTPException:
        log.warning("Could not add reaction (missing permissions?)")


async def process_message(msg):
    new, dup = save_message(msg)
    if not get_settings(msg.guild.id).reactions:
        return
    if new:
        await react(msg, "✅")
    elif dup:
        await react(msg, "🔁")  # this puzzle was already submitted


async def backfill_channel(channel, limit):
    """Scan recent history for results posted while the bot was offline."""
    imported = 0
    reactions = get_settings(channel.guild.id).reactions
    async for msg in channel.history(limit=limit):
        if msg.author.bot:
            continue
        new, _ = save_message(msg)
        if new:
            imported += 1
            if reactions:
                await react(msg, "✅")
    return imported


_backfilled = False


@bot.event
async def on_ready():
    global _backfilled
    log.info("Logged in as %s", bot.user)
    if _backfilled:  # on_ready can fire again after a reconnect
        return
    _backfilled = True

    if CHANNEL_ID:  # first-run convenience: configure the server that has no channel yet
        channel = bot.get_channel(CHANNEL_ID)
        if channel is None or getattr(channel, "guild", None) is None:
            log.warning("CHANNEL_ID %s not found (wrong ID, or the bot can't see that channel)", CHANNEL_ID)
        elif seed_channel(channel.guild.id, channel.id):
            log.info("Configured #%s as the results channel of %s (from CHANNEL_ID)", channel.name, channel.guild.name)

    configured = {guild_id for guild_id, _ in configured_channels()}
    for guild in bot.guilds:  # make a silent "nothing happens" easy to diagnose
        if guild.id not in configured:
            log.warning("Server '%s' has no results channel yet. Someone with the Manage Server permission "
                        "needs to run /settings channel there.", guild.name)

    for guild_id, channel_id in configured_channels():  # catch up on every configured server
        channel = bot.get_channel(channel_id)
        if channel is None:
            log.warning("Channel %s of server %s not found (deleted, or the bot can't see it)", channel_id, guild_id)
            continue
        imported = await backfill_channel(channel, BACKFILL_LIMIT)
        log.info("Backfill: imported %d message(s) from #%s (%s)", imported, channel.name, channel.guild.name)


@bot.event
async def on_message(msg):
    if msg.author.bot or not is_valid_channel(msg):
        return
    if not msg.content and not (msg.attachments or msg.embeds or msg.stickers):
        log.warning("Received a message with empty content. If this happens with text messages, "
                    "enable Message Content Intent in the Developer Portal.")
    await process_message(msg)


@bot.event
async def on_message_edit(before, after):
    if after.author.bot or not is_valid_channel(after) or before.content == after.content:
        return
    delete_message(after.id)  # the edit replaces whatever was stored
    await process_message(after)


@bot.event
async def on_raw_message_delete(payload):
    delete_message(payload.message_id)


@bot.event
async def on_raw_bulk_message_delete(payload):
    # Purges (e.g. from a moderation bot) arrive as ONE bulk event, not as one event per message
    delete_messages(payload.message_ids)


# ----------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------
async def require_setup(interaction):
    """Returns the server's settings, or explains that the channel must be set first and returns None."""
    if interaction.guild_id is None:
        await interaction.response.send_message("This only works in a server.", ephemeral=True)
        return None
    settings = get_settings(interaction.guild_id)
    if settings.channel_id is None:
        await interaction.response.send_message(
            "This server isn't set up yet. Someone with the **Manage Server** permission needs to run "
            "`/settings channel` in the channel where you post your results.",
            ephemeral=True,
        )
        return None
    return settings


@bot.tree.command(name="today", description="Today's results")
async def cmd_today(interaction: discord.Interaction):
    settings = await require_setup(interaction)
    if settings is None:
        return
    today = current_day(settings).isoformat()
    groups = group_rows(fetch_rows(interaction.guild_id))
    embed = discord.Embed(title="Today's results", colour=discord.Colour.blurple())
    for game, title in GAMES.items():
        for (g, _), group in sorted(groups.items()):
            if g != game or max(r["day"] for r in group) != today:
                continue
            text = "\n".join(f"{medal(p)} **{r['name']}** — {r['display']}" for p, r in rank_group(group))
            embed.add_field(name=f"{title} {label(group[0])}", value=text, inline=False)
    if not embed.fields:
        embed.description = "Nobody has submitted anything today yet."
    await interaction.response.send_message(embed=embed)


PERIODS = {"today": "Today", "week": "This week", "month": "This month", "all": "All time"}


@bot.tree.command(name="scoreboard", description="Points leaderboard")
@app_commands.describe(period="Period to show (default: this week)")
@app_commands.choices(period=[app_commands.Choice(name=n, value=v) for v, n in PERIODS.items()])
async def cmd_scoreboard(interaction: discord.Interaction, period: str = "week"):
    settings = await require_setup(interaction)
    if settings is None:
        return
    since = period_start(period, current_day(settings))
    table, names = build_table(fetch_rows(interaction.guild_id), since)
    embed = discord.Embed(title=f"Scoreboard · {PERIODS[period]}", colour=discord.Colour.gold())
    if not table:
        embed.description = "No results in this period yet."
    else:
        ordered = sorted(table.items(), key=lambda kv: (-kv[1]["points"], names[kv[0]]))
        embed.description = "\n".join(
            f"{medal(i)} **{names[uid]}** — {t['points']:g} / {t['max']} pts"
            for i, (uid, t) in enumerate(ordered)
        )
        embed.set_footer(text="Points = opponents beaten (tie = 0.5) / maximum possible")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="records", description="All-time best results")
async def cmd_records(interaction: discord.Interaction):
    if await require_setup(interaction) is None:
        return
    rows = fetch_rows(interaction.guild_id)
    names = {r["user_id"]: r["name"] for r in rows}
    embed = discord.Embed(title="All-time records", colour=discord.Colour.green())
    for game, title in GAMES.items():
        game_rows = [r for r in rows if r["game"] == game]
        if not game_rows:
            continue
        best = min(game_rows, key=lambda r: r["sort_key"])
        lines = [f"🏆 **{names[best['user_id']]}** — {best['display']} ({label(best)})"]
        per_user = {}
        for r in game_rows:
            current = per_user.get(r["user_id"])
            if current is None or r["sort_key"] < current["sort_key"]:
                per_user[r["user_id"]] = r
        for uid, r in sorted(per_user.items(), key=lambda kv: kv[1]["sort_key"]):
            lines.append(f"• {names[uid]}: {r['display']} ({label(r)})")
        embed.add_field(name=title, value="\n".join(lines)[:1024], inline=False)
    if not embed.fields:
        embed.description = "No results yet."
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="import", description="Scan the results channel for results posted while the bot was offline")
@app_commands.describe(limit="How many recent messages to scan (default 200)")
async def cmd_import(interaction: discord.Interaction, limit: app_commands.Range[int, 1, 1000] = 200):
    settings = await require_setup(interaction)
    if settings is None:
        return
    channel = interaction.guild.get_channel(settings.channel_id)
    if channel is None:
        await interaction.response.send_message(
            "I can't see the configured channel any more. Run `/settings channel` to pick it again.",
            ephemeral=True,
        )
        return
    await interaction.response.defer(thinking=True)
    imported = await backfill_channel(channel, limit)
    await interaction.followup.send(f"Imported results from {imported} message(s) in {channel.mention}.")


# ----------------------------------------------------------------------------
# /settings: viewing is open to everyone, changing needs the Manage Server permission
# ----------------------------------------------------------------------------
settings_group = app_commands.Group(name="settings", description="Configure the bot for this server", guild_only=True)
advanced_group = app_commands.Group(name="advanced", description="Optional settings", parent=settings_group)
bot.tree.add_command(settings_group)


async def require_manager(interaction):
    if interaction.user.guild_permissions.manage_guild:
        return True
    await interaction.response.send_message("You need the **Manage Server** permission to change settings.",
                                            ephemeral=True)
    return False


@settings_group.command(name="view", description="Show this server's settings")
async def cmd_settings_view(interaction: discord.Interaction):
    s = get_settings(interaction.guild_id)
    embed = discord.Embed(title="Settings", colour=discord.Colour.blurple())
    embed.add_field(
        name="Results channel",
        value=f"<#{s.channel_id}>" if s.channel_id else "⚠️ Not set. Run `/settings channel` (required)",
        inline=False,
    )
    embed.add_field(name="Timezone", value=f"{s.timezone or DEFAULT_TZ_NAME}" + ("" if s.timezone else " (default)"))
    embed.add_field(name="New day starts at", value=f"{s.day_rollover_hour:02d}:00")
    embed.add_field(name="Reactions", value="on" if s.reactions else "off")
    embed.set_footer(text="Timezone, day start and reactions are under /settings advanced")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@settings_group.command(name="channel", description="Set the channel where results are posted (required)")
@app_commands.describe(channel="The results channel (default: the channel you are typing in)")
async def cmd_settings_channel(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    if not await require_manager(interaction):
        return
    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        await interaction.response.send_message("Pick a regular text channel.", ephemeral=True)
        return
    missing = missing_permissions(target.permissions_for(interaction.guild.me))
    if missing:
        await interaction.response.send_message(
            f"I can't use {target.mention} yet. I'm missing these permissions there: **{', '.join(missing)}**. "
            "Fix the channel (or my role) permissions and run this again.",
            ephemeral=True,
        )
        return
    update_settings(interaction.guild_id, channel_id=target.id)
    await interaction.response.defer(ephemeral=True, thinking=True)
    imported = await backfill_channel(target, BACKFILL_LIMIT)
    await interaction.followup.send(
        f"Done. I'll read results in {target.mention}. "
        f"Imported {imported} result message(s) from its recent history.",
        ephemeral=True,
    )


@advanced_group.command(name="timezone", description="Set the server's timezone (decides where the day ends)")
@app_commands.describe(name="Timezone name, e.g. Europe/Lisbon or America/New_York")
async def cmd_settings_timezone(interaction: discord.Interaction, name: str):
    if not await require_manager(interaction):
        return
    key = parse_timezone(name)
    if key is None:
        await interaction.response.send_message(
            f"`{name}` isn't a valid timezone name. Start typing and pick one from the list, e.g. `Europe/Lisbon`.",
            ephemeral=True,
        )
        return
    update_settings(interaction.guild_id, timezone=key)
    local = datetime.now(ZoneInfo(key)).strftime("%H:%M")
    await interaction.response.send_message(
        f"Timezone set to **{key}** (it is {local} there now). It applies to new results.", ephemeral=True)


@cmd_settings_timezone.autocomplete("name")
async def timezone_autocomplete(interaction: discord.Interaction, current: str):
    wanted = current.strip().lower().replace(" ", "_")
    matches = [t for t in timezone_names() if wanted in t.lower()]
    return [app_commands.Choice(name=t, value=t) for t in matches[:25]]


@advanced_group.command(name="rollover", description="Set the hour at which a new game day starts (default 0 = midnight)")
@app_commands.describe(hour="Hour of the day, 0-12. With 2, a result posted at 01:30 still counts for the day before")
async def cmd_settings_rollover(interaction: discord.Interaction, hour: app_commands.Range[int, 0, 12]):
    if not await require_manager(interaction):
        return
    update_settings(interaction.guild_id, day_rollover_hour=hour)
    await interaction.response.send_message(
        f"A new day now starts at **{hour:02d}:00**. It applies to new results.", ephemeral=True)


@advanced_group.command(name="reactions", description="Turn reactions on or off")
@app_commands.describe(enabled="True to react to saved results, False to stay silent")
async def cmd_settings_reactions(interaction: discord.Interaction, enabled: bool):
    if not await require_manager(interaction):
        return
    update_settings(interaction.guild_id, reactions=enabled)
    await interaction.response.send_message(f"Reactions are now **{'on' if enabled else 'off'}**.", ephemeral=True)


if __name__ == "__main__":
    init_db()
    bot.run(TOKEN)