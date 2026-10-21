"""IRC line parsing and serialization (RFC 1459 / 2812, tolerant of IRCv3 tags)."""
from __future__ import annotations

from dataclasses import dataclass, field

MAX_LINE_BYTES = 512  # including trailing CRLF
CTCP_DELIM = "\x01"


@dataclass
class Message:
    command: str
    params: list[str] = field(default_factory=list)
    prefix: str | None = None
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def nick(self) -> str | None:
        """Nick part of a `nick!user@host` prefix."""
        if self.prefix is None:
            return None
        return self.prefix.split("!", 1)[0]

    @property
    def target(self) -> str | None:
        return self.params[0] if self.params else None

    @property
    def text(self) -> str | None:
        """The last parameter, which is usually the free-text payload."""
        return self.params[-1] if self.params else None

    @property
    def ctcp(self) -> tuple[str, str] | None:
        """(command, argument) when the text is a CTCP payload, e.g. ('ACTION', 'waves')."""
        text = self.text
        if self.command not in ("PRIVMSG", "NOTICE") or not text:
            return None
        if not text.startswith(CTCP_DELIM):
            return None
        body = text.strip(CTCP_DELIM)
        cmd, _, arg = body.partition(" ")
        return cmd.upper(), arg


def _unescape_tag(value: str) -> str:
    out, i = [], 0
    escapes = {":": ";", "s": " ", "\\": "\\", "r": "\r", "n": "\n"}
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            out.append(escapes.get(value[i + 1], value[i + 1]))
            i += 2
        else:
            if ch != "\\":
                out.append(ch)
            i += 1
    return "".join(out)


def parse(line: str) -> Message:
    """Parse one IRC line (without CRLF). Raises ValueError on an empty line."""
    line = line.rstrip("\r\n")
    if not line.strip():
        raise ValueError("empty IRC line")

    tags: dict[str, str] = {}
    if line.startswith("@"):
        raw_tags, _, line = line[1:].partition(" ")
        for item in raw_tags.split(";"):
            if not item:
                continue
            key, _, value = item.partition("=")
            tags[key] = _unescape_tag(value)
        line = line.lstrip(" ")

    prefix = None
    if line.startswith(":"):
        prefix, _, line = line[1:].partition(" ")
        line = line.lstrip(" ")

    trailing = None
    if " :" in line:
        line, trailing = line.split(" :", 1)
    elif line.startswith(":"):
        trailing, line = line[1:], ""

    parts = line.split()
    if not parts:
        raise ValueError("IRC line has no command")
    command, params = parts[0].upper(), parts[1:]
    if trailing is not None:
        params.append(trailing)
    return Message(command=command, params=params, prefix=prefix, tags=tags)


def serialize(command: str, *params: str) -> str:
    """Build a line (without CRLF). The last param gets a ':' when it needs one."""
    if not params:
        return command
    *middle, last = params
    for p in middle:
        if not p or " " in p or p.startswith(":"):
            raise ValueError(f"invalid middle parameter: {p!r}")
    if not last or " " in last or last.startswith(":"):
        last = ":" + last
    return " ".join([command, *middle, last])


def split_text(text: str, max_bytes: int) -> list[str]:
    """Split text into chunks of at most max_bytes UTF-8 bytes, preferring spaces.

    Never cuts inside a multi-byte character.
    """
    if max_bytes < 4:
        raise ValueError("max_bytes too small")
    chunks: list[str] = []
    remaining = text
    while len(remaining.encode("utf-8")) > max_bytes:
        # Longest prefix that fits in max_bytes, counted in characters.
        cut, size = 0, 0
        for ch in remaining:
            ch_len = len(ch.encode("utf-8"))
            if size + ch_len > max_bytes:
                break
            size += ch_len
            cut += 1
        space = remaining.rfind(" ", 0, cut)
        if space > cut // 2:  # break on a word if it doesn't waste too much room
            chunks.append(remaining[:space])
            remaining = remaining[space + 1:]
        else:
            chunks.append(remaining[:cut])
            remaining = remaining[cut:]
    if remaining or not chunks:
        chunks.append(remaining)
    return chunks


def ctcp(command: str, arg: str = "") -> str:
    body = f"{command} {arg}" if arg else command
    return f"{CTCP_DELIM}{body}{CTCP_DELIM}"
