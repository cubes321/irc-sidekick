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
