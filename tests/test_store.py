import sqlite3

import pytest

from llm_irc.store import db
from llm_irc.store.db import Store


def user_version(path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def tables(path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()


def test_fresh_database_is_fully_migrated(tmp_path):
    path = tmp_path / "fresh.sqlite3"
    Store(str(path)).close()
    assert user_version(path) == len(db.MIGRATIONS)
    assert {"messages", "read_markers", "profiles"} <= tables(path)


def test_unversioned_database_upgrades_and_keeps_data(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(db.MIGRATIONS[0])
    conn.execute("INSERT INTO messages (network, buffer, nick, text, kind, own, ts) "
                 "VALUES ('net', '#c', 'bob', 'the release is on friday', 'msg', 0, 1.0)")
    conn.execute("INSERT INTO profiles (network, nick_key, nick, profile, first_seen) "
                 "VALUES ('net', 'bob', 'bob', 'Summary: tester', 1.0)")
    conn.commit()
    conn.close()
    assert user_version(path) == 0

    store = Store(str(path))
    assert [m.text for m in store.recent("net", "#c", 10)] == ["the release is on friday"]
    assert store.get_profile("net", "bob").profile == "Summary: tester"
    store.close()
    assert user_version(path) == len(db.MIGRATIONS)


def test_reopening_is_a_no_op(tmp_path):
    path = tmp_path / "again.sqlite3"
    store = Store(str(path))
    store.add_message("net", "#c", "bob", "hi", "msg", False, 1.0)
    store.close()
    store = Store(str(path))
    assert len(store.recent("net", "#c", 10)) == 1
    store.close()
    assert user_version(path) == len(db.MIGRATIONS)


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "broken.sqlite3"
    Store(str(path)).close()
    good = len(db.MIGRATIONS)
    monkeypatch.setattr(db, "MIGRATIONS", [*db.MIGRATIONS,
                                           "CREATE TABLE broken (x); CREATE TABLE broken (x);"])
    with pytest.raises(sqlite3.OperationalError):
        Store(str(path))
    assert user_version(path) == good
    assert "broken" not in tables(path)


def test_newer_database_opens_unchanged(tmp_path):
    path = tmp_path / "newer.sqlite3"
    Store(str(path)).close()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    Store(str(path)).close()
    assert user_version(path) == 99


def test_own_messages_prefers_buffer_and_skips_private_and_llm_lines(tmp_path):
    store = Store(str(tmp_path / "own.sqlite3"))
    add = store.add_message
    add("net", "#home", "me", "home line", "msg", True, 1.0)
    add("net", "#other", "me", "other channel line", "msg", True, 2.0)
    add("net", "NickServ", "me", "IDENTIFY me secret", "msg", True, 3.0)
    add("net", "dave", "me", "private to dave", "msg", True, 4.0)
    add("net", "#home", "bob", "not mine", "msg", False, 5.0)
    add("net", "#home", "me", "waves", "action", True, 6.0)
    picked = add("net", "#home", "me", "sounds good", "msg", True, 7.0)
    add("net2", "#home", "me", "other network", "msg", True, 8.0)
    store.add_suggestion_event("net", "#home", "picked", index=0, suggestion="sounds good",
                               sent="sounds good", message_id=picked)

    assert [m.text for m in store.own_messages("net", 10, "#Home")] == [
        "home line", "other channel line"]
    # In a query tab, that conversation's own lines come first; other queries stay out.
    assert [m.text for m in store.own_messages("net", 10, "dave")] == [
        "private to dave", "other channel line", "home line"]
    assert [m.text for m in store.own_messages("net", 1, "#other")] == ["other channel line"]
    store.close()


def test_suggestion_stats(tmp_path):
    store = Store(str(tmp_path / "stats.sqlite3"))
    for outcome in ("picked", "picked", "dismissed", "ignored"):
        store.add_suggestion_event("net", "#c", outcome)
    store.add_suggestion_event("other", "#c", "edited")
    assert store.suggestion_stats("net") == {"picked": 2, "dismissed": 1, "ignored": 1}
    assert store.suggestion_stats("nowhere") == {}
    store.close()
