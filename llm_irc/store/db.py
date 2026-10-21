"""SQLite storage: message log, read markers and per-user profiles. Local only."""
from __future__ import annotations

import re
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

# Full-text index over messages.text. Messages are never updated, so no update trigger.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text, content='messages', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts (rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts (messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
INSERT INTO messages_fts (messages_fts) VALUES ('rebuild');
"""


def _fts_step(db: sqlite3.Connection) -> str:
    # Without FTS5 (rare SQLite builds) the version still advances and search uses LIKE.
    has_fts5 = db.execute("SELECT sqlite_compileoption_used('ENABLE_FTS5')").fetchone()[0]
    return FTS_SCHEMA if has_fts5 else ""


# Schema history, applied in order and tracked in PRAGMA user_version. Never edit a
# released step; append a new one. A step is SQL, or a function of the connection
# returning SQL. Step 1's IF NOT EXISTS lets databases from before versioning pass.
MIGRATIONS: list = [
    SCHEMA,
    """
    -- How reply suggestions get used. message_id marks sent lines the LLM wrote.
    CREATE TABLE IF NOT EXISTS suggestion_events (
        id INTEGER PRIMARY KEY,
        network TEXT NOT NULL,
        buffer TEXT NOT NULL,
        ts REAL NOT NULL,
        option_index INTEGER,
        outcome TEXT NOT NULL,     -- picked | edited | rephrased | dismissed | ignored
        suggestion TEXT NOT NULL DEFAULT '',
        sent_text TEXT NOT NULL DEFAULT '',
        message_id INTEGER
    );
    -- Own lines are a small share of the log; keeps the style-example query cheap.
    CREATE INDEX IF NOT EXISTS idx_messages_own ON messages(network, id) WHERE own = 1;
    """,
    _fts_step,
]


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


def _fts_query(words: list[str], match_any: bool) -> str:
    """Each word quoted, so user input can't be FTS syntax; longer words match as prefixes."""
    terms = []
    for word in words:
        if not any(ch.isalnum() for ch in word):
            continue  # a punctuation-only term matches nothing and would void an AND query
        quoted = '"' + word.replace('"', '""') + '"'
        terms.append(quoted + "*" if len(word) >= 3 and word.isalnum() else quoted)
    return (" OR " if match_any else " ").join(terms)


def _row(r: tuple) -> StoredMessage:
    """A StoredMessage from `SELECT id, buffer, nick, text, kind, own, ts`."""
    return StoredMessage(r[0], r[1], r[2], r[3], r[4], bool(r[5]), r[6])


class Store:
    def __init__(self, path: str) -> None:
        self.db = sqlite3.connect(path)
        # WAL + NORMAL: one commit per chat line without an fsync stall each time.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        try:
            self._migrate()
        except sqlite3.Error:
            self.db.close()
            raise
        self.fts = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'messages_fts'").fetchone() is not None

    def _migrate(self) -> None:
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        for number, step in enumerate(MIGRATIONS[version:], version + 1):
            script = step(self.db) if callable(step) else step
            try:
                self.db.executescript(f"BEGIN IMMEDIATE;\n{script}\n"
                                      f"PRAGMA user_version = {number};\nCOMMIT;")
            except sqlite3.Error:
                self.db.rollback()  # executescript leaves the failed transaction open
                raise

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
        return [_row(r) for r in reversed(rows)]

    def messages_by(self, network: str, nick: str, limit: int) -> list[StoredMessage]:
        rows = self.db.execute(
            "SELECT id, buffer, nick, text, kind, own, ts FROM messages "
            "WHERE network = ? AND nick = ? COLLATE NOCASE AND own = 0 "
            "ORDER BY id DESC LIMIT ?",
            (network, nick, limit)).fetchall()
        return [_row(r) for r in reversed(rows)]

    def own_messages(self, network: str, limit: int, buffer: str = "") -> list[StoredMessage]:
        """Lines the user typed themselves, newest first: this buffer first, then channels.

        Other private chats stay out (they may hold /msg NickServ passwords), and so do
        sent lines the LLM wrote (accepted or rephrased suggestions).
        """
        key = irc_lower(buffer)
        rows = self.db.execute(
            "SELECT id, buffer, nick, text, kind, own, ts FROM messages "
            "WHERE network = ? AND own = 1 AND kind = 'msg' "
            "AND (buffer = ? OR substr(buffer, 1, 1) IN ('#', '&')) "
            "AND id NOT IN (SELECT message_id FROM suggestion_events "
            "WHERE message_id IS NOT NULL) "
            "ORDER BY buffer = ? DESC, id DESC LIMIT ?",
            (network, key, key, limit)).fetchall()
        return [_row(r) for r in rows]

    # --- search ---

    def search(self, network: str, words: list[str], *, buffer: str | None = None,
               limit: int = 20, match_any: bool = False) -> list[StoredMessage]:
        """Messages containing all `words` (newest first), or any of them (best first)."""
        query = _fts_query(words, match_any)
        if not query:
            return []
        if not self.fts:
            return self._search_like(network, words, buffer, limit, match_any)
        where, params = "messages_fts MATCH ? AND m.network = ?", [query, network]
        if buffer:
            where += " AND m.buffer = ?"
            params.append(irc_lower(buffer))
        order = "bm25(messages_fts)" if match_any else "m.id DESC"
        try:
            rows = self.db.execute(
                "SELECT m.id, m.buffer, m.nick, m.text, m.kind, m.own, m.ts "
                "FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
                f"WHERE {where} ORDER BY {order} LIMIT ?", (*params, limit)).fetchall()
        except sqlite3.OperationalError:
            return self._search_like(network, words, buffer, limit, match_any)
        return [_row(r) for r in rows]

    def _search_like(self, network: str, words: list[str], buffer: str | None,
                     limit: int, match_any: bool) -> list[StoredMessage]:
        terms = [w for w in words if any(ch.isalnum() for ch in w)]
        if not terms:
            return []
        escaped = ["%" + re.sub(r"([\\%_])", r"\\\1", w) + "%" for w in terms]
        match = (" OR " if match_any else " AND ").join(["text LIKE ? ESCAPE '\\'"] * len(terms))
        where, params = f"network = ? AND ({match})", [network, *escaped]
        if buffer:
            where += " AND buffer = ?"
            params.append(irc_lower(buffer))
        rows = self.db.execute(
            "SELECT id, buffer, nick, text, kind, own, ts FROM messages "
            f"WHERE {where} ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()
        return [_row(r) for r in rows]

    def around(self, network: str, buffer: str, msg_id: int, before: int = 3,
               after: int = 3) -> list[StoredMessage]:
        """A message with its neighbours in the same buffer, oldest first."""
        key = irc_lower(buffer)
        older = self.db.execute(
            "SELECT id, buffer, nick, text, kind, own, ts FROM messages "
            "WHERE network = ? AND buffer = ? AND id <= ? ORDER BY id DESC LIMIT ?",
            (network, key, msg_id, before + 1)).fetchall()
        newer = self.db.execute(
            "SELECT id, buffer, nick, text, kind, own, ts FROM messages "
            "WHERE network = ? AND buffer = ? AND id > ? ORDER BY id LIMIT ?",
            (network, key, msg_id, after)).fetchall()
        return [_row(r) for r in [*reversed(older), *newer]]

    def excerpts(self, network: str, hits: list[StoredMessage], *, context: int = 3,
                 max_lines: int = 60) -> list[list[StoredMessage]]:
        """Hits (best first) widened with context, overlaps merged, in time order."""
        windows: list[dict[int, StoredMessage]] = []
        total = 0
        for hit in hits:
            if any(hit.id in window for window in windows):
                continue
            merged = {row.id: row for row in self.around(network, hit.buffer, hit.id,
                                                          context, context)}
            # Shared ids mean the same buffer: join every window this one touches.
            touching = [w for w in windows if w.keys() & merged.keys()]
            for window in touching:
                merged.update(window)
            added = len(merged) - sum(len(window) for window in touching)
            if total + added > max_lines:
                break
            total += added
            windows = [w for w in windows if not any(w is t for t in touching)] + [merged]
        ordered = [sorted(window.values(), key=lambda row: row.id) for window in windows]
        return sorted(ordered, key=lambda window: window[0].id)

    # --- suggestion feedback ---

    def add_suggestion_event(self, network: str, buffer: str, outcome: str, *,
                             index: int | None = None, suggestion: str = "", sent: str = "",
                             message_id: int | None = None) -> None:
        self.db.execute(
            "INSERT INTO suggestion_events (network, buffer, ts, option_index, outcome, "
            "suggestion, sent_text, message_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (network, irc_lower(buffer), time.time(), index, outcome, suggestion, sent,
             message_id))
        self.db.commit()

    def suggestion_stats(self, network: str) -> dict[str, int]:
        rows = self.db.execute(
            "SELECT outcome, COUNT(*) FROM suggestion_events WHERE network = ? "
            "GROUP BY outcome", (network,)).fetchall()
        return dict(rows)

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
