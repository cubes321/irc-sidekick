"""SQLite storage: message log, read markers and per-user profiles. Local only."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from ..irc.state import irc_lower

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    network TEXT NOT NULL,
    buffer TEXT NOT NULL,      -- irc_lower(channel or query nick)
    nick TEXT NOT NULL,
    text TEXT NOT NULL,
    kind TEXT NOT NULL,        -- msg | action | notice
    own INTEGER NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_buffer ON messages(network, buffer, id);
CREATE INDEX IF NOT EXISTS idx_messages_nick ON messages(network, nick, id);

CREATE TABLE IF NOT EXISTS read_markers (
    network TEXT NOT NULL,
    buffer TEXT NOT NULL,
    last_id INTEGER NOT NULL,
    PRIMARY KEY (network, buffer)
);

CREATE TABLE IF NOT EXISTS profiles (
    network TEXT NOT NULL,
    nick_key TEXT NOT NULL,    -- irc_lower(nick)
    nick TEXT NOT NULL,
    profile TEXT NOT NULL DEFAULT '',
    message_count INTEGER NOT NULL DEFAULT 0,
    analyzed_count INTEGER NOT NULL DEFAULT 0,
    first_seen REAL NOT NULL,
    updated REAL,
    PRIMARY KEY (network, nick_key)
);
"""


@dataclass
class StoredMessage:
    id: int
    buffer: str
    nick: str
    text: str
    kind: str
    own: bool
    ts: float


@dataclass
class Profile:
    nick: str
    profile: str
    message_count: int
    analyzed_count: int
    first_seen: float
    updated: float | None


class Store:
    def __init__(self, path: str) -> None:
        self.db = sqlite3.connect(path)
        # WAL + NORMAL: one commit per chat line without an fsync stall each time.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # --- messages ---

    def add_message(self, network: str, buffer: str, nick: str, text: str,
                    kind: str, own: bool, ts: float) -> int:
        cur = self.db.execute(
            "INSERT INTO messages (network, buffer, nick, text, kind, own, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (network, irc_lower(buffer), nick, text, kind, int(own), ts))
        self.db.commit()
        return cur.lastrowid

    def recent(self, network: str, buffer: str, limit: int,
               after_id: int = 0) -> list[StoredMessage]:
        """The last `limit` messages in a buffer (oldest first), optionally after an id."""
        rows = self.db.execute(
            "SELECT id, buffer, nick, text, kind, own, ts FROM messages "
            "WHERE network = ? AND buffer = ? AND id > ? ORDER BY id DESC LIMIT ?",
            (network, irc_lower(buffer), after_id, limit)).fetchall()
        return [StoredMessage(r[0], r[1], r[2], r[3], r[4], bool(r[5]), r[6])
                for r in reversed(rows)]

    def messages_by(self, network: str, nick: str, limit: int) -> list[StoredMessage]:
        rows = self.db.execute(
            "SELECT id, buffer, nick, text, kind, own, ts FROM messages "
            "WHERE network = ? AND nick = ? COLLATE NOCASE AND own = 0 "
            "ORDER BY id DESC LIMIT ?",
            (network, nick, limit)).fetchall()
        return [StoredMessage(r[0], r[1], r[2], r[3], r[4], bool(r[5]), r[6])
                for r in reversed(rows)]

    # --- read markers ---

    def read_marker(self, network: str, buffer: str) -> int:
        row = self.db.execute(
            "SELECT last_id FROM read_markers WHERE network = ? AND buffer = ?",
            (network, irc_lower(buffer))).fetchone()
        return row[0] if row else 0

    def set_read_marker(self, network: str, buffer: str, last_id: int) -> None:
        self.db.execute(
            "INSERT INTO read_markers (network, buffer, last_id) VALUES (?, ?, ?) "
            "ON CONFLICT (network, buffer) DO UPDATE SET last_id = excluded.last_id",
            (network, irc_lower(buffer), last_id))
        self.db.commit()

    def count_after(self, network: str, buffer: str, after_id: int) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) FROM messages WHERE network = ? AND buffer = ? AND id > ?",
            (network, irc_lower(buffer), after_id)).fetchone()
        return row[0]

    # --- profiles ---

    def bump_profile(self, network: str, nick: str) -> Profile:
        """Count one more message from `nick` and return the updated profile row."""
        now = time.time()
        self.db.execute(
            "INSERT INTO profiles (network, nick_key, nick, message_count, first_seen) "
            "VALUES (?, ?, ?, 1, ?) "
            "ON CONFLICT (network, nick_key) DO UPDATE SET "
            "message_count = message_count + 1, nick = excluded.nick",
            (network, irc_lower(nick), nick, now))
        self.db.commit()
        return self.get_profile(network, nick)  # type: ignore[return-value]

    def get_profile(self, network: str, nick: str) -> Profile | None:
        row = self.db.execute(
            "SELECT nick, profile, message_count, analyzed_count, first_seen, updated "
            "FROM profiles WHERE network = ? AND nick_key = ?",
            (network, irc_lower(nick))).fetchone()
        return Profile(*row) if row else None

    def save_profile(self, network: str, nick: str, profile: str, analyzed_count: int) -> None:
        self.db.execute(
            "UPDATE profiles SET profile = ?, analyzed_count = ?, updated = ? "
            "WHERE network = ? AND nick_key = ?",
            (profile, analyzed_count, time.time(), network, irc_lower(nick)))
        self.db.commit()

    def forget_profile(self, network: str, nick: str) -> bool:
        cur = self.db.execute(
            "DELETE FROM profiles WHERE network = ? AND nick_key = ?",
            (network, irc_lower(nick)))
        self.db.commit()
        return cur.rowcount > 0
