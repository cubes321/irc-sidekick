"""Connection state: own nick, joined channels, their members and topics."""
from __future__ import annotations

import re
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
    # irc_lower(nick) -> (display nick, all mode prefixes in rank order, e.g. '@+' or '')
    members: dict[str, tuple[str, str]] = field(default_factory=dict)
    prefixes: str = MEMBER_PREFIXES  # the server's prefix characters, highest rank first
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

    def set_prefix(self, nick: str, char: str, on: bool) -> bool:
        """Give or take one prefix (from a MODE change), keeping the others."""
        key = irc_lower(nick)
        if key not in self.members:
            return False
        name, prefix = self.members[key]
        self.members[key] = (name, self._ranked(prefix.replace(char, "") + (char if on else "")))
        return True

    def _ranked(self, prefix: str) -> str:
        return "".join(ch for ch in self.prefixes if ch in prefix)

    def names_chunk(self, names: str) -> None:
        if self._pending_names is None:
            self._pending_names = {}
        for raw in names.split():
            prefix = ""
            while raw and raw[0] in self.prefixes:
                prefix += raw[0]
                raw = raw[1:]
            if raw:
                self._pending_names[irc_lower(raw)] = (raw, self._ranked(prefix))

    def names_end(self) -> None:
        if self._pending_names is not None:
            self.members = self._pending_names
            self._pending_names = None

    def sorted_members(self) -> list[tuple[str, str]]:
        """Members ordered by rank (ops first), then alphabetically, with their top prefix."""
        def rank(entry: tuple[str, str]) -> tuple[int, str]:
            nick, prefix = entry
            r = self.prefixes.index(prefix[0]) if prefix else len(self.prefixes)
            return r, irc_lower(nick)
        return [(nick, prefix[:1]) for nick, prefix in sorted(self.members.values(), key=rank)]


@dataclass
class ConnectionState:
    nick: str = ""
    channels: dict[str, ChannelState] = field(default_factory=dict)
    # From RPL_ISUPPORT (005); the defaults cover servers that don't send it.
    prefix_modes: str = "qaohv"
    prefix_chars: str = MEMBER_PREFIXES
    chanmodes: tuple[str, ...] = ("beI", "k", "l", "imnpst")  # list, always, on set, flag

    def is_me(self, nick: str | None) -> bool:
        return nick is not None and irc_lower(nick) == irc_lower(self.nick)

    def channel(self, name: str) -> ChannelState | None:
        return self.channels.get(irc_lower(name))

    def ensure_channel(self, name: str) -> ChannelState:
        key = irc_lower(name)
        if key not in self.channels:
            self.channels[key] = ChannelState(name, prefixes=self.prefix_chars)
        return self.channels[key]

    def drop_channel(self, name: str) -> None:
        self.channels.pop(irc_lower(name), None)

    def channels_with(self, nick: str) -> list[str]:
        return [c.name for c in self.channels.values() if c.has(nick)]

    def isupport(self, tokens: list[str]) -> None:
        """Take PREFIX and CHANMODES from the tokens of a 005 line."""
        for token in tokens:
            name, _, value = token.partition("=")
            if name == "PREFIX":
                match = re.fullmatch(r"\((\w+)\)(\S+)", value)
                if match and len(match[1]) == len(match[2]):
                    self.prefix_modes, self.prefix_chars = match[1], match[2]
            elif name == "CHANMODES" and value.count(",") >= 3:
                self.chanmodes = tuple(value.split(",")[:4])

    def prefix_char(self, mode: str) -> str | None:
        """'o' -> '@' for modes that give a member prefix."""
        i = self.prefix_modes.find(mode)
        return self.prefix_chars[i] if i >= 0 else None

    def parse_modes(self, modes: str, args: list[str]) -> list[tuple[str, str | None]]:
        """'+ov-l alice bob' -> [('+o', 'alice'), ('+v', 'bob'), ('-l', None)]."""
        lists, always, on_set = self.chanmodes[:3]
        with_arg = self.prefix_modes + lists + always
        sign, args, changes = "+", list(args), []
        for ch in modes:
            if ch in "+-":
                sign = ch
                continue
            needs = ch in with_arg or (sign == "+" and ch in on_set)
            changes.append((sign + ch, args.pop(0) if needs and args else None))
        return changes
