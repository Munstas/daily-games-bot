"""Tests for the parsers, scoring and database logic (no Discord connection needed)."""
import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord
import pytest

import bot

TZ = ZoneInfo("Europe/Lisbon")
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=TZ)

GREEN_GRID = "🟩🟩🟩🟩\n🟩🟩🟩🟩"
MISTAKE_GRID = "🟩🟩🟨🟩\n🟨🟩🟩🟩\n🟩🟨🟩🟩"  # 3 non-green squares


# ----------------------------------------------------------------------------
# Helpers that build realistic share texts
# ----------------------------------------------------------------------------
def fermi(no=68, score="3.79"):
    return (f"Fermi · No. {no}\n01  1.87×\n02  10.2×\n03  2.86×\n"
            f"─────────\n{score}× score · top 20%\n<https://fermi.gg/s/daily>")


def krillion(no=79, score=285):
    return f"Krillion #{no} 🦐\n{score}\n\n🫧🏮🐟🦑🐟🦑🫧"


def maptap(score=956, day="October 2", markdown=True):
    site = "[www.maptap.gg](https://www.maptap.gg)" if markdown else "www.maptap.gg"
    return f"{site} {day}\n96🏅 99🔥 97🔥 94🏅 95🏅\nFinal score: {score}"


def clues(when="in 05:09", grid=GREEN_GRID, date_part="Oct 2nd 2026 (Tricky)"):
    return f"I solved the daily #CluesBySam, {date_part}, {when}\n{grid}\nhttps://cluesbysam.com"


def sizeit(score=240):
    return (f"Size It Up\nOverall Score {score}\n\n⬜⬜⬜⬜⬜ 0\n🟥🟥🟥⬜⬜ 69\n⬜⬜⬜⬜⬜ 0\n🟥🟥🟥🟥⬜ 79\n"
            "🟥🟥🟥🟥🟥 92\nhttps://magnitudle.com/size-it-up")


def utc_noon(year, month, day):
    return datetime(year, month, day, 12, 0, tzinfo=timezone.utc)


def make_msg(msg_id, user_id, name, content, created_at=None, guild_id=1):
    """A minimal stand-in for discord.Message."""
    return SimpleNamespace(
        id=msg_id,
        content=content,
        guild=SimpleNamespace(id=guild_id),
        author=SimpleNamespace(id=user_id, display_name=name),
        created_at=created_at or utc_noon(2026, 10, 2),
        channel=SimpleNamespace(id=555),
    )


# ----------------------------------------------------------------------------
# Parsers: Fermi
# ----------------------------------------------------------------------------
def test_fermi_parses_puzzle_and_score():
    r = bot.parse_fermi(fermi(), NOW)
    assert (r.game, r.puzzle, r.sort_key, r.display) == ("fermi", "68", 3.79, "3.79×")


def test_fermi_lower_is_better():
    assert bot.parse_fermi(fermi(score="2.10"), NOW).sort_key < bot.parse_fermi(fermi(score="3.79"), NOW).sort_key


def test_fermi_accepts_comma_decimal():
    assert bot.parse_fermi(fermi(score="3,79"), NOW).sort_key == 3.79


# ----------------------------------------------------------------------------
# Parsers: Krillion
# ----------------------------------------------------------------------------
def test_krillion_parses_puzzle_and_score():
    r = bot.parse_krillion(krillion(), NOW)
    assert (r.game, r.puzzle, r.display) == ("krillion", "79", "285")


def test_krillion_higher_is_better():
    assert bot.parse_krillion(krillion(score=285), NOW).sort_key < bot.parse_krillion(krillion(score=100), NOW).sort_key


# ----------------------------------------------------------------------------
# Parsers: MapTap
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("markdown", [True, False])
def test_maptap_parses_date_and_score(markdown):
    r = bot.parse_maptap(maptap(markdown=markdown), NOW)
    assert (r.game, r.puzzle, r.display) == ("maptap", "2026-10-02", "956")


def test_maptap_higher_is_better():
    assert bot.parse_maptap(maptap(score=990), NOW).sort_key < bot.parse_maptap(maptap(score=900), NOW).sort_key


def test_maptap_year_rolls_back_around_new_year():
    # Shared on Jan 2nd 2027, but the puzzle was from "December 31"
    jan = datetime(2027, 1, 2, 12, 0, tzinfo=TZ)
    assert bot.parse_maptap(maptap(day="December 31"), jan).puzzle == "2026-12-31"


# ----------------------------------------------------------------------------
# Parsers: Clues by Sam
# ----------------------------------------------------------------------------
def test_clues_exact_time_all_green():
    r = bot.parse_clues(clues(), NOW)
    assert (r.game, r.puzzle, r.sort_key, r.display) == ("cluesbysam", "2026-10-02", 309, "05:09 · all green")


def test_clues_counts_non_green_squares():
    r = bot.parse_clues(clues(grid=MISTAKE_GRID), NOW)
    assert r.display == "05:09 · 3 non-green"
    assert r.sort_key == 3 * 1_000_000 + 309


def test_clues_hint_squares_count_as_non_green():
    # 🟡 = one hint, 🟠 = two hints. A hint square in the FIRST row used to break grid detection
    # and made the result look "all green".
    r = bot.parse_clues(clues(grid="🟩🟡🟩🟩\n🟩🟠🟩🟩\n🟩🟩🟩🟩"), NOW)
    assert r.display == "05:09 · 2 non-green"
    assert r.sort_key == 2 * 1_000_000 + 309


def test_clues_hint_is_worse_than_all_green_even_if_slower():
    green_slow = bot.parse_clues(clues(when="in 09:00"), NOW)
    hint_fast = bot.parse_clues(clues(when="in 01:00", grid="🟩🟡🟩🟩\n🟩🟩🟩🟩"), NOW)
    assert green_slow.sort_key < hint_fast.sort_key


def test_clues_less_than_11_minutes_means_10_xx():
    r = bot.parse_clues(clues(when="in less than 11 minutes"), NOW)
    assert r.display == "10:xx · all green"
    assert r.sort_key == 600


def test_clues_same_bucket_ties_and_later_bucket_is_slower():
    a = bot.parse_clues(clues(when="in less than 11 minutes"), NOW)
    b = bot.parse_clues(clues(when="in less than 11 minutes"), NOW)
    c = bot.parse_clues(clues(when="in less than 12 minutes"), NOW)
    assert a.sort_key == b.sort_key
    assert a.sort_key < c.sort_key


def test_clues_hours_are_supported():
    r = bot.parse_clues(clues(when="in 1:05:09"), NOW)
    assert r.display == "1:05:09 · all green"


def test_clues_all_green_slow_beats_mistakes_fast():
    slow_green = bot.parse_clues(clues(when="in less than 11 minutes"), NOW)
    fast_mistake = bot.parse_clues(clues(when="in 01:00", grid=MISTAKE_GRID), NOW)
    assert slow_green.sort_key < fast_mistake.sort_key


def test_clues_requires_the_grid():
    header_only = "I solved the daily #CluesBySam, Oct 2nd 2026 (Tricky), in 05:09\n\nhttps://cluesbysam.com"
    assert bot.parse_clues(header_only, NOW) is None


# ----------------------------------------------------------------------------
# extract_results: routing between parsers
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "",
    "bora jogar logo?",
    "Krillion is fun",
    "Wordle 1,234 4/6\n🟩🟨⬛⬛⬛",
    "🟩🟩🟩🟩",
])
def test_unrelated_messages_are_ignored(text):
    assert bot.extract_results(text, NOW) == []


def test_one_message_can_contain_several_games():
    found = bot.extract_results(fermi() + "\n\n" + krillion(), NOW)
    assert sorted(r.game for r in found) == ["fermi", "krillion"]


# ----------------------------------------------------------------------------
# Scoring
# ----------------------------------------------------------------------------
def players(*sort_keys):
    return [{"user_id": i, "sort_key": k} for i, k in enumerate(sort_keys)]


def test_winner_gets_one_point():
    assert bot.score_group(players(1, 2)) == {0: 1.0, 1: 0.0}


def test_tie_gives_half_a_point_each():
    assert bot.score_group(players(5, 5)) == {0: 0.5, 1: 0.5}


def test_four_players_with_a_tie():
    assert bot.score_group(players(1, 2, 2, 3)) == {0: 3.0, 1: 1.5, 2: 1.5, 3: 0.0}


def test_single_player_scores_nothing():
    assert bot.score_group(players(1)) == {0: 0.0}


@pytest.mark.parametrize("keys", [(1,), (1, 2), (1, 1), (1, 2, 3), (1, 1, 1), (1, 2, 2, 3), (1, 2, 3, 4, 5, 6)])
def test_total_points_do_not_depend_on_ties(keys):
    n = len(keys)
    assert sum(bot.score_group(players(*keys)).values()) == n * (n - 1) / 2


def test_ties_share_the_same_position():
    ranked = bot.rank_group(players(1, 2, 2, 3))
    assert [position for position, _ in ranked] == [0, 1, 1, 3]


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def test_period_start():
    thursday = date(2026, 10, 8)
    assert bot.period_start("today", thursday) == "2026-10-08"
    assert bot.period_start("week", thursday) == "2026-10-05"  # Monday
    assert bot.period_start("month", thursday) == "2026-10-01"
    assert bot.period_start("all", thursday) is None



# ----------------------------------------------------------------------------
# Database (each test gets its own temporary SQLite file)
# ----------------------------------------------------------------------------
@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "DB_PATH", str(tmp_path / "test.db"))
    bot.init_db()


def test_save_message_stores_the_result(db):
    assert bot.save_message(make_msg(1, 10, "Ana", fermi())) == (1, 0)
    [row] = bot.fetch_rows(1)
    assert (row["game"], row["puzzle"], row["user_id"], row["name"], row["display"]) == \
        ("fermi", "68", 10, "Ana", "3.79×")


def test_normal_chat_saves_nothing(db):
    assert bot.save_message(make_msg(1, 10, "Ana", "bora jogar?")) == (0, 0)
    assert bot.fetch_rows(1) == []


def test_same_puzzle_twice_is_rejected(db):
    assert bot.save_message(make_msg(1, 10, "Ana", fermi())) == (1, 0)
    assert bot.save_message(make_msg(2, 10, "Ana", fermi(score="1.00"))) == (0, 1)
    [row] = bot.fetch_rows(1)
    assert row["msg_id"] == 1  # the first submission stays


def test_same_puzzle_from_different_users_is_fine(db):
    bot.save_message(make_msg(1, 10, "Ana", fermi()))
    bot.save_message(make_msg(2, 20, "Bob", fermi(score="2.10")))
    assert len(bot.fetch_rows(1)) == 2


def test_one_message_with_two_games(db):
    assert bot.save_message(make_msg(1, 10, "Ana", fermi() + "\n\n" + krillion())) == (2, 0)


def test_delete_message_removes_its_results(db):
    bot.save_message(make_msg(1, 10, "Ana", fermi() + "\n\n" + krillion()))
    bot.delete_message(1)
    assert bot.fetch_rows(1) == []


def test_bulk_delete_removes_only_the_listed_messages(db):
    for i, (uid, name) in enumerate([(10, "Ana"), (20, "Bob"), (30, "Cris")], start=1):
        bot.save_message(make_msg(i, uid, name, fermi()))
    bot.delete_messages([1, 2])
    assert [r["user_id"] for r in bot.fetch_rows(1)] == [30]


def test_bulk_delete_event_handler(db):
    for i, (uid, name) in enumerate([(10, "Ana"), (20, "Bob"), (30, "Cris")], start=1):
        bot.save_message(make_msg(i, uid, name, fermi()))
    payload = SimpleNamespace(message_ids={1, 3, 999})  # unknown ids are simply ignored
    asyncio.run(bot.on_raw_bulk_message_delete(payload))
    assert [r["user_id"] for r in bot.fetch_rows(1)] == [20]



def test_editing_a_message_replaces_the_result(db):
    bot.save_message(make_msg(1, 10, "Ana", fermi(score="3.79")))
    # This is what on_message_edit does: delete what the message stored, then process it again
    bot.delete_message(1)
    bot.save_message(make_msg(1, 10, "Ana", fermi(score="2.00")))
    [row] = bot.fetch_rows(1)
    assert row["display"] == "2.00×"


def test_servers_are_kept_separate(db):
    bot.save_message(make_msg(1, 10, "Ana", fermi(), guild_id=1))
    bot.save_message(make_msg(2, 10, "Ana", fermi(), guild_id=2))
    assert len(bot.fetch_rows(1)) == 1
    assert len(bot.fetch_rows(2)) == 1


def test_day_uses_the_local_timezone(db):
    # 23:30 UTC on Oct 8th is 00:30 on Oct 9th in Lisbon (UTC+1 in October)
    late = datetime(2026, 10, 8, 23, 30, tzinfo=timezone.utc)
    bot.save_message(make_msg(1, 10, "Ana", fermi(), created_at=late))
    assert bot.fetch_rows(1)[0]["day"] == "2026-10-09"


def test_scoreboard_with_three_players(db):
    ana, bob, cris = (10, "Ana"), (20, "Bob"), (30, "Cris")
    texts = [
        (ana, fermi(score="3.79")), (bob, fermi(score="2.10")), (cris, fermi(score="9.5")),
        (ana, krillion(score=300)), (bob, krillion(score=285)), (cris, krillion(score=285)),
        (ana, clues(when="in 05:09", grid=MISTAKE_GRID)),
        (bob, clues(when="in 06:00")),
        (cris, clues(when="in 04:00")),
        (ana, maptap()),  # nobody to compare with: adds no points and no maximum
    ]
    for i, ((uid, name), text) in enumerate(texts):
        bot.save_message(make_msg(i, uid, name, text))

    table, names = bot.build_table(bot.fetch_rows(1), None)
    assert {names[u]: t["points"] for u, t in table.items()} == {"Bob": 3.5, "Ana": 3.0, "Cris": 2.5}
    assert all(t["max"] == 6 for t in table.values())


def test_scoreboard_period_filter(db):
    bot.save_message(make_msg(1, 10, "Ana", fermi(no=67, score="1.00"), created_at=utc_noon(2026, 10, 1)))
    bot.save_message(make_msg(2, 20, "Bob", fermi(no=67, score="2.00"), created_at=utc_noon(2026, 10, 1)))
    bot.save_message(make_msg(3, 10, "Ana", fermi(no=68, score="2.00"), created_at=utc_noon(2026, 10, 2)))
    bot.save_message(make_msg(4, 20, "Bob", fermi(no=68, score="1.00"), created_at=utc_noon(2026, 10, 2)))
    rows = bot.fetch_rows(1)

    everything, _ = bot.build_table(rows, None)
    assert everything[10]["points"] == 1.0 and everything[20]["points"] == 1.0

    only_day_2, _ = bot.build_table(rows, "2026-10-02")
    assert only_day_2[20]["points"] == 1.0 and only_day_2[10]["points"] == 0.0


# ----------------------------------------------------------------------------
# Server settings
# ----------------------------------------------------------------------------
def test_defaults_for_a_server_that_was_never_configured(db):
    s = bot.get_settings(999)
    assert (s.channel_id, s.timezone, s.day_rollover_hour, s.reactions) == (None, None, 0, True)
    assert (s.autopost, s.autopost_from, s.guide_channel_id) == (True, None, None)
    assert s.tz.key == bot.DEFAULT_TZ_NAME


def test_update_settings_changes_only_what_it_is_given(db):
    bot.update_settings(1, channel_id=555)
    bot.update_settings(1, timezone="Asia/Tokyo", reactions=False)
    s = bot.get_settings(1)
    assert (s.channel_id, s.timezone, s.day_rollover_hour, s.reactions) == (555, "Asia/Tokyo", 0, False)


def test_update_settings_rejects_unknown_fields(db):
    with pytest.raises(ValueError):
        bot.update_settings(1, nonsense=1)



def test_configured_channels_lists_only_finished_servers(db):
    bot.update_settings(1, channel_id=111)
    bot.update_settings(2, timezone="Europe/Madrid")  # no channel yet
    assert bot.configured_channels() == [(1, 111)]


def test_seed_channel_only_fills_an_empty_server(db):
    assert bot.seed_channel(1, 111) is True
    assert bot.seed_channel(1, 999) is False  # already configured: /settings channel wins over the .env
    assert bot.get_settings(1).channel_id == 111


def channel_msg(guild_id, channel_id):
    guild = SimpleNamespace(id=guild_id) if guild_id else None
    return SimpleNamespace(guild=guild, channel=SimpleNamespace(id=channel_id))


def test_nothing_is_read_until_the_channel_is_set(db):
    assert bot.is_valid_channel(channel_msg(1, 555)) is False


def test_only_the_configured_channel_is_read(db):
    bot.update_settings(1, channel_id=555)
    assert bot.is_valid_channel(channel_msg(1, 555)) is True
    assert bot.is_valid_channel(channel_msg(1, 556)) is False
    assert bot.is_valid_channel(channel_msg(2, 555)) is False  # another server that isn't set up


def test_direct_messages_are_ignored(db):
    assert bot.is_valid_channel(channel_msg(None, 555)) is False


def test_an_invalid_stored_timezone_falls_back_to_the_default(db):
    bot.update_settings(1, timezone="Not/AZone")
    assert bot.get_settings(1).tz.key == bot.DEFAULT_TZ_NAME


def test_parse_timezone():
    assert bot.parse_timezone("Europe/Lisbon") == "Europe/Lisbon"
    assert bot.parse_timezone("  America/New_York ") == "America/New_York"
    for bad in ["Nope/Nope", "", "../../etc/passwd", "lisbon"]:
        assert bot.parse_timezone(bad) is None


def test_game_day_rollover():
    def at(hour, minute):
        return datetime(2026, 10, 10, hour, minute, tzinfo=TZ)

    assert bot.game_day(at(1, 30), 0) == date(2026, 10, 10)
    assert bot.game_day(at(1, 30), 2) == date(2026, 10, 9)  # still the previous game day
    assert bot.game_day(at(2, 0), 2) == date(2026, 10, 10)  # the new day starts exactly at 02:00


def test_each_server_uses_its_own_timezone_for_the_day(db):
    late = datetime(2026, 10, 8, 23, 30, tzinfo=timezone.utc)
    bot.update_settings(1, timezone="Asia/Tokyo")  # 08:30 on Oct 9
    bot.update_settings(2, timezone="America/New_York")  # 19:30 on Oct 8
    bot.save_message(make_msg(1, 10, "Ana", fermi(), created_at=late, guild_id=1))
    bot.save_message(make_msg(2, 10, "Ana", fermi(), created_at=late, guild_id=2))
    assert bot.fetch_rows(1)[0]["day"] == "2026-10-09"
    assert bot.fetch_rows(2)[0]["day"] == "2026-10-08"


def test_day_rollover_moves_late_night_results_to_the_previous_day(db):
    bot.update_settings(1, timezone="Europe/Lisbon", day_rollover_hour=2)
    # 00:30 UTC on Oct 9 is 01:30 in Lisbon (UTC+1), before the 02:00 rollover
    created = datetime(2026, 10, 9, 0, 30, tzinfo=timezone.utc)
    bot.save_message(make_msg(1, 10, "Ana", fermi(), created_at=created))
    assert bot.fetch_rows(1)[0]["day"] == "2026-10-08"


def test_missing_permissions():
    enough = discord.Permissions(view_channel=True, send_messages=True, embed_links=True,
                                 read_message_history=True, add_reactions=True)
    assert bot.missing_permissions(enough) == []
    partial = discord.Permissions(view_channel=True, send_messages=True)
    assert bot.missing_permissions(partial) == ["Embed Links", "Read Message History", "Add Reactions"]


# ----------------------------------------------------------------------------
# Reactions and catching up on history (with fake Discord objects)
# ----------------------------------------------------------------------------
def reacting_msg(msg_id, content):
    msg = make_msg(msg_id, 10, "Ana", content)
    msg.author.bot = False
    msg.reactions_added = []

    async def add_reaction(emoji):
        msg.reactions_added.append(emoji)

    msg.add_reaction = add_reaction
    return msg


def test_reacts_with_a_check_mark_by_default(db):
    msg = reacting_msg(1, fermi())
    asyncio.run(bot.process_message(msg))
    assert msg.reactions_added == ["✅"]


def test_reactions_can_be_turned_off_and_the_result_is_still_saved(db):
    bot.update_settings(1, reactions=False)
    msg = reacting_msg(1, fermi())
    asyncio.run(bot.process_message(msg))
    assert msg.reactions_added == []
    assert len(bot.fetch_rows(1)) == 1


def test_duplicate_submission_gets_the_repeat_reaction(db):
    asyncio.run(bot.process_message(reacting_msg(1, fermi())))
    duplicate = reacting_msg(2, fermi())
    asyncio.run(bot.process_message(duplicate))
    assert duplicate.reactions_added == ["🔁"]


class FakeChannel:
    guild = SimpleNamespace(id=1)
    id = 555
    name = "results"

    def __init__(self, messages):
        self.messages = messages

    async def history(self, limit):
        for m in self.messages[:limit]:
            yield m


def test_backfill_imports_history_skips_bots_and_is_repeatable(db):
    from_a_bot = reacting_msg(3, fermi(no=70))
    from_a_bot.author.bot = True
    messages = [reacting_msg(1, fermi()), reacting_msg(2, krillion()), from_a_bot]

    assert asyncio.run(bot.backfill_channel(FakeChannel(messages), 10)) == 2
    assert len(bot.fetch_rows(1)) == 2
    assert messages[0].reactions_added == ["✅"]
    assert from_a_bot.reactions_added == []

    assert asyncio.run(bot.backfill_channel(FakeChannel(messages), 10)) == 0  # nothing new the second time


def test_command_tree_has_the_expected_commands():
    tree = bot.bot.tree
    assert {c.name for c in tree.get_commands()} == {"today", "scoreboard", "records", "import", "settings", "help", "clear", "stats"}
    settings = tree.get_command("settings")
    assert {c.name for c in settings.commands} == {"view", "channel", "advanced"}
    advanced = next(c for c in settings.commands if c.name == "advanced")
    assert {c.name for c in advanced.commands} == {"timezone", "rollover", "reactions", "autopost"}
    for command in tree.get_commands():  # what would be sent to Discord must serialise without errors
        command.to_dict(tree)


def test_parse_guild_ids():
    assert bot.parse_guild_ids(None) == []
    assert bot.parse_guild_ids("") == []
    assert bot.parse_guild_ids("123") == [123]
    assert bot.parse_guild_ids("123, 456;789  ") == [123, 456, 789]


# ----------------------------------------------------------------------------
# Automatic daily rankings
# ----------------------------------------------------------------------------
TODAY = date(2026, 10, 10)  # a Saturday
_msg_ids = iter(range(10_000, 20_000))


def day(n):
    """October 2026, day n."""
    return date(2026, 10, n)


def add(user_id, name, game, on, guild_id=1, score=None):
    """Store one result for `game` posted on the day `on` (a date)."""
    number = 100 + on.day  # a different puzzle each day
    if game == "fermi":
        text = fermi(no=number, score=score or "3.00")
    else:
        text = krillion(no=number, score=int(score or 200))
    created = datetime(on.year, on.month, on.day, 12, 0, tzinfo=timezone.utc)
    bot.save_message(make_msg(next(_msg_ids), user_id, name, text, created_at=created, guild_id=guild_id))


def rows(guild_id=1):
    return bot.fetch_rows(guild_id)


ANA, BOB = (10, "Ana"), (20, "Bob")


def test_old_databases_get_the_new_settings_columns(tmp_path, monkeypatch):
    import sqlite3
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE guild_settings (guild_id INTEGER PRIMARY KEY, channel_id INTEGER, timezone TEXT, "
                "day_rollover_hour INTEGER NOT NULL DEFAULT 0, reactions INTEGER NOT NULL DEFAULT 1)")
    con.execute("INSERT INTO guild_settings (guild_id, channel_id) VALUES (1, 555)")
    con.commit()
    con.close()
    monkeypatch.setattr(bot, "DB_PATH", path)
    bot.init_db()
    s = bot.get_settings(1)
    assert (s.channel_id, s.autopost, s.autopost_from, s.guide_channel_id) == (555, True, None, None)


def test_autopost_settings_are_saved_per_server(db):
    bot.update_settings(1, autopost=False, autopost_from="2026-10-01")
    assert (bot.get_settings(1).autopost, bot.get_settings(1).autopost_from) == (False, "2026-10-01")
    assert bot.get_settings(2).autopost is True


# --- has_comparison -----------------------------------------------------------
def test_a_day_needs_two_players_on_the_same_puzzle_to_have_a_ranking(db):
    add(*ANA, "fermi", day(9))
    add(*BOB, "krillion", day(9))  # different games: nothing to compare
    assert bot.has_comparison(rows(), "2026-10-09") is False
    add(*BOB, "fermi", day(9))
    assert bot.has_comparison(rows(), "2026-10-09") is True


# --- day_is_complete ----------------------------------------------------------
def test_without_history_nothing_is_expected_so_a_day_is_never_complete(db):
    add(*ANA, "fermi", day(9))
    add(*BOB, "fermi", day(9))
    assert bot.day_is_complete(rows(), "2026-10-09") is False


def test_a_day_is_complete_when_everyone_from_the_week_before_has_posted(db):
    for who in (ANA, BOB):
        add(*who, "fermi", day(8))
    add(*ANA, "fermi", day(9))
    assert bot.day_is_complete(rows(), "2026-10-09") is False  # Bob is still missing
    add(*BOB, "fermi", day(9))
    assert bot.day_is_complete(rows(), "2026-10-09") is True


def test_each_player_is_expected_to_post_the_games_they_usually_play(db):
    add(*ANA, "fermi", day(8))
    add(*ANA, "krillion", day(8))
    add(*BOB, "fermi", day(8))  # Bob doesn't play Krillion
    add(*ANA, "fermi", day(9))
    add(*BOB, "fermi", day(9))
    assert bot.day_is_complete(rows(), "2026-10-09") is False  # Ana's Krillion is missing
    add(*ANA, "krillion", day(9))
    assert bot.day_is_complete(rows(), "2026-10-09") is True  # Bob is not expected to play Krillion


def test_habits_older_than_a_week_are_forgotten(db):
    add(*ANA, "fermi", day(1))  # 8 days before the 9th
    add(*BOB, "fermi", day(1))
    add(*ANA, "fermi", day(8))
    add(*ANA, "fermi", day(9))
    add(*BOB, "krillion", day(9))
    # Only Ana played in the 7 days before the 9th, and she has posted
    assert bot.day_is_complete(rows(), "2026-10-09") is True


# --- days_to_post -------------------------------------------------------------
def ready(since="2026-10-01", posted=(), today=TODAY, guild_id=1):
    return bot.days_to_post(rows(guild_id), set(posted), today, since)


def test_a_day_that_has_ended_is_ready_even_if_someone_is_missing(db):
    add(*ANA, "fermi", day(8))
    add(*BOB, "fermi", day(8))
    add(*ANA, "fermi", day(9))
    add(*BOB, "fermi", day(9))
    add(*ANA, "krillion", day(9))  # Ana plays two games, Bob only one
    assert ready() == ["2026-10-08", "2026-10-09"]


def test_today_waits_until_everyone_has_posted(db):
    for who in (ANA, BOB):
        add(*who, "fermi", day(9))
    add(*ANA, "fermi", day(10))
    assert ready(posted=["2026-10-09"]) == []
    add(*BOB, "fermi", day(10))
    assert ready(posted=["2026-10-09"]) == ["2026-10-10"]


def test_today_is_not_ready_when_there_is_nobody_to_compare_with(db):
    add(*ANA, "fermi", day(9))
    add(*ANA, "fermi", day(10))
    assert ready(posted=["2026-10-09"]) == []  # complete, but Ana played alone


def test_days_already_posted_are_not_returned(db):
    for who in (ANA, BOB):
        add(*who, "fermi", day(9))
    assert ready(posted=["2026-10-09"]) == []


def test_days_before_the_activation_date_are_never_returned(db):
    for who in (ANA, BOB):
        add(*who, "fermi", day(8))
        add(*who, "fermi", day(9))
    assert ready(since="2026-10-09") == ["2026-10-09"]


def test_days_older_than_the_window_are_never_returned(db):
    for who in (ANA, BOB):
        add(*who, "fermi", day(2))  # 8 days ago
        add(*who, "fermi", day(3))  # 7 days ago: still inside the window
    assert ready() == ["2026-10-03"]


def test_future_days_are_not_returned(db):
    add(*ANA, "fermi", day(11))
    assert ready() == []


# --- posted_days --------------------------------------------------------------
def test_posted_days_are_remembered_per_server_and_marking_twice_is_harmless(db):
    bot.mark_posted(1, ["2026-10-08", "2026-10-09"])
    bot.mark_posted(1, ["2026-10-09"])
    assert bot.posted_days(1) == {"2026-10-08", "2026-10-09"}
    assert bot.posted_days(2) == set()


# --- What the messages look like ---------------------------------------------

def test_day_winners_lists_ties_and_empty_days(db):
    add(*ANA, "fermi", day(9), score="1.00")
    add(*BOB, "fermi", day(9), score="2.00")
    add(*ANA, "fermi", day(8), score="1.00")
    add(*BOB, "fermi", day(8), score="1.00")
    assert bot.day_winners(rows(), "2026-10-09") == "Ana (1 pts)"
    assert bot.day_winners(rows(), "2026-10-08") == "Ana & Bob (0.5 pts)"
    assert bot.day_winners(rows(), "2026-10-07") == "no results"


def test_one_day_shows_its_ranking_and_the_total_scoreboard(db):
    add(*ANA, "fermi", day(9), score="1.00")
    add(*BOB, "fermi", day(9), score="2.00")
    first, total = bot.autopost_embeds(rows(), ["2026-10-09"])
    assert first.title == "Results · Fri 09 Oct 2026"
    assert [f.name for f in first.fields] == ["Fermi"]
    assert first.fields[0].value.splitlines()[0].startswith("🥇 **Ana**")
    assert total.title == "Scoreboard · All time"
    assert "**Ana** — 1 / 1 pts" in total.description


def test_several_days_show_a_summary_instead_of_every_ranking(db):
    for n, (a, b) in {8: ("1.00", "2.00"), 9: ("2.00", "1.00")}.items():
        add(*ANA, "fermi", day(n), score=a)
        add(*BOB, "fermi", day(n), score=b)
    first, total = bot.autopost_embeds(rows(), ["2026-10-08", "2026-10-09"])
    assert first.title == "Recent days"
    assert first.description.splitlines() == ["**Thu 08 Oct 2026** — Ana (1 pts)", "**Fri 09 Oct 2026** — Bob (1 pts)"]
    assert not first.fields
    assert total.title == "Scoreboard · All time"


# --- late posts keep their own day -------------------------------------------
def post(msg_id, user_id, name, text, y, m, d):
    bot.save_message(make_msg(msg_id, user_id, name, text, created_at=utc_noon(y, m, d)))


def test_a_late_dated_puzzle_stays_on_its_own_day(db):
    post(1, 10, "Ana", maptap(956, "October 8"), 2026, 10, 8)
    post(2, 20, "Bob", maptap(900, "October 8"), 2026, 10, 9)  # yesterday's puzzle, posted today
    assert {r["day"] for r in rows()} == {"2026-10-08"}
    assert bot.today_embed(rows(), "2026-10-09").fields == []
    assert bot.has_comparison(rows(), "2026-10-09") is False


def test_a_late_post_still_counts_in_the_all_time_scoreboard(db):
    post(1, 10, "Ana", maptap(956, "October 8"), 2026, 10, 8)
    post(2, 20, "Bob", maptap(900, "October 8"), 2026, 10, 9)
    assert "**Ana** — 1 / 1 pts" in bot.scoreboard_embed(rows(), None, "All time").description


def test_a_numbered_puzzle_takes_the_first_day_it_was_posted(db):
    add(*ANA, "fermi", day(8))
    bot.save_message(make_msg(900, 20, "Bob", fermi(no=108, score="2.00"), created_at=utc_noon(2026, 10, 9)))
    bot.save_message(make_msg(901, 10, "Ana", fermi(no=108, score="1.00"), created_at=utc_noon(2026, 10, 8)))
    assert {r["day"] for r in rows() if r["puzzle"] == "108"} == {"2026-10-08"}


def test_out_of_order_messages_move_the_siblings_to_the_earliest_day(db):
    bot.save_message(make_msg(1, 20, "Bob", fermi(no=50), created_at=utc_noon(2026, 10, 9)))
    bot.save_message(make_msg(2, 10, "Ana", fermi(no=50), created_at=utc_noon(2026, 10, 8)))
    assert {r["day"] for r in rows()} == {"2026-10-08"}


def test_a_late_post_does_not_complete_todays_day(db):
    add(*ANA, "fermi", day(8))
    add(*ANA, "fermi", day(9))
    post(5, 20, "Bob", maptap(900, "October 8"), 2026, 10, 9)
    assert bot.day_is_complete(rows(), "2026-10-09") is False


def test_existing_databases_get_their_days_normalized(db):
    post(1, 10, "Ana", maptap(956, "October 8"), 2026, 10, 8)
    with bot.connect() as con:
        con.execute("INSERT INTO results VALUES (1,'maptap','2026-10-08',20,'Bob',-900,'900',2,'2026-10-09',555)")
        bot.normalize_days(con)
    assert {r["day"] for r in rows()} == {"2026-10-08"}


def test_field_names_show_the_puzzle_only_when_a_game_has_two_that_day(db):
    add(*ANA, "fermi", day(9))
    add(*BOB, "fermi", day(9))
    bot.save_message(make_msg(800, 10, "Ana", fermi(no=5), created_at=utc_noon(2026, 10, 9)))
    bot.save_message(make_msg(801, 20, "Bob", fermi(no=5, score="9.00"), created_at=utc_noon(2026, 10, 9)))
    names = [f.name for f in bot.today_embed(rows(), "2026-10-09").fields]
    assert "Fermi #5" in names and "Fermi #109" in names


# --- /today title, /records and /stats --------------------------------------

def test_records_show_the_top_3_results_and_a_player_can_repeat(db):
    add(*ANA, "fermi", day(9), score="1.00")
    add(*ANA, "fermi", day(8), score="2.00")
    add(*ANA, "fermi", day(7), score="3.00")
    add(*BOB, "fermi", day(9), score="4.00")
    [field] = bot.records_embed(rows()).fields
    lines = field.value.splitlines()
    assert [l.split(" — ")[0] for l in lines] == ["🥇 **Ana**", "🥈 **Ana**", "🥉 **Ana**"]
    assert [l.rsplit(" · ", 1)[1] for l in lines] == ["2026-10-09", "2026-10-08", "2026-10-07"]



def test_records_with_ties_share_the_position(db):
    add(*ANA, "fermi", day(9), score="1.00")
    add(*BOB, "fermi", day(9), score="1.00")
    [field] = bot.records_embed(rows()).fields
    assert [l.split()[0] for l in field.value.splitlines()] == ["🥇", "🥇"]



def test_streaks():
    today = date(2026, 10, 12)
    assert bot.day_streaks(["2026-10-10", "2026-10-11", "2026-10-12"], today) == (3, 3)
    assert bot.day_streaks(["2026-10-10", "2026-10-11"], today) == (2, 2)  # today isn't over yet
    assert bot.day_streaks(["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-11"], today) == (1, 3)
    assert bot.day_streaks(["2026-10-05"], today) == (0, 1)
    assert bot.day_streaks([], today) == (0, 0)


def test_player_stats(db):
    add(*ANA, "fermi", day(8), score="1.00")
    add(*BOB, "fermi", day(8), score="2.00")
    add(*ANA, "fermi", day(9), score="3.00")
    add(*BOB, "fermi", day(9), score="2.00")
    add(*ANA, "krillion", day(9))  # alone: not contested
    s = bot.player_stats(rows(), 10, date(2026, 10, 9))
    assert (s["puzzles"], s["days"], s["current_streak"], s["longest_streak"]) == (3, 2, 2, 2)
    assert (s["contested"], s["wins"], s["average_position"]) == (2, 1, 1.5)
    assert s["standing"][0] == 0 and s["players"] == 2
    assert s["best"]["fermi"]["display"] == "1.00×" and s["best"]["krillion"]["day"] == "2026-10-09"


def test_stats_embed(db):
    add(*ANA, "fermi", day(9), score="1.00")
    add(*BOB, "fermi", day(9), score="2.00")
    embed = bot.stats_embed(rows(), 10, date(2026, 10, 9))
    fields = {f.name: f.value for f in embed.fields}
    assert embed.title == "Stats · Ana"
    assert fields["Wins"] == "1 of 1 (100%)"
    assert fields["Scoreboard"] == "#1 of 2 · 1 / 1 pts"
    assert fields["Best results"] == "**Fermi** 1.00× · 2026-10-09"


def test_stats_of_a_player_without_results(db):
    add(*ANA, "fermi", day(9))
    embed = bot.stats_embed(rows(), 999, date(2026, 10, 9))
    assert not embed.fields and embed.description == "No results for this player yet."


def test_a_solo_player_has_stats_without_wins_and_average(db):
    add(*ANA, "fermi", day(9))
    names = [f.name for f in bot.stats_embed(rows(), 10, date(2026, 10, 9)).fields]
    assert "Wins" not in names and "Average position" not in names and "Best results" in names


# --- autopost() with a fake channel -------------------------------------------
class SendChannel:
    name = "results"

    def __init__(self):
        self.sent = []
        self.fail = False

    async def send(self, **kwargs):
        if self.fail:
            raise discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")
        self.sent.append(kwargs)


@pytest.fixture
def channel(db, monkeypatch):
    fake = SendChannel()
    monkeypatch.setattr(bot.bot, "get_channel", lambda channel_id: fake)
    monkeypatch.setattr(bot, "current_day", lambda settings: TODAY)
    bot._autopost_locks.clear()
    bot._autopost_warned.clear()
    bot.update_settings(1, channel_id=555, autopost_from="2026-10-01")
    return fake


def run_autopost(guild_id=1):
    asyncio.run(bot.autopost(guild_id))


def two_players_on(n):
    add(*ANA, "fermi", day(n), score="1.00")
    add(*BOB, "fermi", day(n), score="2.00")


def test_the_first_run_only_activates_and_never_announces_old_history(channel):
    bot.update_settings(1, autopost_from=None)
    two_players_on(9)
    run_autopost()
    assert channel.sent == []
    assert bot.get_settings(1).autopost_from == "2026-10-10"
    assert bot.posted_days(1) == set()


def test_a_finished_day_is_announced_once(channel):
    two_players_on(9)
    run_autopost()
    run_autopost()
    assert len(channel.sent) == 1
    first, total = channel.sent[0]["embeds"]
    assert (first.title, total.title) == ("Results · Fri 09 Oct 2026", "Scoreboard · All time")
    assert bot.posted_days(1) == {"2026-10-09"}


def test_several_missed_days_are_announced_in_a_single_message(channel):
    two_players_on(8)
    two_players_on(9)
    run_autopost()
    assert len(channel.sent) == 1
    assert channel.sent[0]["embeds"][0].title == "Recent days"
    assert bot.posted_days(1) == {"2026-10-08", "2026-10-09"}


def test_mentions_are_never_pinged(channel):
    two_players_on(9)
    run_autopost()
    assert channel.sent[0]["allowed_mentions"].users is False


def test_today_is_announced_as_soon_as_everyone_has_posted(channel):
    two_players_on(9)
    bot.mark_posted(1, ["2026-10-09"])
    add(*ANA, "fermi", day(10), score="1.00")
    run_autopost()
    assert channel.sent == []
    add(*BOB, "fermi", day(10), score="2.00")
    run_autopost()
    assert [m["embeds"][0].title for m in channel.sent] == ["Results · Sat 10 Oct 2026"]


def test_nothing_is_posted_when_automatic_rankings_are_off(channel):
    bot.update_settings(1, autopost=False)
    two_players_on(9)
    run_autopost()
    assert channel.sent == [] and bot.posted_days(1) == set()


def test_nothing_is_posted_in_a_server_without_a_channel(channel):
    bot.update_settings(2, autopost_from="2026-10-01")
    two_players_on(9)
    add(*ANA, "fermi", day(9), guild_id=2)
    add(*BOB, "fermi", day(9), guild_id=2)
    run_autopost(2)
    assert channel.sent == []


def test_a_day_nobody_can_be_compared_on_is_skipped_quietly(channel):
    add(*ANA, "fermi", day(9))
    run_autopost()
    assert channel.sent == []
    assert bot.posted_days(1) == {"2026-10-09"}  # closed, so it is not examined again


def test_a_failed_send_is_retried_later(channel):
    two_players_on(9)
    channel.fail = True
    run_autopost()
    assert bot.posted_days(1) == set()
    channel.fail = False
    run_autopost()
    assert len(channel.sent) == 1 and bot.posted_days(1) == {"2026-10-09"}


def test_a_missing_channel_is_not_marked_as_posted(channel, monkeypatch):
    two_players_on(9)
    monkeypatch.setattr(bot.bot, "get_channel", lambda channel_id: None)
    run_autopost()
    assert bot.posted_days(1) == set()


def test_servers_are_announced_independently(channel):
    two_players_on(9)
    bot.update_settings(2, channel_id=666, autopost_from="2026-10-01")
    run_autopost(2)
    assert channel.sent == [] and bot.posted_days(2) == set()  # server 2 has no results of its own


def test_an_error_while_announcing_does_not_crash_the_bot(channel, monkeypatch):
    def boom(guild_id):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(bot, "fetch_rows", boom)
    asyncio.run(bot.autopost_safely(1))  # must not raise


# --- /settings advanced autopost ----------------------------------------------
class FakeInteraction:
    def __init__(self, manager=True, guild_id=1):
        self.guild_id = guild_id
        self.user = SimpleNamespace(guild_permissions=SimpleNamespace(manage_guild=manager))
        self.replies = []
        self.response = SimpleNamespace(send_message=self._reply)

    async def _reply(self, text, ephemeral=False):
        self.replies.append((text, ephemeral))


def run_autopost_command(interaction, enabled):
    asyncio.run(bot.cmd_settings_autopost.callback(interaction, enabled))


def test_turning_autopost_back_on_does_not_announce_the_days_it_was_off(channel):
    bot.update_settings(1, autopost=False, autopost_from="2026-10-01")
    run_autopost_command(FakeInteraction(), True)
    s = bot.get_settings(1)
    assert (s.autopost, s.autopost_from) == (True, "2026-10-10")


def test_turning_autopost_off_keeps_the_activation_date(channel):
    run_autopost_command(FakeInteraction(), False)
    s = bot.get_settings(1)
    assert (s.autopost, s.autopost_from) == (False, "2026-10-01")



def test_only_managers_can_change_autopost(channel):
    interaction = FakeInteraction(manager=False)
    run_autopost_command(interaction, False)
    assert bot.get_settings(1).autopost is True
    assert interaction.replies and "Manage Server" in interaction.replies[0][0]


# ----------------------------------------------------------------------------
# Welcome message, usage guide and /help
# ----------------------------------------------------------------------------
def forbidden():
    return discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Cannot send messages to this user")


class Sink:
    """Something messages can be sent to (a user or a channel)."""

    def __init__(self, can_send=True, name="channel"):
        self.name = name
        self.can_send = can_send
        self.messages = []

    async def send(self, text=None, **kwargs):
        if not self.can_send:
            raise forbidden()
        self.messages.append(text if text is not None else kwargs)


def writable(sink, allowed=True):
    sink.permissions_for = lambda member: SimpleNamespace(view_channel=allowed, send_messages=allowed)
    return sink


def make_guild(owner=None, system=None, channels=(), guild_id=1):
    return SimpleNamespace(id=guild_id, name="Test Server", owner_id=99, me=object(),
                           system_channel=system, text_channels=list(channels))


@pytest.fixture
def owner(monkeypatch):
    user = Sink(name="owner")

    async def fetch_user(user_id):
        assert user_id == 99
        return user

    monkeypatch.setattr(bot.bot, "fetch_user", fetch_user)
    return user



def test_the_owner_gets_the_welcome_by_direct_message(owner):
    channel = writable(Sink())
    assert asyncio.run(bot.send_welcome(make_guild(system=channel))) == "owner"
    assert owner.messages == [bot.welcome_text("Test Server")]
    assert channel.messages == []


def test_with_closed_direct_messages_it_goes_to_the_system_channel(owner):
    owner.can_send = False
    system, other = writable(Sink()), writable(Sink())
    assert asyncio.run(bot.send_welcome(make_guild(system=system, channels=[other]))) == "channel"
    assert system.messages == [bot.welcome_text("Test Server")] and other.messages == []


def test_without_a_usable_system_channel_it_uses_the_first_channel_it_can_write_in(owner):
    owner.can_send = False
    system = writable(Sink(), allowed=False)
    hidden, first, second = writable(Sink(), allowed=False), writable(Sink()), writable(Sink())
    guild = make_guild(system=system, channels=[hidden, first, second])
    assert asyncio.run(bot.send_welcome(guild)) == "channel"
    assert first.messages and not second.messages and not hidden.messages and not system.messages


def test_when_nobody_can_be_told_it_only_logs_and_never_crashes(owner, caplog):
    owner.can_send = False
    assert asyncio.run(bot.send_welcome(make_guild(system=None, channels=[]))) is None
    broken = writable(Sink(can_send=False))  # looks writable but the send fails
    assert asyncio.run(bot.send_welcome(make_guild(system=broken))) is None
    assert "Manage Server" in caplog.text



def test_joining_a_new_server_sends_the_welcome(db, owner):
    asyncio.run(bot.on_guild_join(make_guild()))
    assert owner.messages == [bot.welcome_text("Test Server")]


def test_rejoining_a_server_that_is_still_set_up_stays_quiet(db, owner):
    bot.update_settings(1, channel_id=555)
    asyncio.run(bot.on_guild_join(make_guild()))
    assert owner.messages == []


# --- the guide posted in the results channel -----------------------------------
class GuideChannel(Sink):
    def __init__(self, channel_id, can_send=True):
        super().__init__(can_send=can_send, name=f"results-{channel_id}")
        self.id = channel_id
        self.guild = SimpleNamespace(id=1)


def test_the_guide_is_posted_once_per_channel(db):
    first = GuideChannel(555)
    assert asyncio.run(bot.post_guide(first)) is True
    assert asyncio.run(bot.post_guide(first)) is False  # running /settings channel again changes nothing
    assert len(first.messages) == 1
    assert first.messages[0]["embed"].title == "Daily Games Scoreboard"
    assert bot.get_settings(1).guide_channel_id == 555

    moved = GuideChannel(556)
    assert asyncio.run(bot.post_guide(moved)) is True  # a new channel gets its own guide
    assert len(moved.messages) == 1


def test_a_guide_that_could_not_be_posted_is_tried_again_next_time(db):
    channel = GuideChannel(555, can_send=False)
    assert asyncio.run(bot.post_guide(channel)) is False
    assert bot.get_settings(1).guide_channel_id is None
    channel.can_send = True
    assert asyncio.run(bot.post_guide(channel)) is True



def test_the_env_shortcut_does_not_trigger_a_guide(db):
    bot.seed_channel(1, 555)
    assert asyncio.run(bot.post_guide(GuideChannel(555))) is False



def test_the_guide_mentions_every_command_the_bot_has():
    commands_field = next(f for f in bot.help_embed().fields if f.name == "Commands")
    for command in bot.bot.tree.get_commands():
        assert f"`/{command.name}`" in commands_field.value



def test_the_guide_fits_discords_embed_limits():
    embed = bot.help_embed(555)
    assert len(embed) <= 6000 and all(len(f.value) <= 1024 for f in embed.fields)


def test_help_command_replies_privately_with_the_guide(db):
    bot.update_settings(1, channel_id=555)
    sent = {}

    async def send_message(**kwargs):
        sent.update(kwargs)

    interaction = SimpleNamespace(guild_id=1, response=SimpleNamespace(send_message=send_message))
    asyncio.run(bot.cmd_help.callback(interaction))
    assert sent["ephemeral"] is True and "<#555>" in sent["embed"].description


# ----------------------------------------------------------------------------
# Big servers: rankings are limited to the top players
# ----------------------------------------------------------------------------
def crowd(n, game="fermi", on=day(9)):
    """n players with all-different results, player 1 being the best."""
    for i in range(1, n + 1):
        add(i, f"Player {i}", game, on, score=f"{1 + i / 100:.2f}" if game == "fermi" else str(1000 - i))


def full_day(n, on=day(9)):
    """n players who played all four games on the same day."""
    created = datetime(on.year, on.month, on.day, 12, 0, tzinfo=timezone.utc)
    for i in range(1, n + 1):
        texts = [fermi(no=70, score=f"{1 + i / 100:.2f}"), krillion(no=80, score=1000 - i),
                 maptap(score=1000 - i, day="October 9"),
                 clues(when=f"in {i % 9 + 1:02d}:{i % 60:02d}", date_part="Oct 9th 2026 (Tricky)")]
        for text in texts:
            bot.save_message(make_msg(next(_msg_ids), i, f"Player {i}", text, created_at=created))


def test_players_with_the_same_points_share_a_scoreboard_position():
    table = {1: {"points": 3.0, "max": 3}, 2: {"points": 2.0, "max": 3},
             3: {"points": 2.0, "max": 3}, 4: {"points": 0.0, "max": 3}}
    names = {1: "A", 2: "B", 3: "C", 4: "D"}
    assert [p for p, _, _ in bot.scoreboard_standings(table, names)] == [0, 1, 1, 3]



def test_the_scoreboard_shows_the_top_players_and_how_many_are_hidden(db):
    crowd(13)
    lines = bot.scoreboard_embed(rows(), None, "Scoreboard").description.splitlines()
    assert len(lines) == bot.TOP_N + 1
    assert "**Player 1**" in lines[0] and "**Player 10**" in lines[9]
    assert lines[-1] == "… and 3 more"



def day_field(embed, title):
    return next(f.value for f in embed.fields if f.name.startswith(title))



def test_the_player_who_asks_sees_their_own_position_below_the_top(db):
    crowd(13)
    lines = day_field(bot.today_embed(rows(), "2026-10-09", viewer_id=12), "Fermi").splitlines()
    assert len(lines) == bot.TODAY_TOP + 2
    assert lines[-2] == "… and 10 more"
    assert lines[-1] == "12. **Player 12** — 1.12× (you)"


def test_a_viewer_inside_the_top_or_without_a_result_gets_no_extra_line(db):
    crowd(13)
    for viewer in (3, 999):
        lines = day_field(bot.today_embed(rows(), "2026-10-09", viewer_id=viewer), "Fermi").splitlines()
        assert len(lines) == bot.TODAY_TOP + 1 and not any("(you)" in line for line in lines)


def test_today_shows_only_the_top_three_of_each_game(db):
    crowd(13)
    lines = day_field(bot.today_embed(rows(), "2026-10-09"), "Fermi").splitlines()
    assert [l.split()[1] for l in lines[:3]] == ["**Player", "**Player", "**Player"]
    assert "**Player 3**" in lines[2] and lines[3] == "… and 10 more"



def test_fit_to_discord_shows_fewer_players_until_the_message_fits():
    def build(top):
        return [discord.Embed(description="x" * (top * 1000))]

    [embed] = bot.fit_to_discord(build)
    assert len(embed.description) == 5000  # 10 players would be 10000 characters, 5 fit


def test_a_server_with_a_hundred_players_gets_a_message_discord_accepts(db):
    full_day(100)
    embeds = bot.autopost_embeds(rows(), ["2026-10-09"])
    assert sum(len(e) for e in embeds) <= 6000
    assert all(len(f.value) <= 1024 for f in embeds[0].fields)
    assert len(embeds[1].description) <= 4096
    assert "… and 90 more" in embeds[1].description


def test_the_automatic_message_shows_a_podium_per_game_but_the_top_10_scoreboard(db):
    crowd(13)
    first, scoreboard = bot.autopost_embeds(rows(), ["2026-10-09"])
    assert len(day_field(first, "Fermi").splitlines()) == bot.AUTOPOST_TOP + 1  # 3 players and "… and 10 more"
    assert len(scoreboard.description.splitlines()) == bot.TOP_N + 1  # 10 players and "… and 3 more"


def test_a_long_summary_of_missed_days_still_fits(db):
    for n in range(3, 10):
        full_day(100, on=day(n))
    embeds = bot.autopost_embeds(rows(), [f"2026-10-{n:02d}" for n in range(3, 10)])
    assert embeds[0].title == "Recent days" and sum(len(e) for e in embeds) <= 6000


def test_many_players_tied_for_the_win_are_abbreviated(db):
    for i in range(1, 6):
        add(i, f"P{i}", "fermi", day(9), score="2.00")
    assert bot.day_winners(rows(), "2026-10-09") == "P1 & P2 & P3 & 2 more (2 pts)"


def test_today_command_shows_the_top_and_the_askers_own_position(db, monkeypatch):
    bot.update_settings(1, channel_id=555)
    crowd(13, on=TODAY)
    sent = {}

    async def send_message(**kwargs):
        sent.update(kwargs)

    interaction = SimpleNamespace(guild_id=1, user=SimpleNamespace(id=12),
                                  response=SimpleNamespace(send_message=send_message))
    monkeypatch.setattr(bot, "current_day", lambda settings: TODAY)
    asyncio.run(bot.cmd_today.callback(interaction))
    assert "(you)" in day_field(sent["embed"], "Fermi")


def test_a_failed_announcement_logs_what_discord_said(channel, caplog):
    two_players_on(9)
    channel.fail = True
    run_autopost()
    assert "403" in caplog.text and "Missing Permissions" in caplog.text


# ----------------------------------------------------------------------------
# "My ranking" button
# ----------------------------------------------------------------------------
class PressedInteraction:
    """What the bot sees when someone presses a button or runs a command."""

    def __init__(self, user_id=1, guild_id=1):
        self.guild_id = guild_id
        self.user = SimpleNamespace(id=user_id)
        self.sent = {}
        self.response = SimpleNamespace(send_message=self._send)

    async def _send(self, **kwargs):
        self.sent.update(kwargs)


def my_ranking(user_id, since=None):
    return bot.my_ranking_embed(rows(), since, "Your ranking", user_id).description


def test_my_ranking_shows_the_position_and_the_players_around_it(db):
    crowd(30)
    text = my_ranking(15)
    assert text.startswith("You are **#15** of 30 with ")
    lines = text.split("\n\n", 1)[1].splitlines()
    assert lines[0] == "…" and lines[-1] == "…"
    assert [l for l in lines if "(you)" in l] == [l for l in lines if "**Player 15**" in l]
    assert "**Player 10**" in lines[1] and "**Player 20**" in lines[-2]
    assert "**Player 9**" not in text and "**Player 21**" not in text


def test_my_ranking_at_the_top_has_no_dots_above(db):
    crowd(30)
    lines = my_ranking(1).split("\n\n", 1)[1].splitlines()
    assert lines[0].startswith("🥇 **Player 1**") and lines[0].endswith("(you)")
    assert lines[-1] == "…" and len(lines) == 7  # you and the 5 below, then the dots


def test_my_ranking_at_the_bottom_has_no_dots_below(db):
    crowd(30)
    lines = my_ranking(30).split("\n\n", 1)[1].splitlines()
    assert lines[0] == "…" and lines[-1].endswith("(you)") and len(lines) == 7



def test_my_ranking_without_results_says_so(db):
    crowd(3)
    assert my_ranking(999) == "You have no results in this period yet."


def test_my_ranking_only_counts_the_chosen_period(db):
    add(*ANA, "fermi", day(1))  # old
    add(*BOB, "fermi", day(1))
    add(*BOB, "krillion", day(9))
    add(5, "Cris", "krillion", day(9))
    assert my_ranking(ANA[0], since="2026-10-09") == "You have no results in this period yet."
    assert my_ranking(ANA[0], since=None).startswith("You are **#")


def test_pressing_the_button_answers_privately_with_the_presser_position(db, monkeypatch):
    monkeypatch.setattr(bot, "current_day", lambda settings: TODAY)
    crowd(30)
    interaction = PressedInteraction(user_id=12)
    asyncio.run(bot.MyRankingButton("all").callback(interaction))
    assert interaction.sent["ephemeral"] is True
    assert interaction.sent["embed"].title == "Your ranking · All time"
    assert interaction.sent["embed"].description.startswith("You are **#12** of 30")



def test_the_button_never_expires():
    assert bot.ranking_view("all").timeout is None  # a persistent view, so it still works after a restart


def test_the_bot_registers_the_button_on_startup(monkeypatch):
    registered, synced = [], []

    async def fake_sync(**kwargs):
        synced.append(kwargs)

    monkeypatch.setattr(bot, "GUILD_IDS", [])
    monkeypatch.setattr(bot.bot, "add_dynamic_items", lambda *items: registered.extend(items))
    monkeypatch.setattr(bot.bot.tree, "sync", fake_sync)
    asyncio.run(bot.bot.setup_hook())
    assert registered == [bot.MyRankingButton]


def test_with_guild_ids_the_commands_are_synced_there_and_removed_globally(monkeypatch):
    synced = []

    async def fake_sync(guild=None):
        synced.append((guild.id if guild else None, len(bot.bot.tree.get_commands(guild=guild))))

    monkeypatch.setattr(bot, "GUILD_IDS", [111, 222])
    monkeypatch.setattr(bot.bot, "add_dynamic_items", lambda *items: None)
    monkeypatch.setattr(bot.bot.tree, "sync", fake_sync)
    all_global = list(bot.bot.tree.get_commands())
    try:
        asyncio.run(bot.bot.setup_hook())
        assert [g for g, _ in synced] == [111, 222, None]
        assert synced[0][1] == synced[1][1] == len(all_global) and synced[2][1] == 0
    finally:
        for command in all_global:
            bot.bot.tree.add_command(command, override=True)


def test_the_scoreboard_command_comes_with_the_button_for_its_period(db, monkeypatch):
    monkeypatch.setattr(bot, "current_day", lambda settings: TODAY)
    bot.update_settings(1, channel_id=555)
    crowd(bot.TOP_N + 1)
    for chosen, expected in [(None, "week"), ("month", "month"), ("all", "all")]:
        interaction = PressedInteraction()
        asyncio.run(bot.cmd_scoreboard.callback(interaction, *([chosen] if chosen else [])))
        assert interaction.sent["view"].children[0].custom_id == f"myrank:{expected}"


def test_the_automatic_message_comes_with_the_all_time_button(channel):
    crowd(bot.TOP_N + 1)
    run_autopost()
    assert channel.sent[0]["view"].children[0].custom_id == "myrank:all"


def test_the_button_only_appears_when_the_scoreboard_hides_players(db, monkeypatch):
    monkeypatch.setattr(bot, "current_day", lambda settings: TODAY)
    bot.update_settings(1, channel_id=555)
    crowd(bot.TOP_N)  # everybody fits in the top 10
    interaction = PressedInteraction()
    asyncio.run(bot.cmd_scoreboard.callback(interaction))
    assert "view" not in interaction.sent
    add(99, "One more", "fermi", day(9))
    interaction = PressedInteraction()
    asyncio.run(bot.cmd_scoreboard.callback(interaction))
    assert "view" in interaction.sent



def test_the_button_counts_players_of_the_period_it_belongs_to(db):
    crowd(bot.TOP_N + 1, on=day(1))  # lots of players, but only last month
    add(*ANA, "fermi", day(9))
    add(*BOB, "fermi", day(9))
    assert bot.ranking_buttons(rows(), None, "all") != {}
    assert bot.ranking_buttons(rows(), "2026-10-09", "week") == {}



# ----------------------------------------------------------------------------
# /clear
# ----------------------------------------------------------------------------
def three_players_two_days(guild_id=1):
    """Ana, Bob and Cris on days 8 and 9, each with Fermi and Krillion."""
    for n in (8, 9):
        for user, name in ((10, "Ana"), (20, "Bob"), (30, "Cris")):
            add(user, name, "fermi", day(n), guild_id=guild_id)
            add(user, name, "krillion", day(n), guild_id=guild_id)


def test_count_results_filters_by_day_game_and_player(db):
    three_players_two_days()
    assert bot.count_results(1) == 12
    assert bot.count_results(1, day="2026-10-09") == 6
    assert bot.count_results(1, game="fermi") == 6
    assert bot.count_results(1, user_id=10) == 4
    assert bot.count_results(1, day="2026-10-09", game="fermi", user_id=10) == 1  # filters combine
    assert bot.count_results(1, day="2026-10-01") == 0
    assert bot.count_results(2) == 0


def test_clearing_removes_only_what_matches(db):
    three_players_two_days()
    assert bot.clear_results(1, day="2026-10-09", user_id=10) == 2
    assert bot.count_results(1) == 10
    assert bot.count_results(1, day="2026-10-08", user_id=10) == 2  # her other day is untouched



def test_clearing_one_server_never_touches_another(db):
    three_players_two_days(guild_id=1)
    three_players_two_days(guild_id=2)
    bot.clear_results(1)
    assert bot.count_results(1) == 0 and bot.count_results(2) == 12


def test_a_cleared_message_is_not_imported_again_when_the_channel_is_read(db):
    messages = [reacting_msg(1, fermi()), reacting_msg(2, krillion())]
    asyncio.run(bot.backfill_channel(FakeChannel(messages), 10))
    bot.clear_results(1)
    assert asyncio.run(bot.backfill_channel(FakeChannel(messages), 10)) == 0
    assert bot.fetch_rows(1) == []


def test_clearing_one_game_keeps_the_other_results_of_the_same_message(db):
    both = reacting_msg(1, fermi() + "\n" + krillion())
    asyncio.run(bot.backfill_channel(FakeChannel([both]), 10))
    assert len(bot.fetch_rows(1)) == 2
    bot.clear_results(1, game="fermi")
    asyncio.run(bot.backfill_channel(FakeChannel([both]), 10))
    assert [r["game"] for r in bot.fetch_rows(1)] == ["krillion"]  # Krillion stays, Fermi doesn't come back


def test_a_new_message_with_the_same_puzzle_is_saved_after_a_clear(db):
    bot.save_message(make_msg(1, 10, "Ana", fermi()))
    bot.clear_results(1)
    assert bot.save_message(make_msg(2, 10, "Ana", fermi())) == (1, 0)  # she posts it again, as a new message


def test_a_cleared_message_stays_cleared_even_if_it_is_edited(db):
    bot.save_message(make_msg(1, 10, "Ana", fermi()))
    bot.clear_results(1)
    assert bot.save_message(make_msg(1, 10, "Ana", fermi(score="1.00"))) == (0, 0)
    assert bot.fetch_rows(1) == []



def test_player_name_is_the_latest_one(db):
    bot.save_message(make_msg(1, 10, "Ana", fermi(no=1)))
    bot.save_message(make_msg(2, 10, "Ana B.", fermi(no=2)))
    assert bot.player_name(1, 10) == "Ana B." and bot.player_name(1, 99) is None


# --- the command ----------------------------------------------------------------
class ClearInteraction:
    def __init__(self, user_id=1, manager=True, guild_id=1):
        self.guild_id = guild_id
        self.user = SimpleNamespace(id=user_id, guild_permissions=SimpleNamespace(manage_guild=manager))
        self.sent = {}
        self.edited = {}
        self.response = SimpleNamespace(send_message=self._send, edit_message=self._edit)

    async def _send(self, content=None, **kwargs):
        self.sent = {"content": content, **kwargs}

    async def _edit(self, **kwargs):
        self.edited = kwargs


def run_clear(interaction, **filters):
    asyncio.run(bot.cmd_clear.callback(interaction, **filters))


def test_only_managers_can_clear(db):
    three_players_two_days()
    interaction = ClearInteraction(manager=False)
    run_clear(interaction)
    assert "Manage Server" in interaction.sent["content"] and "view" not in interaction.sent
    assert bot.count_results(1) == 12


def test_clear_asks_for_confirmation_before_deleting_anything(db):
    three_players_two_days()
    interaction = ClearInteraction()
    run_clear(interaction, day="2026-10-09", game="fermi", player="10")
    assert bot.count_results(1) == 12  # nothing happened yet
    text = interaction.sent["content"]
    assert "**1**" in text and "Fri 09 Oct 2026" in text and "Fermi" in text and "Ana" in text
    assert interaction.sent["ephemeral"] is True and interaction.sent["view"] is not None



def test_pressing_delete_removes_the_results(db):
    three_players_two_days()
    interaction = ClearInteraction()
    run_clear(interaction, game="krillion")
    delete_button = interaction.sent["view"].children[0]
    assert delete_button.label == "Delete"
    pressed = ClearInteraction()
    asyncio.run(delete_button.callback(pressed))
    assert pressed.edited["content"] == "Deleted 6 result(s)."
    assert bot.count_results(1) == 6 and bot.count_results(1, game="krillion") == 0


def test_pressing_cancel_deletes_nothing(db):
    three_players_two_days()
    interaction = ClearInteraction()
    run_clear(interaction)
    cancel_button = interaction.sent["view"].children[1]
    pressed = ClearInteraction()
    asyncio.run(cancel_button.callback(pressed))
    assert "Cancelled" in pressed.edited["content"] and bot.count_results(1) == 12


def test_somebody_else_cannot_press_delete(db):
    three_players_two_days()
    interaction = ClearInteraction(user_id=1)
    run_clear(interaction)
    stranger = ClearInteraction(user_id=2)
    asyncio.run(interaction.sent["view"].children[0].callback(stranger))
    assert bot.count_results(1) == 12 and stranger.edited == {}


def test_clear_with_nothing_to_delete_says_so_without_buttons(db):
    three_players_two_days()
    interaction = ClearInteraction()
    run_clear(interaction, day="2026-10-01")
    assert "Nothing to clear" in interaction.sent["content"] and "view" not in interaction.sent


def test_clear_rejects_a_malformed_day_or_player(db):
    three_players_two_days()
    for filters in ({"day": "yesterday"}, {"day": "2026-13-45"}, {"player": "Ana"}):
        interaction = ClearInteraction()
        run_clear(interaction, **filters)
        assert "view" not in interaction.sent and bot.count_results(1) == 12


def test_a_confirmation_that_times_out_reports_it(db):
    three_players_two_days()
    replies = []

    async def edit_original_response(**kwargs):
        replies.append(kwargs)

    interaction = ClearInteraction()
    interaction.edit_original_response = edit_original_response
    run_clear(interaction)
    asyncio.run(interaction.sent["view"].on_timeout())
    assert "Timed out" in replies[0]["content"] and bot.count_results(1) == 12


def test_day_autocomplete_lists_the_days_that_have_results(db):
    three_players_two_days()
    interaction = SimpleNamespace(guild_id=1)
    choices = asyncio.run(bot.clear_day_autocomplete(interaction, ""))
    assert [c.value for c in choices] == ["2026-10-09", "2026-10-08"]  # newest first
    assert choices[0].name == "Fri 09 Oct 2026"
    assert [c.value for c in asyncio.run(bot.clear_day_autocomplete(interaction, "08"))] == ["2026-10-08"]
    assert asyncio.run(bot.clear_day_autocomplete(SimpleNamespace(guild_id=2), "")) == []


def test_player_autocomplete_lists_the_players_of_this_server(db):
    three_players_two_days()
    add(77, "Other server", "fermi", day(9), guild_id=2)
    interaction = SimpleNamespace(guild_id=1)
    choices = asyncio.run(bot.player_autocomplete(interaction, ""))
    assert [(c.name, c.value) for c in choices] == [("Ana", "10"), ("Bob", "20"), ("Cris", "30")]
    assert [c.name for c in asyncio.run(bot.player_autocomplete(interaction, "BO"))] == ["Bob"]


# ----------------------------------------------------------------------------
# Results of messages deleted while the bot was offline
# ----------------------------------------------------------------------------
def scanned(*messages, channel_id=555):
    channel = FakeChannel(list(messages))
    channel.id = channel_id
    return channel


def stored_message_ids():
    return sorted({r["msg_id"] for r in bot.fetch_rows(1)})


def test_results_are_remembered_with_their_channel(db):
    bot.save_message(make_msg(1, 10, "Ana", fermi()))
    assert bot.fetch_rows(1)[0]["channel_id"] == 555


def test_a_message_deleted_while_offline_loses_its_result_on_the_next_scan(db):
    m1, m2, m3 = (reacting_msg(i, fermi(no=60 + i)) for i in (1, 2, 3))
    asyncio.run(bot.backfill_channel(scanned(m3, m2, m1), 10))
    assert stored_message_ids() == [1, 2, 3]
    asyncio.run(bot.backfill_channel(scanned(m3, m1), 10))  # message 2 was deleted
    assert stored_message_ids() == [1, 3]


def test_an_empty_scan_removes_nothing(db):
    # Discord also returns no messages when the bot lacks Read Message History. That must not wipe the scoreboard,
    # so a purged channel is cleaned with /clear instead.
    asyncio.run(bot.backfill_channel(scanned(reacting_msg(1, fermi()), reacting_msg(2, krillion())), 10))
    asyncio.run(bot.backfill_channel(scanned(), 10))
    assert stored_message_ids() == [1, 2]


def test_a_channel_down_to_a_single_message_is_still_cleaned(db):
    m1, m2 = reacting_msg(1, fermi()), reacting_msg(2, krillion())
    asyncio.run(bot.backfill_channel(scanned(m2, m1), 10))
    asyncio.run(bot.backfill_channel(scanned(m2), 10))
    assert stored_message_ids() == [2]


def test_messages_older_than_the_scanned_window_are_left_alone(db):
    m1, m2, m3 = (reacting_msg(i, fermi(no=60 + i)) for i in (1, 2, 3))
    asyncio.run(bot.backfill_channel(scanned(m3, m2, m1), 10))
    # Message 1 is gone, but this scan only looks at the 2 newest messages: it can't tell, so it keeps the result
    asyncio.run(bot.backfill_channel(scanned(m3, m2), 2))
    assert stored_message_ids() == [1, 2, 3]
    # A scan that does reach the start of the channel removes it
    asyncio.run(bot.backfill_channel(scanned(m3, m2), 10))
    assert stored_message_ids() == [2, 3]


def test_a_deleted_message_inside_the_window_is_removed_even_with_a_small_limit(db):
    m1, m2, m3, m4 = (reacting_msg(i, fermi(no=60 + i)) for i in (1, 2, 3, 4))
    asyncio.run(bot.backfill_channel(scanned(m4, m3, m2, m1), 10))
    asyncio.run(bot.backfill_channel(scanned(m4, m2, m1), 3))  # window = messages 2 to 4, and 3 is missing
    assert stored_message_ids() == [1, 2, 4]


def test_results_from_another_channel_are_not_touched(db):
    old_channel_message = reacting_msg(1, fermi())
    old_channel_message.channel = SimpleNamespace(id=999)  # the server used to read another channel
    bot.save_message(old_channel_message)
    asyncio.run(bot.backfill_channel(scanned(reacting_msg(2, krillion())), 10))
    assert stored_message_ids() == [1, 2]



def test_results_saved_before_channels_were_stored_get_tagged_when_their_message_is_seen(db):
    bot.save_message(make_msg(1, 10, "Ana", fermi()))
    bot.save_message(make_msg(2, 10, "Ana", krillion()))
    with bot.connect() as con:
        con.execute("UPDATE results SET channel_id = NULL")  # what an older database looks like
    asyncio.run(bot.backfill_channel(scanned(reacting_msg(2, krillion())), 10))
    by_message = {r["msg_id"]: r["channel_id"] for r in bot.fetch_rows(1)}
    assert by_message == {1: None, 2: 555}  # message 1 wasn't seen: nothing is known, nothing is removed


def test_bot_messages_in_the_channel_do_not_get_in_the_way(db):
    from_a_bot = reacting_msg(2, "Here is today's ranking")
    from_a_bot.author.bot = True
    result = reacting_msg(1, fermi())
    asyncio.run(bot.backfill_channel(scanned(from_a_bot, result), 10))
    asyncio.run(bot.backfill_channel(scanned(from_a_bot, result), 10))
    assert stored_message_ids() == [1]


def test_a_failed_scan_removes_nothing(db):
    class BrokenChannel(FakeChannel):
        async def history(self, limit):
            yield reacting_msg(5, krillion(no=5))
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Access")

    bot.save_message(make_msg(1, 10, "Ana", fermi()))
    with pytest.raises(discord.Forbidden):
        asyncio.run(bot.backfill_channel(BrokenChannel([]), 10))
    assert 1 in stored_message_ids()


def test_the_database_of_an_older_version_gets_the_new_columns_and_table(tmp_path, monkeypatch):
    import sqlite3
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE results (guild_id INTEGER NOT NULL, game TEXT NOT NULL, puzzle TEXT NOT NULL, "
                "user_id INTEGER NOT NULL, name TEXT NOT NULL, sort_key REAL NOT NULL, display TEXT NOT NULL, "
                "msg_id INTEGER NOT NULL, day TEXT NOT NULL, PRIMARY KEY (guild_id, game, puzzle, user_id))")
    con.execute("INSERT INTO results VALUES (1, 'fermi', '68', 10, 'Ana', 3.79, '3.79×', 1, '2026-10-02')")
    con.commit()
    con.close()
    monkeypatch.setattr(bot, "DB_PATH", path)
    bot.init_db()
    [row] = bot.fetch_rows(1)
    assert row["name"] == "Ana" and row["channel_id"] is None
    assert bot.clear_results(1) == 1 and bot.save_message(make_msg(1, 10, "Ana", fermi())) == (0, 0)


def test_stats_list_the_games_in_the_same_order_for_everyone(db):
    add(*ANA, "krillion", day(9))
    add(*ANA, "fermi", day(9))
    add(*BOB, "fermi", day(9))
    add(*BOB, "krillion", day(9))
    for uid in (10, 20):
        best = [f for f in bot.stats_embed(rows(), uid, date(2026, 10, 9)).fields if f.name == "Best results"][0]
        assert [l.split("**")[1] for l in best.value.splitlines()] == ["Fermi", "Krillion"]



# --- Size It Up: no puzzle number or date in the share text -----------------------
def test_size_it_up_parses_the_overall_score_higher_is_better():
    r = bot.parse_sizeitup(sizeit(240), NOW)
    assert (r.game, r.display) == ("sizeitup", "240")
    assert bot.parse_sizeitup(sizeit(300), NOW).sort_key < r.sort_key
    assert bot.parse_sizeitup("Overall Score 240", NOW) is None  # needs the game's title too


def test_size_it_up_takes_the_game_day_of_the_message_as_its_puzzle(db):
    bot.update_settings(1, timezone="Europe/Lisbon", day_rollover_hour=2)
    # 00:30 in Lisbon on Oct 9th still belongs to Oct 8th with a 02:00 rollover
    created = datetime(2026, 10, 8, 23, 30, tzinfo=timezone.utc)
    bot.save_message(make_msg(1, 10, "Ana", sizeit(), created_at=created))
    [row] = rows()
    assert (row["puzzle"], row["day"]) == ("2026-10-08", "2026-10-08")


def test_size_it_up_twice_on_the_same_day_keeps_the_first(db):
    bot.save_message(make_msg(1, 10, "Ana", sizeit(240)))
    assert bot.save_message(make_msg(2, 10, "Ana", sizeit(300))) == (0, 1)