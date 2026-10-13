"""
Discord bot that reads shared results from daily games and keeps a scoreboard.
Games: Clues by Sam, Fermi, Krillion, MapTap.

Slash commands: /today, /scoreboard [period], /records, /stats [player], /import [limit], /settings, /clear, /help
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
    "cluesbysam": "Clues by Sam",
    "fermi": "Fermi",
    "krillion": "Krillion",
    "maptap": "MapTap",
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


PARSERS = [parse_clues, parse_fermi, parse_krillion, parse_maptap]


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


def normalize_days(con):
    """Give every stored result the day of its puzzle (see puzzle_day), also those saved by older versions.
    Safe to run on every start: once the days are right, nothing changes."""
    con.execute("UPDATE results SET day = puzzle WHERE puzzle GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' "
                "AND puzzle < day")
    con.execute("""
        UPDATE results SET day = (SELECT MIN(r.day) FROM results r
                                  WHERE r.guild_id = results.guild_id AND r.game = results.game
                                    AND r.puzzle = results.puzzle)
        WHERE day > (SELECT MIN(r.day) FROM results r
                     WHERE r.guild_id = results.guild_id AND r.game = results.game AND r.puzzle = results.puzzle)
    """)


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
                channel_id INTEGER,
                PRIMARY KEY (guild_id, game, puzzle, user_id)
            )
        """)
        add_missing_columns(con, "results", {"channel_id": "INTEGER"})  # databases from before channels were stored
        con.execute("""
            CREATE TABLE IF NOT EXISTS cleared_results (
                guild_id INTEGER NOT NULL,
                msg_id   INTEGER NOT NULL,
                game     TEXT    NOT NULL,
                PRIMARY KEY (guild_id, msg_id, game)
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_msg ON results(msg_id)")
        normalize_days(con)
        con.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id          INTEGER PRIMARY KEY,
                channel_id        INTEGER,
                timezone          TEXT,
                day_rollover_hour INTEGER NOT NULL DEFAULT 0,
                reactions         INTEGER NOT NULL DEFAULT 1,
                autopost          INTEGER NOT NULL DEFAULT 1,
                autopost_from     TEXT,
                guide_channel_id  INTEGER
            )
        """)
        # Databases created by an older version don't have the newer columns yet
        add_missing_columns(con, "guild_settings", {
            "autopost": "INTEGER NOT NULL DEFAULT 1",
            "autopost_from": "TEXT",
            "guide_channel_id": "INTEGER",
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
    guide_channel_id: Optional[int] = None  # channel where the usage guide was last posted (never twice in one)

    @property
    def tz(self):
        try:
            return ZoneInfo(self.timezone or DEFAULT_TZ_NAME)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            return ZoneInfo(DEFAULT_TZ_NAME)


SETTINGS_FIELDS = ("channel_id", "timezone", "day_rollover_hour", "reactions", "autopost", "autopost_from",
                   "guide_channel_id")


def get_settings(guild_id):
    with connect() as con:
        row = con.execute("SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,)).fetchone()
    if row is None:
        return GuildSettings(guild_id)
    return GuildSettings(guild_id, row["channel_id"], row["timezone"], row["day_rollover_hour"],
                         bool(row["reactions"]), bool(row["autopost"]), row["autopost_from"],
                         row["guide_channel_id"])


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
    """Set the channel only if the server has none yet. Returns True if it was set.
    Whoever uses this shortcut already knows the bot, so no usage guide is posted."""
    if get_settings(guild_id).channel_id is not None:
        return False
    update_settings(guild_id, channel_id=channel_id, guide_channel_id=channel_id)
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


def puzzle_day(con, guild_id, result, posted_day):
    """The game day a result belongs to: the day its puzzle was played, not necessarily the day it was posted.
    Yesterday's puzzle posted late therefore stays on yesterday: it counts in the scoreboards but not in today's
    results. MapTap and Clues by Sam carry their own date; for the other games the puzzle is placed on the first
    day anyone posted it. (If the late post is the very first one of a numbered puzzle, there is no way to know.)"""
    day = posted_day
    try:
        day = min(day, date.fromisoformat(result.puzzle).isoformat())
    except ValueError:  # a puzzle number, not a date
        pass
    earliest = con.execute("SELECT MIN(day) FROM results WHERE guild_id = ? AND game = ? AND puzzle = ?",
                           (guild_id, result.game, result.puzzle)).fetchone()[0]
    return min(day, earliest) if earliest else day


def save_message(msg):
    """Store the results found in a message. Returns (new, duplicates).
    Results that were removed on purpose with /clear are never stored again."""
    settings = get_settings(msg.guild.id)
    now = msg.created_at.astimezone(settings.tz)
    day = game_day(now, settings.day_rollover_hour).isoformat()
    new = dup = 0
    with connect() as con:
        for r in extract_results(msg.content, now):
            cleared = con.execute("SELECT 1 FROM cleared_results WHERE guild_id = ? AND msg_id = ? AND game = ?",
                                  (msg.guild.id, msg.id, r.game)).fetchone()
            if cleared:
                continue
            result_day = puzzle_day(con, msg.guild.id, r, day)
            try:
                con.execute(
                    "INSERT INTO results (guild_id, game, puzzle, user_id, name, sort_key, display, msg_id, day, "
                    "channel_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (msg.guild.id, r.game, r.puzzle, msg.author.id,
                     msg.author.display_name, r.sort_key, r.display, msg.id,
                     result_day, msg.channel.id),
                )
                new += 1
                # Messages can be read out of order (a scan goes newest first): the puzzle takes its earliest day
                con.execute("UPDATE results SET day = ? WHERE guild_id = ? AND game = ? AND puzzle = ? AND day > ?",
                            (result_day, msg.guild.id, r.game, r.puzzle, result_day))
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


def _result_filter(guild_id, day=None, game=None, user_id=None):
    """WHERE clause for the results of a server, optionally narrowed to a day, a game and/or a player."""
    clauses, params = ["guild_id = ?"], [guild_id]
    for column, value in (("day", day), ("game", game), ("user_id", user_id)):  # fixed column names
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    return " AND ".join(clauses), params


def count_results(guild_id, day=None, game=None, user_id=None):
    where, params = _result_filter(guild_id, day, game, user_id)
    with connect() as con:
        return con.execute(f"SELECT COUNT(*) FROM results WHERE {where}", params).fetchone()[0]


def clear_results(guild_id, day=None, game=None, user_id=None):
    """Delete the matching results and remember their messages, so reading the channel again (startup, /import)
    doesn't bring them back. Returns how many results were deleted."""
    where, params = _result_filter(guild_id, day, game, user_id)
    with connect() as con:
        con.execute(f"INSERT OR IGNORE INTO cleared_results SELECT guild_id, msg_id, game FROM results WHERE {where}",
                    params)
        return con.execute(f"DELETE FROM results WHERE {where}", params).rowcount


def player_name(guild_id, user_id):
    """The latest name stored for a player, or None."""
    with connect() as con:
        row = con.execute("SELECT name FROM results WHERE guild_id = ? AND user_id = ? ORDER BY rowid DESC LIMIT 1",
                          (guild_id, user_id)).fetchone()
    return row["name"] if row else None


def tag_channel(guild_id, channel_id, msg_ids):
    """Results saved before channels were stored get theirs once their message is seen in a channel."""
    with connect() as con:
        con.executemany("UPDATE results SET channel_id = ? WHERE guild_id = ? AND msg_id = ? AND channel_id IS NULL",
                        [(channel_id, guild_id, m) for m in msg_ids])


def forget_missing_results(guild_id, channel_id, seen_ids, oldest_id):
    """Delete the results of this channel whose message no longer exists (deleted while the bot was offline).
    Only messages at least as new as `oldest_id` are judged: older ones were not looked at, so they are left alone.
    Returns how many messages' results were removed."""
    seen = set(seen_ids)
    with connect() as con:
        stored = {r["msg_id"] for r in con.execute(
            "SELECT DISTINCT msg_id FROM results WHERE guild_id = ? AND channel_id = ? AND msg_id >= ?",
            (guild_id, channel_id, oldest_id))}
        gone = stored - seen
        con.executemany("DELETE FROM results WHERE guild_id = ? AND msg_id = ?", [(guild_id, m) for m in gone])
    return len(gone)


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


PERIODS = {"today": "Today", "week": "This week", "month": "This month", "all": "All time"}


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
        puzzle_day_ = min(r["day"] for r in group)  # normally the same for the whole group
        if since and puzzle_day_ < since:
            continue
        if only_day and puzzle_day_ != only_day:
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
    """The puzzles that belong to a day (a puzzle belongs to the day it was played, see puzzle_day)."""
    return {key: group for key, group in group_rows(rows).items() if min(r["day"] for r in group) == day}


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
    return date.fromisoformat(day).strftime("%a %d %b %Y")  # e.g. "Mon 05 Oct 2026"


TOP_N = 10  # players shown per ranking: a big server must never exceed Discord's embed limits
TODAY_TOP = 3  # /today lists every game, so it shows fewer players per game
AUTOPOST_TOP = 3  # the automatic daily message also lists every game, so it shows a podium per game
EMBED_TOTAL_LIMIT = 6000  # Discord rejects a message whose embeds hold more characters than this


def fit_to_discord(build, top=TOP_N):
    """build(top) -> embeds. Starts at `top` players per ranking and shows fewer until the message fits Discord's
    size limit."""
    for n in sorted({top, 5, 3, 1} & set(range(1, top + 1)), reverse=True):
        embeds = build(n)
        if sum(len(e) for e in embeds) <= EMBED_TOTAL_LIMIT:
            break
    return embeds


def ranking_line(position, row):
    return f"{medal(position)} **{row['name']}** — {row['display']}"


def add_day_fields(embed, groups, top=TOP_N, viewer_id=None):
    """One field per puzzle with its top players, in the order the games are listed.
    If `viewer_id` is outside the top, their own line is added so they can see where they stand."""
    for game, title in GAMES.items():
        same_game = [(key, group) for key, group in sorted(groups.items()) if key[0] == game]
        for _, group in same_game:
            ranked = rank_group(group)
            lines = [ranking_line(p, r) for p, r in ranked[:top]]
            if len(ranked) > top:
                lines.append(f"… and {len(ranked) - top} more")
                mine = next(((p, r) for p, r in ranked[top:] if r["user_id"] == viewer_id), None)
                if mine:
                    lines.append(f"{ranking_line(*mine)} (you)")
            # The day is in the message's title; the puzzle is named only when a game has two of them that day
            name = f"{title} {label(group[0])}" if len(same_game) > 1 else title
            embed.add_field(name=name, value="\n".join(lines)[:1024], inline=False)


def scoreboard_standings(table, names):
    """[(position, user_id, entry)], best first. Players with the same points share a position (0-based)."""
    ordered = sorted(table.items(), key=lambda kv: (-kv[1]["points"], names[kv[0]]))
    standings, position, previous = [], 0, None
    for i, (uid, entry) in enumerate(ordered):
        if entry["points"] != previous:
            position, previous = i, entry["points"]
        standings.append((position, uid, entry))
    return standings


def standing_line(position, name, entry):
    return f"{medal(position)} **{name}** — {entry['points']:g} / {entry['max']} pts"


def scoreboard_embed(rows, since, title, top=TOP_N):
    table, names = build_table(rows, since)
    embed = discord.Embed(title=title, colour=discord.Colour.gold())
    if not table:
        embed.description = "No results in this period yet."
        return embed
    standings = scoreboard_standings(table, names)
    lines = [standing_line(p, names[uid], entry) for p, uid, entry in standings[:top]]
    if len(standings) > top:
        lines.append(f"… and {len(standings) - top} more")
    embed.description = "\n".join(lines)
    embed.set_footer(text="Points = opponents beaten (tie = 0.5) / maximum possible")
    return embed


def my_ranking_embed(rows, since, title, user_id, around=5):
    """Where one player stands on the scoreboard: their position and the players just above and below them."""
    table, names = build_table(rows, since)
    standings = scoreboard_standings(table, names)
    embed = discord.Embed(title=title, colour=discord.Colour.gold())
    index = next((i for i, (_, uid, _) in enumerate(standings) if uid == user_id), None)
    if index is None:
        embed.description = "You have no results in this period yet."
        return embed
    position, _, entry = standings[index]
    start, end = max(0, index - around), min(len(standings), index + around + 1)
    lines = [standing_line(p, names[uid], e) + (" (you)" if uid == user_id else "")
             for p, uid, e in standings[start:end]]
    if start > 0:
        lines.insert(0, "…")
    if end < len(standings):
        lines.append("…")
    embed.description = (f"You are **#{position + 1}** of {len(standings)} with "
                         f"{entry['points']:g} / {entry['max']} pts.\n\n" + "\n".join(lines))
    embed.set_footer(text="Points = opponents beaten (tie = 0.5) / maximum possible")
    return embed


def today_embed(rows, day, viewer_id=None, top=TODAY_TOP):
    embed = discord.Embed(title=f"Today's results · {format_day(day)}", colour=discord.Colour.blurple())
    add_day_fields(embed, day_groups(rows, day), top, viewer_id)
    if not embed.fields:
        embed.description = "Nobody has submitted anything today yet."
    return embed


def day_winners(rows, day):
    """'Alice (3 pts)' or 'Alice & Bob (1.5 pts)' for a day's top scorer(s)."""
    table, names = build_table(rows, None, only_day=day)
    if not table:
        return "no results"
    best = max(t["points"] for t in table.values())
    winners = sorted(names[uid] for uid, t in table.items() if t["points"] == best)
    shown = " & ".join(winners[:3])
    if len(winners) > 3:
        shown += f" & {len(winners) - 3} more"
    return f"{shown} ({best:g} pts)"


RECORDS_TOP = 3  # best results shown per game in /records


def records_embed(rows, top=RECORDS_TOP):
    """The best results of each game, one line per result (the same player can appear more than once). Every line
    carries the day the puzzle was played, so all games are labelled the same way."""
    embed = discord.Embed(title="All-time records", colour=discord.Colour.green())
    for game, title in GAMES.items():
        results = sorted((r for r in rows if r["game"] == game), key=lambda r: (r["sort_key"], r["day"], r["name"]))
        if not results:
            continue
        lines, position, previous = [], 0, None
        for i, r in enumerate(results[:top]):
            if r["sort_key"] != previous:
                position, previous = i, r["sort_key"]
            lines.append(f"{medal(position)} **{r['name']}** — {r['display']} · {r['day']}")
        embed.add_field(name=title, value="\n".join(lines), inline=False)
    if not embed.fields:
        embed.description = "No results yet."
    return embed


def day_streaks(days, today):
    """(current, longest) run of consecutive days. A run that ended yesterday is still current: today isn't over."""
    ordered = sorted({date.fromisoformat(d) for d in days})
    longest = run = 0
    previous = None
    for d in ordered:
        run = run + 1 if previous is not None and d - previous == timedelta(days=1) else 1
        longest = max(longest, run)
        previous = d
    current = run if previous is not None and previous >= today - timedelta(days=1) else 0
    return current, longest


def player_stats(rows, user_id, today):
    """Numbers about one player, or None if they have no results."""
    mine = [r for r in rows if r["user_id"] == user_id]
    if not mine:
        return None
    contested = positions = wins = 0
    for group in group_rows(rows).values():
        if len(group) < 2:
            continue
        for position, r in rank_group(group):
            if r["user_id"] == user_id:
                contested += 1
                positions += position + 1
                wins += position == 0
    table, names = build_table(rows, None)
    standing = next(((p, entry) for p, uid, entry in scoreboard_standings(table, names) if uid == user_id), None)
    best = {}
    for r in mine:
        if r["game"] not in best or r["sort_key"] < best[r["game"]]["sort_key"]:
            best[r["game"]] = r
    current, longest = day_streaks([r["day"] for r in mine], today)
    return {
        "name": names[user_id], "puzzles": len(mine), "days": len({r["day"] for r in mine}),
        "current_streak": current, "longest_streak": longest,
        "contested": contested, "wins": wins,
        "average_position": positions / contested if contested else None,
        "standing": standing, "players": len(table), "best": best,
    }


def stats_embed(rows, user_id, today):
    stats = player_stats(rows, user_id, today)
    if stats is None:
        embed = discord.Embed(title="Stats", colour=discord.Colour.purple())
        embed.description = "No results for this player yet."
        return embed
    embed = discord.Embed(title=f"Stats · {stats['name']}", colour=discord.Colour.purple())
    embed.add_field(name="Played", value=f"{stats['puzzles']} puzzles on {stats['days']} days")
    embed.add_field(name="Streak", value=f"{stats['current_streak']} days (best {stats['longest_streak']})")
    if stats["contested"]:
        rate = stats["wins"] / stats["contested"]
        embed.add_field(name="Wins", value=f"{stats['wins']} of {stats['contested']} ({rate:.0%})")
        embed.add_field(name="Average position", value=f"{stats['average_position']:.1f}")
    position, entry = stats["standing"]
    embed.add_field(name="Scoreboard",
                    value=f"#{position + 1} of {stats['players']} · {entry['points']:g} / {entry['max']} pts")
    best = "\n".join(f"**{GAMES[g]}** {r['display']} · {r['day']}" for g, r in sorted(stats["best"].items(), key=lambda kv: list(GAMES).index(kv[0])) if g in GAMES)
    embed.add_field(name="Best results", value=best, inline=False)
    embed.set_footer(text="A shared first place counts as a win")
    return embed


def autopost_embeds(rows, days):
    """The message announcing one or several days: the ranking (or a summary) plus the all-time scoreboard."""
    def build(top):
        if len(days) == 1:
            first = discord.Embed(title=f"Results · {format_day(days[0])}", colour=discord.Colour.blurple())
            add_day_fields(first, day_groups(rows, days[0]), top)
        else:
            first = discord.Embed(title="Recent days", colour=discord.Colour.blurple(),
                                  description="\n".join(f"**{format_day(d)}** — {day_winners(rows, d)}"
                                                        for d in days))
            first.set_footer(text="Winner of each day, by points")
        scoreboard_top = TOP_N if top == AUTOPOST_TOP else top  # the scoreboard keeps its top 10 unless it must shrink
        return [first, scoreboard_embed(rows, None, "Scoreboard · All time", scoreboard_top)]

    return fit_to_discord(build, AUTOPOST_TOP)


# ----------------------------------------------------------------------------
# Welcome message and usage guide
# ----------------------------------------------------------------------------
def welcome_text(guild_name):
    """Sent when the bot joins a server. The bot can't know who invited it, so the wording is neutral."""
    return (f"Thanks for adding me to **{guild_name}**. I'm not active there yet: someone with the "
            "**Manage Server** permission needs to run `/settings channel` in the channel where you post "
            "your daily game results. Type `/help` to see what I can do.")


def help_embed(channel_id=None):
    """The usage guide, shown by /help and posted in the results channel once it is set."""
    embed = discord.Embed(title="Daily Games Scoreboard", colour=discord.Colour.blurple())
    games = ", ".join(GAMES.values())
    if channel_id:
        embed.description = f"Paste your daily game results in <#{channel_id}> and I'll work out who did best."
    else:
        embed.description = ("I'm not set up here yet. Someone with the **Manage Server** permission needs to run "
                             "`/settings channel` in the channel where you post your results.")
    embed.add_field(
        name="Posting results",
        value=(f"Supported games: {games}. Paste the game's share text in the results channel and I'll save it, "
               "reacting with ✅ (saved) or 🔁 (you already posted that puzzle). Edit your message to change a "
               "result, or delete it to remove it. Other chat is ignored."),
        inline=False,
    )
    embed.add_field(
        name="Scoring",
        value=("Every puzzle is a head-to-head: 1 point for each opponent you beat, 0.5 for a tie. Scoreboards show "
               "the top 10; with more players than that, a **My ranking** button appears under them to see where you "
               "stand (only you see the answer)."),
        inline=False,
    )
    embed.add_field(
        name="Commands",
        value=("`/today` today's results\n"
               "`/scoreboard` points leaderboard (today, week, month or all time)\n"
               "`/records` all-time best results (top 3 results of each game)\n"
               "`/stats` your stats, or those of another player (only you see them)\n"
               "`/import` scan the channel for results posted while I was offline\n"
               "`/settings` view or change this server's settings\n"
               "`/clear` delete saved results, all or just a day, game or player (needs Manage Server)\n"
               "`/help` show this guide"),
        inline=False,
    )
    embed.add_field(
        name="Automatic rankings",
        value=("When everyone has posted, or the day ends, I post the day's ranking and the all-time scoreboard "
               "by myself. This can be turned off with `/settings advanced autopost`."),
        inline=False,
    )
    embed.add_field(
        name="Settings",
        value=("Changing settings needs the **Manage Server** permission. `/settings channel` picks the results "
               "channel (required). Under `/settings advanced` are the timezone, the hour a new day starts, "
               "reactions and automatic rankings."),
        inline=False,
    )
    return embed


def fallback_channel(guild):
    """Where to say hello when the owner can't be messaged: the system channel, else the first channel we can write in."""
    def can_write(channel):
        perms = channel.permissions_for(guild.me)
        return perms.view_channel and perms.send_messages

    if guild.system_channel is not None and can_write(guild.system_channel):
        return guild.system_channel
    return next((c for c in guild.text_channels if can_write(c)), None)


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
        self.add_dynamic_items(MyRankingButton)  # makes the buttons on old messages work again after a restart
        if GUILD_IDS:  # instant sync, only in these servers
            for guild_id in GUILD_IDS:
                guild = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            # Commands synced globally by an earlier run (no GUILD_ID yet) would show up twice: remove them
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
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


class MyRankingButton(discord.ui.DynamicItem[discord.ui.Button], template=r"myrank:(?P<period>today|week|month|all)"):
    """'My ranking' under a scoreboard. The period is stored in the button's id, and the answer is worked out
    when it is pressed, so the button keeps working on old messages and after the bot restarts."""

    def __init__(self, period):
        super().__init__(discord.ui.Button(label="My ranking", style=discord.ButtonStyle.secondary,
                                           custom_id=f"myrank:{period}"))
        self.period = period

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["period"])

    async def callback(self, interaction):
        await send_my_ranking(interaction, self.period)


def ranking_view(period):
    view = discord.ui.View(timeout=None)
    view.add_item(MyRankingButton(period))
    return view


def ranking_buttons(rows, since, period):
    """The message arguments that attach the 'My ranking' button, only when the scoreboard hides some players."""
    table, _ = build_table(rows, since)
    return {"view": ranking_view(period)} if len(table) > TOP_N else {}


async def send_my_ranking(interaction, period):
    """Answer a button press privately: only the person who pressed it sees their position."""
    settings = get_settings(interaction.guild_id)
    since = period_start(period, current_day(settings))
    embed = my_ranking_embed(fetch_rows(interaction.guild_id), since, f"Your ranking · {PERIODS[period]}",
                             interaction.user.id)
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def send_welcome(guild):
    """Tell the server how to finish the setup: DM its owner, else a channel. Returns where it went (or None)."""
    text = welcome_text(guild.name)
    try:
        owner = await bot.fetch_user(guild.owner_id)
        await owner.send(text)
        return "owner"
    except discord.HTTPException:  # DMs closed, or the owner can't be fetched
        pass
    channel = fallback_channel(guild)
    if channel is not None:
        try:
            await channel.send(text)
            return "channel"
        except discord.HTTPException:
            pass
    log.warning("Joined '%s' but couldn't tell anyone how to set me up. Someone with Manage Server "
                "needs to run /settings channel.", guild.name)
    return None


async def post_guide(channel):
    """Post the usage guide in the results channel, once per channel. Returns True if it was posted."""
    if get_settings(channel.guild.id).guide_channel_id == channel.id:
        return False
    try:
        await channel.send(embed=help_embed(channel.id), allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        log.warning("Couldn't post the guide in #%s (missing permissions?)", channel.name)
        return False
    update_settings(channel.guild.id, guide_channel_id=channel.id)
    return True


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
                await channel.send(embeds=autopost_embeds(rows, announce), **ranking_buttons(rows, None, "all"),
                                   allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as error:
                if guild_id not in _autopost_warned:
                    _autopost_warned.add(guild_id)
                    log.warning("Can't post the daily ranking in #%s: %s %s", channel.name, error.status, error.text)
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
    """Scan recent history: import results posted while the bot was offline, and drop the results of messages
    that were deleted meanwhile. Returns how many messages were imported."""
    imported = 0
    seen = set()
    reactions = get_settings(channel.guild.id).reactions
    async for msg in channel.history(limit=limit):
        seen.add(msg.id)
        if msg.author.bot:
            continue
        new, _ = save_message(msg)
        if new:
            imported += 1
            if reactions:
                await react(msg, "✅")

    # An empty scan proves nothing: Discord also answers with no messages when the bot lacks Read Message History,
    # and that must never wipe a server's scoreboard. So only a scan that saw messages is used to clean up.
    if seen:
        # If it ran out of messages before reaching the limit it saw the whole channel, otherwise only the window
        # from the oldest message it saw.
        oldest = 0 if len(seen) < limit else min(seen)
        tag_channel(channel.guild.id, channel.id, seen)
        removed = forget_missing_results(channel.guild.id, channel.id, seen, oldest)
        if removed:
            log.info("Removed the results of %d message(s) deleted while the bot was offline (#%s)",
                     removed, channel.name)
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
        try:
            imported = await backfill_channel(channel, BACKFILL_LIMIT)
        except discord.HTTPException:
            log.warning("Couldn't read the history of #%s (%s). Is Read Message History missing?",
                        channel.name, channel.guild.name)
            continue
        log.info("Backfill: imported %d message(s) from #%s (%s)", imported, channel.name, channel.guild.name)

    autopost_loop.start()  # after the backfill, so results posted while offline are counted first


@bot.event
async def on_guild_join(guild):
    if get_settings(guild.id).channel_id is not None:  # re-invited to a server that is still set up
        return
    await send_welcome(guild)


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
    rows = fetch_rows(interaction.guild_id)
    [embed] = fit_to_discord(lambda top: [today_embed(rows, today, interaction.user.id, top)], TODAY_TOP)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="help", description="How the bot works and what it can do")
async def cmd_help(interaction: discord.Interaction):
    settings = get_settings(interaction.guild_id) if interaction.guild_id else None
    channel_id = settings.channel_id if settings else None
    await interaction.response.send_message(embed=help_embed(channel_id), ephemeral=True)


@bot.tree.command(name="scoreboard", description="Points leaderboard")
@app_commands.describe(period="Period to show (default: this week)")
@app_commands.choices(period=[app_commands.Choice(name=n, value=v) for v, n in PERIODS.items()])
async def cmd_scoreboard(interaction: discord.Interaction, period: str = "week"):
    settings = await require_setup(interaction)
    if settings is None:
        return
    since = period_start(period, current_day(settings))
    rows = fetch_rows(interaction.guild_id)
    embed = scoreboard_embed(rows, since, f"Scoreboard · {PERIODS[period]}")
    await interaction.response.send_message(embed=embed, **ranking_buttons(rows, since, period))


@bot.tree.command(name="records", description="All-time best results")
async def cmd_records(interaction: discord.Interaction):
    if await require_setup(interaction) is None:
        return
    await interaction.response.send_message(embed=records_embed(fetch_rows(interaction.guild_id)))


@bot.tree.command(name="stats", description="Stats of a player (only you see the reply)")
@app_commands.describe(player="Whose stats to show (empty = yours)")
async def cmd_stats(interaction: discord.Interaction, player: Optional[str] = None):
    settings = await require_setup(interaction)
    if settings is None:
        return
    if player is not None and not player.isdigit():
        await interaction.response.send_message("Pick the player from the list.", ephemeral=True)
        return
    user_id = int(player) if player is not None else interaction.user.id
    embed = stats_embed(fetch_rows(interaction.guild_id), user_id, current_day(settings))
    if player is None and not embed.fields:
        embed.description = "You have no results yet. Post one in the results channel."
    await interaction.response.send_message(embed=embed, ephemeral=True)


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
    await post_guide(target)
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


# ----------------------------------------------------------------------------
# /clear: delete saved results (needs Manage Server)
# ----------------------------------------------------------------------------
class ConfirmClear(discord.ui.View):
    """Delete / Cancel buttons shown (privately) before anything is removed."""

    def __init__(self, owner_id, guild_id, day, game, user_id):
        super().__init__(timeout=60)
        self.owner_id, self.guild_id = owner_id, guild_id
        self.day, self.game, self.user_id = day, game, user_id
        self.opened_with = None  # the interaction that showed these buttons, used to report a timeout

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only the person who ran the command can confirm.", ephemeral=True)
            return
        removed = clear_results(self.guild_id, self.day, self.game, self.user_id)
        self.stop()
        await interaction.response.edit_message(content=f"Deleted {removed} result(s).", view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        self.stop()
        await interaction.response.edit_message(content="Cancelled. Nothing was deleted.", view=None)

    async def on_timeout(self):
        if self.opened_with is not None:
            try:
                await self.opened_with.edit_original_response(content="Timed out. Nothing was deleted.", view=None)
            except discord.HTTPException:
                pass


def describe_clear(guild_id, day, game, user_id):
    who = (player_name(guild_id, user_id) or "unknown player") if user_id is not None else "all"
    return (f"**Day:** {format_day(day) if day else 'all'} · **Game:** {GAMES[game] if game else 'all'} · "
            f"**Player:** {who}")


@bot.tree.command(name="clear", description="Delete saved results: everything, or only a day, a game or a player")
@app_commands.guild_only()
@app_commands.describe(day="Only this day (empty = every day)", game="Only this game (empty = every game)",
                       player="Only this player (empty = every player)")
@app_commands.choices(game=[app_commands.Choice(name=title, value=key) for key, title in GAMES.items()])
async def cmd_clear(interaction: discord.Interaction, day: Optional[str] = None, game: Optional[str] = None,
                    player: Optional[str] = None):
    if not await require_manager(interaction):
        return
    if day is not None:
        try:
            date.fromisoformat(day)
        except ValueError:
            await interaction.response.send_message(
                f"`{day}` isn't a valid day. Pick one from the list, or write it as `YYYY-MM-DD`.", ephemeral=True)
            return
    user_id = None
    if player is not None:
        if not player.isdigit():
            await interaction.response.send_message("Pick the player from the list.", ephemeral=True)
            return
        user_id = int(player)
    count = count_results(interaction.guild_id, day, game, user_id)
    if count == 0:
        await interaction.response.send_message("Nothing to clear: no saved result matches.", ephemeral=True)
        return
    view = ConfirmClear(interaction.user.id, interaction.guild_id, day, game, user_id)
    view.opened_with = interaction
    await interaction.response.send_message(
        f"This will delete **{count}** saved result(s).\n{describe_clear(interaction.guild_id, day, game, user_id)}\n"
        "It can't be undone. The messages stay in the channel, but they won't be imported again.",
        view=view, ephemeral=True)


@cmd_clear.autocomplete("day")
async def clear_day_autocomplete(interaction: discord.Interaction, current: str):
    with connect() as con:
        days = [r["day"] for r in con.execute(
            "SELECT DISTINCT day FROM results WHERE guild_id = ? ORDER BY day DESC", (interaction.guild_id,))]
    wanted = current.strip().lower()
    choices = [app_commands.Choice(name=format_day(d), value=d)
               for d in days if wanted in f"{format_day(d)} {d}".lower()]
    return choices[:25]


@cmd_clear.autocomplete("player")
@cmd_stats.autocomplete("player")
async def player_autocomplete(interaction: discord.Interaction, current: str):
    names = {r["user_id"]: r["name"] for r in fetch_rows(interaction.guild_id)}  # the latest name wins
    wanted = current.strip().lower()
    matches = sorted((name, uid) for uid, name in names.items() if wanted in name.lower())
    return [app_commands.Choice(name=name[:100], value=str(uid)) for name, uid in matches[:25]]


if __name__ == "__main__":
    init_db()
    bot.run(TOKEN)