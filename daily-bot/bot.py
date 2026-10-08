"""
Discord bot that reads shared results from daily games and keeps a scoreboard.
Games: Fermi, Krillion, MapTap, Clues by Sam.

Slash commands: /today, /scoreboard [period], /records
"""
import logging
import os
import re
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]
CHANNEL_ID = int(os.getenv("CHANNEL_ID") or 0) or None  # optional: only listen to this channel
GUILD_ID = int(os.getenv("GUILD_ID") or 0) or None  # optional: instant slash command sync
DB_PATH = os.getenv("DB_PATH", "daily.db")
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Lisbon"))
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


SQUARES = "🟩🟨🟡🟠"
NON_GREEN = "🟨🟡🟠"


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


def save_message(msg):
    """Store the results found in a message. Returns (new, duplicates)."""
    now = msg.created_at.astimezone(TZ)
    new = dup = 0
    with connect() as con:
        for r in extract_results(msg.content, now):
            try:
                con.execute(
                    "INSERT INTO results VALUES (?,?,?,?,?,?,?,?,?)",
                    (msg.guild.id, r.game, r.puzzle, msg.author.id,
                     msg.author.display_name, r.sort_key, r.display, msg.id,
                     now.date().isoformat()),
                )
                new += 1
            except sqlite3.IntegrityError:
                dup += 1
    return new, dup


def delete_message(msg_id):
    with connect() as con:
        con.execute("DELETE FROM results WHERE msg_id = ?", (msg_id,))


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
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()


bot = DailyBot()


def is_valid_channel(msg):
    return msg.guild is not None and (CHANNEL_ID is None or msg.channel.id == CHANNEL_ID)


async def react(msg, emoji):
    try:
        await msg.add_reaction(emoji)
    except discord.HTTPException:
        log.warning("Could not add reaction (missing permissions?)")


async def process_message(msg):
    new, dup = save_message(msg)
    if new:
        await react(msg, "✅")
    elif dup:
        await react(msg, "🔁")  # this puzzle was already submitted


async def backfill_channel(channel, limit):
    """Scan recent history for results posted while the bot was offline."""
    imported = 0
    async for msg in channel.history(limit=limit):
        if msg.author.bot:
            continue
        new, _ = save_message(msg)
        if new:
            imported += 1
            await react(msg, "✅")
    return imported


_backfilled = False


@bot.event
async def on_ready():
    global _backfilled
    log.info("Logged in as %s", bot.user)
    if CHANNEL_ID and not _backfilled:  # on startup, catch up on the configured channel
        _backfilled = True
        channel = bot.get_channel(CHANNEL_ID)
        if channel is None:
            log.warning("CHANNEL_ID %s not found (wrong ID, or the bot can't see that channel)", CHANNEL_ID)
            return
        imported = await backfill_channel(channel, BACKFILL_LIMIT)
        log.info("Backfill: imported %d message(s) from #%s", imported, channel.name)


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


# ----------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------
async def guild_only(interaction):
    if interaction.guild_id is None:
        await interaction.response.send_message("This only works in a server.", ephemeral=True)
        return True
    return False


@bot.tree.command(name="today", description="Today's results")
async def cmd_today(interaction: discord.Interaction):
    if await guild_only(interaction):
        return
    today = datetime.now(TZ).date().isoformat()
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
    if await guild_only(interaction):
        return
    since = period_start(period, datetime.now(TZ).date())
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
    if await guild_only(interaction):
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


@bot.tree.command(name="import", description="Scan recent messages here for results posted while the bot was offline")
@app_commands.describe(limit="How many recent messages to scan (default 200)")
async def cmd_import(interaction: discord.Interaction, limit: app_commands.Range[int, 1, 1000] = 200):
    if await guild_only(interaction):
        return
    await interaction.response.defer(thinking=True)
    imported = await backfill_channel(interaction.channel, limit)
    await interaction.followup.send(f"Imported results from {imported} message(s).")


if __name__ == "__main__":
    init_db()
    bot.run(TOKEN)