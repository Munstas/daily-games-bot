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


def test_label():
    assert bot.label({"puzzle": "68"}) == "#68"
    assert bot.label({"puzzle": "2026-10-02"}) == "2026-10-02"


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


def test_deleting_everything_empties_scoreboard_and_records(db):
    for i, (uid, name, score) in enumerate([(10, "Ana", "3.79"), (20, "Bob", "2.10")], start=1):
        bot.save_message(make_msg(i, uid, name, fermi(score=score)))
    bot.delete_messages([1, 2])
    rows = bot.fetch_rows(1)
    assert rows == []
    assert bot.build_table(rows, None) == ({}, {})


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


def test_settings_are_per_server(db):
    bot.update_settings(1, channel_id=111)
    bot.update_settings(2, channel_id=222, reactions=False)
    assert bot.get_settings(1).channel_id == 111 and bot.get_settings(1).reactions is True
    assert bot.get_settings(2).channel_id == 222 and bot.get_settings(2).reactions is False


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
    assert {c.name for c in tree.get_commands()} == {"today", "scoreboard", "records", "import", "settings", "help"}
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
def test_format_day():
    assert bot.format_day("2026-10-09") == "Fri 09 Oct"


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
    assert first.title == "Results · Fri 09 Oct"
    assert [f.name for f in first.fields] == ["Fermi #109"]
    assert first.fields[0].value.splitlines()[0].startswith("🥇 **Ana**")
    assert total.title == "Scoreboard · All time"
    assert "**Ana** — 1 / 1 pts" in total.description


def test_several_days_show_a_summary_instead_of_every_ranking(db):
    for n, (a, b) in {8: ("1.00", "2.00"), 9: ("2.00", "1.00")}.items():
        add(*ANA, "fermi", day(n), score=a)
        add(*BOB, "fermi", day(n), score=b)
    first, total = bot.autopost_embeds(rows(), ["2026-10-08", "2026-10-09"])
    assert first.title == "Recent days"
    assert first.description.splitlines() == ["**Thu 08 Oct** — Ana (1 pts)", "**Fri 09 Oct** — Bob (1 pts)"]
    assert not first.fields
    assert total.title == "Scoreboard · All time"


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
    assert (first.title, total.title) == ("Results · Fri 09 Oct", "Scoreboard · All time")
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
    assert [m["embeds"][0].title for m in channel.sent] == ["Results · Sat 10 Oct"]


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


def test_saying_on_when_it_is_already_on_changes_nothing(channel):
    run_autopost_command(FakeInteraction(), True)
    assert bot.get_settings(1).autopost_from == "2026-10-01"


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


def test_the_welcome_message_names_the_server_and_the_command_to_run():
    text = bot.welcome_text("Test Server")
    assert "Test Server" in text and "/settings channel" in text and "Manage Server" in text


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


def test_an_unreachable_owner_is_handled_like_closed_direct_messages(monkeypatch):
    async def fetch_user(user_id):
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown User")

    monkeypatch.setattr(bot.bot, "fetch_user", fetch_user)
    system = writable(Sink())
    assert asyncio.run(bot.send_welcome(make_guild(system=system))) == "channel"


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


def test_the_guide_never_pings_anyone(db):
    channel = GuideChannel(555)
    asyncio.run(bot.post_guide(channel))
    assert channel.messages[0]["allowed_mentions"].everyone is False


def test_the_env_shortcut_does_not_trigger_a_guide(db):
    bot.seed_channel(1, 555)
    assert asyncio.run(bot.post_guide(GuideChannel(555))) is False


def test_the_guide_points_to_the_results_channel_or_asks_for_setup():
    assert "<#555>" in bot.help_embed(555).description
    assert "/settings channel" in bot.help_embed().description


def test_the_guide_mentions_every_command_the_bot_has():
    commands_field = next(f for f in bot.help_embed().fields if f.name == "Commands")
    for command in bot.bot.tree.get_commands():
        assert f"`/{command.name}`" in commands_field.value


def test_the_guide_lists_every_supported_game():
    posting = next(f for f in bot.help_embed().fields if f.name == "Posting results")
    for title in bot.GAMES.values():
        assert title in posting.value


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