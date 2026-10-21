"""Connection state: own nick, joined channels, their members and topics."""
from __future__ import annotations

from dataclasses import dataclass, field

MEMBER_PREFIXES = "~&@%+"
CHANNEL_TYPES = "#&!+"

_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ[]\\~", "abcdefghijklmnopqrstuvwxyz{}|^")


def irc_lower(name: str) -> str:
    """RFC 1459 casemapping: '[]\\~' are the uppercase forms of '{}|^'."""
    return name.translate(_LOWER)


def is_channel(name: str) -> bool:
    return bool(name) and name[0] in CHANNEL_TYPES


@dataclass
class ChannelState:
    name: str
    topic: str = ""
    # irc_lower(nick) -> (display nick, mode prefix such as '@' or '')
    members: dict[str, tuple[str, str]] = field(default_factory=dict)
    # Collects 353 replies until 366 arrives, so a NAMES refresh replaces the list.
    _pending_names: dict[str, tuple[str, str]] | None = None

    def add(self, nick: str, prefix: str = "") -> None:
        self.members[irc_lower(nick)] = (nick, prefix)

    def remove(self, nick: str) -> bool:
        return self.members.pop(irc_lower(nick), None) is not None

    def rename(self, old: str, new: str) -> bool:
        entry = self.members.pop(irc_lower(old), None)
        if entry is None:
            return False
        self.members[irc_lower(new)] = (new, entry[1])
        return True

    def has(self, nick: str) -> bool:
        return irc_lower(nick) in self.members

    def names_chunk(self, names: str) -> None:
        if self._pending_names is None:
            self._pending_names = {}
        for raw in names.split():
            prefix = ""
            while raw and raw[0] in MEMBER_PREFIXES:
                prefix += raw[0]
                raw = raw[1:]
            if raw:
                self._pending_names[irc_lower(raw)] = (raw, prefix[:1])

    def names_end(self) -> None:
        if self._pending_names is not None:
            self.members = self._pending_names
            self._pending_names = None

    def sorted_members(self) -> list[tuple[str, str]]:
        """Members ordered by rank (ops first), then alphabetically."""
        def rank(entry: tuple[str, str]) -> tuple[int, str]:
            nick, prefix = entry
            r = MEMBER_PREFIXES.index(prefix) if prefix else len(MEMBER_PREFIXES)
            return r, irc_lower(nick)
        return sorted(self.members.values(), key=rank)


@dataclass
class ConnectionState:
    nick: str = ""
    channels: dict[str, ChannelState] = field(default_factory=dict)

    def is_me(self, nick: str | None) -> bool:
        return nick is not None and irc_lower(nick) == irc_lower(self.nick)

    def channel(self, name: str) -> ChannelState | None:
        return self.channels.get(irc_lower(name))

    def ensure_channel(self, name: str) -> ChannelState:
        key = irc_lower(name)
        if key not in self.channels:
            self.channels[key] = ChannelState(name)
        return self.channels[key]

    def drop_channel(self, name: str) -> None:
        self.channels.pop(irc_lower(name), None)

    def channels_with(self, nick: str) -> list[str]:
        return [c.name for c in self.channels.values() if c.has(nick)]
