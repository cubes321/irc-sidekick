"""Asyncio IRC connection: registration, read loop, rate-limited send queue, reconnect."""
from __future__ import annotations

import asyncio
import contextlib
import ssl
import time
from dataclasses import dataclass, field
from typing import Callable

from ..config import NetworkConfig
from . import message as m
from .auth import Q_PREFIX, Q_TARGET, challengeauth_response, pick_algorithm
from .state import ConnectionState, irc_lower, is_channel

CLIENT_VERSION = "llm-irc 0.1 (Textual + LM Studio)"
# Servers relay our messages as ":nick!user@host PRIVMSG target :text", so leave
# room for a prefix we don't control when splitting long messages.
PREFIX_RESERVE = 100
READ_TIMEOUT_S = 180
PING_GRACE_S = 60


# --- Events delivered to the UI -------------------------------------------------------

@dataclass
class Event:
    pass


@dataclass
class Connected(Event):
    nick: str


@dataclass
class Disconnected(Event):
    reason: str
    will_retry: bool


@dataclass
class Status(Event):
    text: str
    buffer: str | None = None  # None means the server/status tab
    error: bool = False


@dataclass
class ChatMessage(Event):
    buffer: str  # channel name, or the other nick for private messages
    nick: str
    text: str
    kind: str = "msg"  # msg | action | notice
    own: bool = False
    ts: float = field(default_factory=time.time)


@dataclass
class Joined(Event):
    channel: str
    nick: str
    own: bool


@dataclass
class Parted(Event):
    channel: str
    nick: str
    reason: str
    own: bool


@dataclass
class Kicked(Event):
    channel: str
    nick: str
    by: str
    reason: str
    own: bool


@dataclass
class UserQuit(Event):
    nick: str
    reason: str
    channels: list[str]


@dataclass
class NickChanged(Event):
    old: str
    new: str
    channels: list[str]
    own: bool


@dataclass
class TopicChanged(Event):
    channel: str
    topic: str
    by: str | None


@dataclass
class NamesUpdated(Event):
    channel: str


@dataclass
class ModeChanged(Event):
    channel: str
    by: str
    modes: str  # as sent, e.g. "+o-v alice bob"


@dataclass
class WhoisReply(Event):
    nick: str
    lines: list[str]


# --- Rate limiting --------------------------------------------------------------------

class TokenBucket:
    """Allows `burst` lines at once, then one line every `interval` seconds."""

    def __init__(self, burst: int = 5, interval: float = 1.5) -> None:
        self.capacity = burst
        self.tokens = float(burst)
        self.interval = interval
        self.updated = time.monotonic()

    def delay(self) -> float:
        """Take a token, returning how long to wait before sending."""
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) / self.interval)
        self.updated = now
        self.tokens -= 1
        return 0.0 if self.tokens >= 0 else -self.tokens * self.interval


# --- Client ---------------------------------------------------------------------------

class IRCClient:
    def __init__(self, network: NetworkConfig, on_event: Callable[[Event], None]) -> None:
        self.network = network
        self.on_event = on_event
        self.state = ConnectionState(nick=network.nick)
        self.connected = False
        self._writer: asyncio.StreamWriter | None = None
        self._send_queue: asyncio.Queue[str] = asyncio.Queue()
        self._bucket = TokenBucket()
        self._quitting = False
        self._nick_attempt = 0
        # Channels to (re)join after registration: config + whatever we joined since.
        self._autojoin: dict[str, str] = {irc_lower(c): c for c in network.channels}
        # Set once login / host hiding is done, so channels never see our real host.
        self._ready = asyncio.Event()
        self._join_task: asyncio.Task | None = None
        self._q_pending = False
        self.account: str | None = None
        self.away = False
        self._away_seen: dict[str, str] = {}  # irc_lower(nick) -> last away message shown
        self._whois: dict[str, list[str]] = {}  # irc_lower(nick) -> reply lines so far
        self.ignored: set[str] = set()  # irc_lower(nick); filled from the store by the UI

    # --- public API ---

    async def run(self) -> None:
        """Connect and keep reconnecting with backoff until quit() is called."""
        backoff = 2.0
        while not self._quitting:
            reason = "connection closed"
            try:
                await self._session()
                backoff = 2.0
            except (OSError, asyncio.IncompleteReadError, ssl.SSLError) as exc:
                reason = f"{type(exc).__name__}: {exc}"
            except _PingTimeout:
                reason = "ping timeout"
            self.connected = False
            if self._quitting:
                self._emit(Disconnected("quit", will_retry=False))
                break
            self._emit(Disconnected(reason, will_retry=True))
            self._emit(Status(f"Reconnecting in {backoff:.0f}s..."))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    def send_raw(self, line: str) -> None:
        if "\r" in line or "\n" in line:
            raise ValueError("raw line must not contain CR/LF")
        self._send_queue.put_nowait(line)

    def privmsg(self, target: str, text: str, *, action: bool = False) -> None:
        for line in text.splitlines() or [""]:
            budget = m.MAX_LINE_BYTES - 2 - PREFIX_RESERVE - len(f"PRIVMSG {target} :".encode())
            if action:
                budget -= len(m.ctcp("ACTION").encode()) + 1
            for chunk in m.split_text(line, budget):
                payload = m.ctcp("ACTION", chunk) if action else chunk
                self.send_raw(m.serialize("PRIVMSG", target, payload))
                self._emit(ChatMessage(target, self.state.nick, chunk,
                                       kind="action" if action else "msg", own=True))

    def notice(self, target: str, text: str) -> None:
        self.send_raw(m.serialize("NOTICE", target, text))

    def join(self, channel: str, key: str = "") -> None:
        if key:
            self.send_raw(m.serialize("JOIN", channel, key))
        else:
            self.send_raw(m.serialize("JOIN", channel))

    def part(self, channel: str, reason: str = "") -> None:
        self._autojoin.pop(irc_lower(channel), None)
        if reason:
            self.send_raw(m.serialize("PART", channel, reason))
        else:
            self.send_raw(m.serialize("PART", channel))

    def set_nick(self, nick: str) -> None:
        self.send_raw(m.serialize("NICK", nick))

    def set_away(self, message: str = "") -> None:
        """Mark ourselves away with `message`, or back when it is empty."""
        self.send_raw(m.serialize("AWAY", message) if message else "AWAY")

    def whois(self, nick: str) -> None:
        self._whois[irc_lower(nick)] = []
        self.send_raw(m.serialize("WHOIS", nick))

    def topic(self, channel: str, text: str | None = None) -> None:
        if text is None:
            self.send_raw(m.serialize("TOPIC", channel))
        else:
            self.send_raw(m.serialize("TOPIC", channel, text))

    async def quit(self, reason: str = "Leaving") -> None:
        self._quitting = True
        if self._writer is not None and not self._writer.is_closing():
            with contextlib.suppress(Exception):
                self._writer.write(f"{m.serialize('QUIT', reason)}\r\n".encode())
                await asyncio.wait_for(self._writer.drain(), 2)
            self._writer.close()

    # --- internals ---

    def _emit(self, event: Event) -> None:
        self.on_event(event)

    async def _session(self) -> None:
        net = self.network
        ssl_ctx = None
        if net.tls:
            ssl_ctx = ssl.create_default_context()
            if not net.tls_verify:
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
        self._emit(Status(f"Connecting to {net.host}:{net.port}{' (TLS)' if net.tls else ''}..."))
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(net.host, net.port, ssl=ssl_ctx, limit=2**16), 30)
        self._writer = writer
        self._send_queue = asyncio.Queue()
        self._nick_attempt = 0
        self.state.channels.clear()
        self.state.nick = net.nick
        self.away = False
        self._away_seen.clear()
        self._whois.clear()

        if net.server_password:
            self._write_now(m.serialize("PASS", net.server_password))
        # Hook for IRCv3 CAP/SASL negotiation later: it would go here, before NICK/USER.
        self._write_now(m.serialize("NICK", net.nick))
        self._write_now(m.serialize("USER", net.username or net.nick, "0", "*", net.realname))

        sender = asyncio.create_task(self._send_loop(writer))
        try:
            await self._read_loop(reader)
        finally:
            if self._join_task is not None:
                self._join_task.cancel()
                self._join_task = None
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender
            writer.close()
            self._writer = None

    def _write_now(self, line: str) -> None:
        assert self._writer is not None
        self._writer.write(f"{line}\r\n".encode("utf-8"))

    async def _send_loop(self, writer: asyncio.StreamWriter) -> None:
        while True:
            line = await self._send_queue.get()
            delay = self._bucket.delay()
            if delay:
                await asyncio.sleep(delay)
            writer.write(f"{line}\r\n".encode("utf-8"))
            await writer.drain()

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        pinged = False
        while True:
            try:
                raw = await asyncio.wait_for(
                    reader.readline(), PING_GRACE_S if pinged else READ_TIMEOUT_S)
            except asyncio.TimeoutError:
                if pinged:
                    raise _PingTimeout()
                pinged = True
                self._write_now(m.serialize("PING", "llm-irc"))
                continue
            if not raw:
                return  # EOF
            pinged = False
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError:
                line = raw.decode("latin-1")
            try:
                msg = m.parse(line)
            except ValueError:
                continue
            try:
                self._handle(msg)
            except Exception as exc:  # a UI bug must not drop the connection
                self._emit(Status(f"Error handling {msg.command}: {type(exc).__name__}: {exc}",
                                  error=True))

    def _handle(self, msg: m.Message) -> None:
        handler = getattr(self, f"_on_{msg.command.lower()}", None)
        if handler is not None:
            handler(msg)
        elif msg.command.isdigit():
            self._on_numeric(msg)

    # --- command handlers ---

    def _on_ping(self, msg: m.Message) -> None:
        self.send_raw(m.serialize("PONG", msg.text or ""))

    def _on_001(self, msg: m.Message) -> None:
        self.state.nick = msg.params[0]
        self.connected = True
        self._emit(Connected(self.state.nick))
        self._emit(Status(msg.text or "Connected"))
        self._ready = asyncio.Event()
        self.account = None
        hold_joins = self._begin_login()
        if self.network.hide_host:
            self.send_raw(m.serialize("MODE", self.state.nick, "+x"))
            hold_joins = True
        if hold_joins:
            self._join_task = asyncio.create_task(self._join_when_ready())
        else:
            self._join_autojoin()

    def _join_autojoin(self) -> None:
        for channel in self._autojoin.values():
            self.join(channel)

    def _begin_login(self) -> bool:
        """Start services login. Returns True when joins should wait for it."""
        net = self.network
        if not net.auth:
            return False
        password = net.auth_password
        if not password:
            self._emit(Status(f"auth = {net.auth!r} but environment variable "
                              f"{net.auth_password_env or '(auth_password_env)'} is not set; "
                              "skipping login", error=True))
            return False
        if net.auth == "nickserv":
            self.privmsg_silent("NickServ", f"IDENTIFY {net.auth_account} {password}")
            return False
        # QuakeNet Q: ask for a challenge; the reply is handled in _on_q_notice.
        self._q_pending = True
        self.privmsg_silent(Q_TARGET, "CHALLENGE")
        self._emit(Status(f"Logging in to Q as {net.auth_account} (CHALLENGEAUTH)..."))
        return True

    def login(self) -> None:
        """Retry services login (the /auth command) on a live connection."""
        if not self.connected:
            self._emit(Status("Not connected.", error=True))
            return
        self._begin_login()
        if self.network.hide_host:
            self.send_raw(m.serialize("MODE", self.state.nick, "+x"))

    async def _join_when_ready(self) -> None:
        try:
            await asyncio.wait_for(self._ready.wait(), self.network.auth_timeout_s)
        except asyncio.TimeoutError:
            self._emit(Status("Login / host hiding did not finish in "
                              f"{self.network.auth_timeout_s:.0f}s; joining channels anyway",
                              error=True))
        self._join_task = None
        self._join_autojoin()

    def _on_q_notice(self, text: str) -> bool:
        """Handle a notice from QuakeNet's Q. Returns True if it should not be displayed."""
        net = self.network
        if text.startswith("CHALLENGE ") and self._q_pending:
            parts = text.split()
            algorithm = pick_algorithm(parts[2:])
            if len(parts) < 3 or algorithm is None:
                self._emit(Status(f"Q offered no supported algorithm: {text}", error=True))
                self._q_pending = False
                self._ready.set()
                return True
            response = challengeauth_response(net.auth_account, net.auth_password or "",
                                              parts[1], algorithm)
            self.privmsg_silent(Q_TARGET, f"CHALLENGEAUTH {net.auth_account} {response} {algorithm}")
            self._emit(Status(f"Answered Q's challenge ({algorithm}), waiting for Q..."))
            return True
        lowered = text.lower()
        if "you are now logged in as" in lowered:
            self._q_pending = False
            self.account = net.auth_account
            self._emit(Status(f"Logged in to Q as {net.auth_account}"))
            if not net.hide_host:
                self._ready.set()
            return True
        if self._q_pending and ("incorrect" in lowered or "failed" in lowered
                                or "suspended" in lowered or "too many" in lowered):
            self._q_pending = False
            self._emit(Status(f"Q login failed: {text}", error=True))
            self._ready.set()  # join anyway, but with the real host
            return True
        return False

    def _on_396(self, msg: m.Message) -> None:  # RPL_HOSTHIDDEN: me, host, text
        host = msg.params[1] if len(msg.params) > 1 else "?"
        self._emit(Status(f"Your host is now hidden as {host}"))
        self._ready.set()

    def privmsg_silent(self, target: str, text: str) -> None:
        """Send without echoing to the UI (used for passwords)."""
        self.send_raw(m.serialize("PRIVMSG", target, text))

    def _on_005(self, msg: m.Message) -> None:  # RPL_ISUPPORT: me, tokens..., text
        self.state.isupport(msg.params[1:-1])
        self._on_numeric(msg)

    def _on_301(self, msg: m.Message) -> None:  # RPL_AWAY: me, nick, message
        if len(msg.params) < 3:
            return
        nick, text = msg.params[1], msg.params[2]
        key = irc_lower(nick)
        # Part of a WHOIS reply only once that reply has begun (311 comes first): an
        # earlier 301 answers a message we sent before the WHOIS left the send queue.
        if self._whois.get(key):
            self._whois[key].append(f"away: {text}")
        elif self._away_seen.get(key) != text:  # servers repeat it for every message
            self._away_seen[key] = text
            self._emit(Status(f"{nick} is away: {text}", buffer=nick))

    def _on_305(self, msg: m.Message) -> None:  # RPL_UNAWAY
        self.away = False
        self._on_numeric(msg)

    def _on_306(self, msg: m.Message) -> None:  # RPL_NOWAWAY
        self.away = True
        self._on_numeric(msg)

    def _on_318(self, msg: m.Message) -> None:  # RPL_ENDOFWHOIS: me, nick, text
        nick = msg.params[1] if len(msg.params) > 1 else ""
        lines = self._whois.pop(irc_lower(nick), None)
        if lines is None:
            self._on_numeric(msg)  # e.g. a WHOIS sent with /raw
        else:
            self._emit(WhoisReply(nick, lines))

    def _on_401(self, msg: m.Message) -> None:  # ERR_NOSUCHNICK: me, nick, text
        lines = self._whois.get(irc_lower(msg.params[1])) if len(msg.params) > 1 else None
        if lines is not None:
            lines.append("no such nick")  # 318 follows and reports it
        else:
            self._on_numeric(msg)

    def _on_433(self, msg: m.Message) -> None:  # ERR_NICKNAMEINUSE
        if self.connected:
            self._emit(Status(f"Nick {msg.params[1]} is already in use", error=True))
            return
        alts = self.network.alt_nicks
        if self._nick_attempt < len(alts):
            nick = alts[self._nick_attempt]
        else:
            nick = f"{self.network.nick}{'_' * (self._nick_attempt - len(alts) + 1)}"
        self._nick_attempt += 1
        self.state.nick = nick
        self._emit(Status(f"Nick in use, trying {nick}"))
        self._write_now(m.serialize("NICK", nick))

    def _on_join(self, msg: m.Message) -> None:
        channel, nick = msg.params[0], msg.nick or ""
        own = self.state.is_me(nick)
        chan = self.state.ensure_channel(channel)
        chan.add(nick)
        if own:
            self._autojoin[irc_lower(channel)] = channel
        self._emit(Joined(channel, nick, own))

    def _on_part(self, msg: m.Message) -> None:
        channel, nick = msg.params[0], msg.nick or ""
        reason = msg.params[1] if len(msg.params) > 1 else ""
        own = self.state.is_me(nick)
        if own:
            self.state.drop_channel(channel)
        elif (chan := self.state.channel(channel)) is not None:
            chan.remove(nick)
        self._emit(Parted(channel, nick, reason, own))

    def _on_kick(self, msg: m.Message) -> None:
        channel, victim = msg.params[0], msg.params[1]
        reason = msg.params[2] if len(msg.params) > 2 else ""
        own = self.state.is_me(victim)
        if own:
            self.state.drop_channel(channel)
            self._autojoin.pop(irc_lower(channel), None)
        elif (chan := self.state.channel(channel)) is not None:
            chan.remove(victim)
        self._emit(Kicked(channel, victim, msg.nick or "", reason, own))

    def _on_quit(self, msg: m.Message) -> None:
        nick = msg.nick or ""
        channels = self.state.channels_with(nick)
        for name in channels:
            self.state.channel(name).remove(nick)
        self._emit(UserQuit(nick, msg.text or "", channels))

    def _on_nick(self, msg: m.Message) -> None:
        old, new = msg.nick or "", msg.params[0]
        own = self.state.is_me(old)
        if own:
            self.state.nick = new
        channels = [c.name for c in self.state.channels.values() if c.rename(old, new)]
        self._emit(NickChanged(old, new, channels, own))

    def _on_topic(self, msg: m.Message) -> None:
        channel, topic = msg.params[0], msg.params[-1] if len(msg.params) > 1 else ""
        if (chan := self.state.channel(channel)) is not None:
            chan.topic = topic
        self._emit(TopicChanged(channel, topic, msg.nick))

    def _on_332(self, msg: m.Message) -> None:  # RPL_TOPIC
        channel, topic = msg.params[1], msg.params[2]
        self.state.ensure_channel(channel).topic = topic
        self._emit(TopicChanged(channel, topic, None))

    def _on_333(self, msg: m.Message) -> None:  # RPL_TOPICWHOTIME
        pass

    def _on_353(self, msg: m.Message) -> None:  # RPL_NAMREPLY: me, symbol, channel, names
        if (chan := self.state.channel(msg.params[-2])) is not None:
            chan.names_chunk(msg.params[-1])

    def _on_366(self, msg: m.Message) -> None:  # RPL_ENDOFNAMES
        channel = msg.params[1]
        if (chan := self.state.channel(channel)) is not None:
            chan.names_end()
            self._emit(NamesUpdated(channel))

    def _on_mode(self, msg: m.Message) -> None:
        if len(msg.params) < 2:
            return
        target, modes = msg.params[0], " ".join(msg.params[1:])
        if not is_channel(target):
            self._emit(Status(f"Mode {target} {modes}"))  # user modes, e.g. our +x
            return
        chan = self.state.channel(target)
        for change, arg in self.state.parse_modes(msg.params[1], msg.params[2:]):
            char = self.state.prefix_char(change[1])
            if chan is not None and char and arg:
                chan.set_prefix(arg, char, change[0] == "+")
        self._emit(ModeChanged(target, msg.nick or "?", modes))

    def _on_privmsg(self, msg: m.Message) -> None:
        self._on_chat(msg, notice=False)

    def _on_notice(self, msg: m.Message) -> None:
        self._on_chat(msg, notice=True)

    def _on_chat(self, msg: m.Message, *, notice: bool) -> None:
        if len(msg.params) < 2:
            return
        nick = msg.nick or msg.prefix or "?"
        target, text = msg.params[0], msg.params[1]
        if notice and msg.prefix and msg.prefix.lower() == Q_PREFIX and self._on_q_notice(text):
            return
        if irc_lower(nick) in self.ignored:
            return  # before CTCP handling too, so ignored users get no automatic replies
        # Notices from the server itself (or before registration) go to the status tab.
        if notice and (msg.prefix is None or "!" not in msg.prefix or target == "*"):
            self._emit(Status(f"-{nick}- {text}"))
            return
        buffer = target if is_channel(target) else nick

        if (ctcp := msg.ctcp) is not None:
            command, arg = ctcp
            if command == "ACTION":
                self._emit(ChatMessage(buffer, nick, arg, kind="action"))
            elif not notice and command == "VERSION":
                self.notice(nick, m.ctcp("VERSION", CLIENT_VERSION))
                self._emit(Status(f"CTCP VERSION from {nick}"))
            elif not notice and command == "PING":
                self.notice(nick, m.ctcp("PING", arg))
            else:
                self._emit(Status(f"CTCP {command} {'reply ' if notice else ''}from {nick}: {arg}"))
            return
        # Service notices (NickServ etc.) read better in the status tab than as queries.
        if notice and not is_channel(target):
            self._emit(Status(f"-{nick}- {text}"))
            return
        self._emit(ChatMessage(buffer, nick, text, kind="notice" if notice else "msg"))

    def _on_error(self, msg: m.Message) -> None:
        self._emit(Status(f"Server error: {msg.text}", error=True))

    def _on_numeric(self, msg: m.Message) -> None:
        code = int(msg.command)
        rest = msg.params[1:]  # params[0] is our nick
        # Replies about a nick we sent WHOIS for are collected until 318. Servers send
        # many different ones (276, 307, 320, 338, 671...), so take any 2xx/3xx.
        whois = self._whois.get(irc_lower(rest[0])) if len(rest) >= 2 else None
        if whois is not None and code < 400:
            whois.append(_whois_line(code, msg.params))
            return
        # 4xx/5xx errors that name a channel or nick get shown in that tab.
        if 400 <= code < 600 and len(rest) >= 2:
            self._emit(Status(" ".join(rest[1:]) + f" ({rest[0]})", buffer=rest[0], error=True))
        else:
            self._emit(Status(" ".join(rest)))


class _PingTimeout(Exception):
    pass


def _whois_line(code: int, p: list[str]) -> str:
    """One readable line from a WHOIS reply (p[0] is our nick, p[1] theirs)."""
    match code:
        case 311 if len(p) >= 6:  # user host * :realname
            return f"{p[1]}!{p[2]}@{p[3]} ({p[-1]})"
        case 312 if len(p) >= 4:  # server :info
            return f"server: {p[2]} ({p[3]})"
        case 317 if len(p) >= 4 and p[2].isdigit():  # idle signon :text
            text = f"idle {_duration(int(p[2]))}"
            if len(p) >= 5 and p[3].isdigit():
                signon = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(p[3])))
                text += f", signed on {signon}"
            return text
        case 319:
            return f"channels: {p[-1]}"
        case 330 if len(p) >= 4:  # account :is logged in as
            return f"account: {p[2]}"
    return " ".join(p[2:])


def _duration(seconds: int) -> str:
    """125 -> '2m 5s'."""
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            parts.append(f"{seconds // size}{unit}")
            seconds %= size
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)
