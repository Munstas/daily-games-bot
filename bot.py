"""
Discord bot that reads shared results from daily games and keeps a scoreboard.
Games: Fermi, Krillion, MapTap, Clues by Sam.

Slash commands: /today, /scoreboard [period], /records, /import [limit], /settings
Also posts the ranking of each day on its own when the day is complete.
"""
import asyncio
import logging
import os
import re
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

import discord
from discord import app_commands
from discord.ext import tasks
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


def add_missing_columns(con, table, columns):
    """Minimal migration: add the columns an older database doesn't have yet."""
    existing = {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}
    for name, definition in columns.items():
        if name not in existing:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


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
                reactions         INTEGER NOT NULL DEFAULT 1,
                autopost          INTEGER NOT NULL DEFAULT 1,
                autopost_from     TEXT
            )
        """)
        # Databases created by an older version don't have the newer columns yet
        add_missing_columns(con, "guild_settings", {
            "autopost": "INTEGER NOT NULL DEFAULT 1",
            "autopost_from": "TEXT",
        })
        con.execute("""
            CREATE TABLE IF NOT EXISTS posted_days (
                guild_id INTEGER NOT NULL,
                day      TEXT    NOT NULL,
                PRIMARY KEY (guild_id, day)
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
    reactions: bool = True  # react to saved results
    autopost: bool = True  # announce the ranking when a day is complete
    autopost_from: Optional[str] = None  # first day (ISO) that may be announced; older days are never posted

    @property
    def tz(self):
        try:
            return ZoneInfo(self.timezone or DEFAULT_TZ_NAME)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            return ZoneInfo(DEFAULT_TZ_NAME)


SETTINGS_FIELDS = ("channel_id", "timezone", "day_rollover_hour", "reactions", "autopost", "autopost_from")


def get_settings(guild_id):
    with connect() as con:
        row = con.execute("SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,)).fetchone()
    if row is None:
        return GuildSettings(guild_id)
    return GuildSettings(guild_id, row["channel_id"], row["timezone"], row["day_rollover_hour"],
                         bool(row["reactions"]), bool(row["autopost"]), row["autopost_from"])


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


def build_table(rows, since, only_day=None):
    """Points per user. `since` keeps puzzles from that day on, `only_day` keeps just that day's puzzles."""
    table = {}
    names = {r["user_id"]: r["name"] for r in rows}  # latest name used wins
    for group in group_rows(rows).values():
        last_day = max(r["day"] for r in group)
        if since and last_day < since:
            continue
        if only_day and last_day != only_day:
            continue
        for uid, pts in score_group(group).items():
            entry = table.setdefault(uid, {"points": 0.0, "max": 0})
            entry["points"] += pts
            entry["max"] += len(group) - 1
    return table, names


# ----------------------------------------------------------------------------
# Daily rankings: what is shown for a day, and when a day is ready to be announced
# ----------------------------------------------------------------------------
AUTOPOST_WINDOW_DAYS = 7  # how far back a day can still be announced, and how far back habits are looked at


def day_groups(rows, day):
    """The puzzles that belong to a day (a puzzle belongs to the day of its latest result)."""
    return {key: group for key, group in group_rows(rows).items() if max(r["day"] for r in group) == day}


def has_comparison(rows, day):
    """True if at least one puzzle of that day has two or more players, i.e. there is a ranking to show."""
    return any(len(group) >= 2 for group in day_groups(rows, day).values())


def day_is_complete(rows, day):
    """Everyone who played a game in the 7 days before `day` has posted it again on `day`.
    Without any history nothing is expected, so the day can only end by the clock."""
    start = (date.fromisoformat(day) - timedelta(days=AUTOPOST_WINDOW_DAYS)).isoformat()
    expected = {(r["user_id"], r["game"]) for r in rows if start <= r["day"] < day}
    posted = {(r["user_id"], r["game"]) for r in rows if r["day"] == day}
    return bool(expected) and expected <= posted


def days_to_post(rows, posted, today, since):
    """Days that are ready to be closed now, oldest first.
    A day is ready once it has ended, or earlier if everyone expected has posted (and there is something to compare).
    Days before `since`, older than the window, or already posted are never returned."""
    today_iso = today.isoformat()
    earliest = max(since, (today - timedelta(days=AUTOPOST_WINDOW_DAYS)).isoformat())
    candidates = sorted({r["day"] for r in rows if earliest <= r["day"] <= today_iso} - set(posted))
    return [day for day in candidates
            if day < today_iso or (has_comparison(rows, day) and day_is_complete(rows, day))]


def posted_days(guild_id):
    with connect() as con:
        rows = con.execute("SELECT day FROM posted_days WHERE guild_id = ?", (guild_id,)).fetchall()
    return {r["day"] for r in rows}


def mark_posted(guild_id, days):
    with connect() as con:
        con.executemany("INSERT OR IGNORE INTO posted_days VALUES (?, ?)", [(guild_id, d) for d in days])


def format_day(day):
    return date.fromisoformat(day).strftime("%a %d %b")  # e.g. "Mon 05 Oct"


def add_day_fields(embed, groups):
    """One field per puzzle with its ranking, in the order the games are listed."""
    for game, title in GAMES.items():
        for (g, _), group in sorted(groups.items()):
            if g != game:
                continue
            text = "\n".join(f"{medal(p)} **{r['name']}** — {r['display']}" for p, r in rank_group(group))
            embed.add_field(name=f"{title} {label(group[0])}", value=text[:1024], inline=False)


def scoreboard_embed(rows, since, title):
    table, names = build_table(rows, since)
    embed = discord.Embed(title=title, colour=discord.Colour.gold())
    if not table:
        embed.description = "No results in this period yet."
        return embed
    ordered = sorted(table.items(), key=lambda kv: (-kv[1]["points"], names[kv[0]]))
    embed.description = "\n".join(
        f"{medal(i)} **{names[uid]}** — {t['points']:g} / {t['max']} pts"
        for i, (uid, t) in enumerate(ordered)
    )
    embed.set_footer(text="Points = opponents beaten (tie = 0.5) / maximum possible")
    return embed


def day_winners(rows, day):
    """'Alice (3 pts)' or 'Alice & Bob (1.5 pts)' for a day's top scorer(s)."""
    table, names = build_table(rows, None, only_day=day)
    if not table:
        return "no results"
    best = max(t["points"] for t in table.values())
    winners = sorted(names[uid] for uid, t in table.items() if t["points"] == best)
    return f"{' & '.join(winners)} ({best:g} pts)"


def autopost_embeds(rows, days):
    """The message announcing one or several days: the ranking (or a summary) plus the all-time scoreboard."""
    if len(days) == 1:
        first = discord.Embed(title=f"Results · {format_day(days[0])}", colour=discord.Colour.blurple())
        add_day_fields(first, day_groups(rows, days[0]))
    else:
        first = discord.Embed(title="Recent days", colour=discord.Colour.blurple(),
                              description="\n".join(f"**{format_day(d)}** — {day_winners(rows, d)}" for d in days))
        first.set_footer(text="Winner of each day, by points")
    return [first, scoreboard_embed(rows, None, "Scoreboard · All time")]


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
    """Save the results in a message and react. Returns how many new results were saved."""
    new, dup = save_message(msg)
    if get_settings(msg.guild.id).reactions:
        if new:
            await react(msg, "✅")
        elif dup:
            await react(msg, "🔁")  # this puzzle was already submitted
    return new


_autopost_locks = defaultdict(asyncio.Lock)  # one at a time per server, so a day is never announced twice
_autopost_warned = set()  # servers we already logged a "can't post" warning for


async def autopost(guild_id):
    """Announce every day of this server that is ready, in a single message."""
    async with _autopost_locks[guild_id]:
        settings = get_settings(guild_id)
        if not settings.autopost or settings.channel_id is None:
            return
        today = current_day(settings)
        since = settings.autopost_from
        if since is None:  # first time: only days from now on are ever announced, never old history
            since = today.isoformat()
            update_settings(guild_id, autopost_from=since)

        rows = fetch_rows(guild_id)
        ready = days_to_post(rows, posted_days(guild_id), today, since)
        if not ready:
            return
        announce = [d for d in ready if has_comparison(rows, d)]  # days with nothing to compare are skipped quietly
        if announce:
            channel = bot.get_channel(settings.channel_id)
            if channel is None:
                if guild_id not in _autopost_warned:
                    _autopost_warned.add(guild_id)
                    log.warning("Can't post the daily ranking in server %s: results channel not found", guild_id)
                return
            try:
                await channel.send(embeds=autopost_embeds(rows, announce),
                                   allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                if guild_id not in _autopost_warned:
                    _autopost_warned.add(guild_id)
                    log.warning("Can't post the daily ranking in #%s (missing permissions?)", channel.name)
                return
        _autopost_warned.discard(guild_id)
        mark_posted(guild_id, ready)


async def autopost_safely(guild_id):
    try:
        await autopost(guild_id)
    except Exception:
        log.exception("Automatic ranking failed for server %s", guild_id)


@tasks.loop(minutes=5)
async def autopost_loop():
    """Closes days that ended (also catches up after the bot was offline)."""
    for guild_id, _ in configured_channels():
        await autopost_safely(guild_id)


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

    autopost_loop.start()  # after the backfill, so results posted while offline are counted first


@bot.event
async def on_message(msg):
    if msg.author.bot or not is_valid_channel(msg):
        return
    if not msg.content and not (msg.attachments or msg.embeds or msg.stickers):
        log.warning("Received a message with empty content. If this happens with text messages, "
                    "enable Message Content Intent in the Developer Portal.")
    if await process_message(msg):
        await autopost_safely(msg.guild.id)  # this might have been the last result of the day


@bot.event
async def on_message_edit(before, after):
    if after.author.bot or not is_valid_channel(after) or before.content == after.content:
        return
    delete_message(after.id)  # the edit replaces whatever was stored
    if await process_message(after):
        await autopost_safely(after.guild.id)


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
    embed = discord.Embed(title="Today's results", colour=discord.Colour.blurple())
    add_day_fields(embed, day_groups(fetch_rows(interaction.guild_id), today))
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
    embed = scoreboard_embed(fetch_rows(interaction.guild_id), since, f"Scoreboard · {PERIODS[period]}")
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
    embed.add_field(name="Automatic rankings", value="on" if s.autopost else "off")
    embed.set_footer(text="Timezone, day start, reactions and automatic rankings are under /settings advanced")
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


@advanced_group.command(name="reactions", description="Turn the reactions on or off")
@app_commands.describe(enabled="True to react to saved results, False to stay silent")
async def cmd_settings_reactions(interaction: discord.Interaction, enabled: bool):
    if not await require_manager(interaction):
        return
    update_settings(interaction.guild_id, reactions=enabled)
    await interaction.response.send_message(f"Reactions are now **{'on' if enabled else 'off'}**.", ephemeral=True)


@advanced_group.command(name="autopost", description="Turn the automatic daily ranking on or off")
@app_commands.describe(enabled="True to post the ranking when everyone has posted or the day ends, False to stay silent")
async def cmd_settings_autopost(interaction: discord.Interaction, enabled: bool):
    if not await require_manager(interaction):
        return
    settings = get_settings(interaction.guild_id)
    fields = {"autopost": enabled}
    if enabled and not settings.autopost:  # days that passed while it was off are not announced afterwards
        fields["autopost_from"] = current_day(settings).isoformat()
    update_settings(interaction.guild_id, **fields)
    if enabled:
        text = ("Automatic rankings are now **on**. I'll post the ranking when everyone has posted their games, "
                "or when the day ends.")
    else:
        text = "Automatic rankings are now **off**."
    await interaction.response.send_message(text, ephemeral=True)


if __name__ == "__main__":
    init_db()
    bot.run(TOKEN)