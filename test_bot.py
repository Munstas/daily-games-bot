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
    assert {c.name for c in tree.get_commands()} == {"today", "scoreboard", "records", "import", "settings"}
    settings = tree.get_command("settings")
    assert {c.name for c in settings.commands} == {"view", "channel", "advanced"}
    advanced = next(c for c in settings.commands if c.name == "advanced")
    assert {c.name for c in advanced.commands} == {"timezone", "rollover", "reactions"}
    for command in tree.get_commands():  # what would be sent to Discord must serialise without errors
        command.to_dict(tree)


def test_parse_guild_ids():
    assert bot.parse_guild_ids(None) == []
    assert bot.parse_guild_ids("") == []
    assert bot.parse_guild_ids("123") == [123]
    assert bot.parse_guild_ids("123, 456;789  ") == [123, 456, 789]