"""The Textual application: tabs, input, suggestions, and wiring IRC <-> LLM."""
from __future__ import annotations

import asyncio
import re
from functools import partial
from typing import Any, Callable

from rich.text import Text
from textual import on
from textual.actions import SkipAction
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Input, Static, TabbedContent

from ..config import Config
from ..irc import client as ev
from ..irc.client import IRCClient
from ..irc.state import irc_lower, is_channel
from ..llm import features
from ..llm.features import Line
from ..llm.provider import LLMProvider, LLMUnavailable
from ..llm.scheduler import Priority, Scheduler
from ..store.db import Store
from . import commands as c
from .buffer import ChatBuffer
from .profile_view import ProfileScreen

STATUS_KEY = "\x00status"
BACKLOG_LINES = 40


class IRCApp(App):
    TITLE = "LLM IRC"
    CSS = """
    Screen { layout: vertical; }
    TabbedContent { height: 1fr; }
    TabbedContent > ContentSwitcher { height: 1fr; }
    #suggestions { height: auto; max-height: 8; padding: 0 1; background: $boost;
                   display: none; }
    #suggestions.visible { display: block; }
    #statusline { height: 1; padding: 0 1; background: $primary-background;
                  color: $text-muted; }
    """
    BINDINGS = [
        Binding("ctrl+space,ctrl+@,f2", "suggest_now", "Suggest", priority=True),
        Binding("tab", "tab_key", "Use suggestion", show=False, priority=True),
        # F6-F8 too: some terminals (e.g. xterm.js-based panels) turn Alt+digit into
        # characters like '¡' instead of sending an Alt key press.
        Binding("alt+1,f6", "accept(0)", show=False, priority=True),
        Binding("alt+2,f7", "accept(1)", show=False, priority=True),
        Binding("alt+3,f8", "accept(2)", show=False, priority=True),
        Binding("ctrl+r", "rephrase", "Rephrase", priority=True),
        Binding("ctrl+z", "undo_rephrase", show=False, priority=True),
        Binding("escape", "dismiss_suggestions", show=False, priority=True),
        Binding("f5", "summary", "Catch up"),
        Binding("alt+right,ctrl+pagedown", "next_tab", "Next tab", show=False, priority=True),
        Binding("alt+left,ctrl+pageup", "prev_tab", "Prev tab", show=False, priority=True),
        Binding("ctrl+q", "quit_client", "Quit", priority=True),
    ]

    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self.cfg = cfg
        self.store = Store(cfg.db_path)
        self.clients: dict[str, IRCClient] = {}
        self.buffers: dict[tuple[str, str], ChatBuffer] = {}
        self._pane_seq = 0
        self._active_id: str | None = None
        self._drafts: dict[str, str] = {}
        self._focus_on_join: set[tuple[str, str]] = set()
        self._closing: set[tuple[str, str]] = set()
        # LLM
        self.llm: LLMProvider | None = None
        self.llm_ready = False
        self.llm_status = "off" if not cfg.llm.enabled else "connecting..."
        self.scheduler = Scheduler(cfg.llm.max_concurrency, on_change=self.update_statusline)
        self.suggestions: list[str] = []
        self._suggest_cursor = -1
        self._suggest_pane: str | None = None
        self._rephrase_original: str | None = None
        self._triage_queue: dict[str, list[Line]] = {}

    # --- layout -----------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        # Kept as attributes: App.query_one only searches the active screen, so it
        # fails while a modal (e.g. /profile) is open and IRC events keep arriving.
        self.tabs = TabbedContent(id="tabs")
        self.bar = Static(id="suggestions")
        self.input = Input(placeholder="Type a message or /help", id="input")
        self.statusline = Static(id="statusline")
        yield self.tabs
        yield self.bar
        yield self.input
        yield self.statusline
        yield Footer()

    def on_mount(self) -> None:
        for net in self.cfg.networks:
            client = IRCClient(net, partial(self.handle_irc_event, net.name))
            self.clients[net.name] = client
            self.ensure_buffer(net.name, net.name, "status", focus=True)
            self.run_worker(client.run(), name=f"irc:{net.name}", group="irc")
        if self.cfg.llm.enabled:
            self.run_worker(self.connect_llm(), group="llm")
        self.input.focus()
        self.update_statusline()

    # --- buffers ----------------------------------------------------------------------

    def _key(self, network: str, name: str, kind: str = "") -> tuple[str, str]:
        return (network, STATUS_KEY if kind == "status" else irc_lower(name))

    def find(self, network: str, name: str) -> ChatBuffer | None:
        return self.buffers.get(self._key(network, name))

    def status_pane(self, network: str | None = None) -> ChatBuffer:
        network = network or self.cfg.networks[0].name
        return self.buffers[self._key(network, network, "status")]

    def active_buffer(self) -> ChatBuffer | None:
        pane = self.tabs.active_pane
        return pane if isinstance(pane, ChatBuffer) else None

    def ensure_buffer(self, network: str, name: str, kind: str, *,
                      focus: bool = False) -> ChatBuffer:
        key = self._key(network, name, kind)
        if (pane := self.buffers.get(key)) is not None:
            if focus:
                self.tabs.active = pane.id
            return pane
        self._pane_seq += 1
        pane = ChatBuffer(network, name, kind, f"buf{self._pane_seq}")
        self.buffers[key] = pane
        if kind != "status":
            self._load_backlog(pane)
        self.run_worker(self._mount_pane(pane, focus), group="ui")
        return pane

    async def _mount_pane(self, pane: ChatBuffer, focus: bool) -> None:
        await self.tabs.add_pane(pane)
        self.refresh_label(pane)
        if focus or self._active_id is None:
            self.tabs.active = pane.id

    def _load_backlog(self, pane: ChatBuffer) -> None:
        rows = self.store.recent(pane.network, pane.buffer_name, BACKLOG_LINES)
        for row in rows:
            pane.write_chat(row.nick, row.text, row.kind, own=row.own, ts=row.ts)
            pane.history.append(Line(row.nick, row.text, row.kind, row.ts))
        if rows:
            pane.write_info("── end of saved history ──")
            pane.last_db_id = rows[-1].id
        pane.catchup_from = self.store.read_marker(pane.network, pane.buffer_name)

    def remove_buffer(self, pane: ChatBuffer) -> None:
        self.store.set_read_marker(pane.network, pane.buffer_name, pane.last_db_id)
        self.buffers.pop(self._key(pane.network, pane.buffer_name, pane.kind), None)
        self._drafts.pop(pane.id, None)
        self.tabs.remove_pane(pane.id)

    def refresh_label(self, pane: ChatBuffer) -> None:
        try:
            self.tabs.get_tab(pane.id).label = pane.tab_label()
        except Exception:
            pass  # not mounted yet; _mount_pane refreshes it

    def update_statusline(self) -> None:
        line = getattr(self, "statusline", None)
        if line is None:
            return
        pane = self.active_buffer()
        parts = []
        if pane is not None and (client := self.clients.get(pane.network)):
            state = "connected" if client.connected else "offline"
            parts.append(f"{pane.network} as {client.state.nick} ({state})")
        llm = f"LLM: {self.llm_status}"
        if self.llm_ready:
            llm += f" · {self.scheduler.running}/{self.scheduler.max_concurrency} busy"
            if self.scheduler.queued:
                llm += f", {self.scheduler.queued} queued"
        parts.append(llm)
        if pane is not None and pane.translate:
            parts.append(f"translating → {self.cfg.features.translate_target}")
        line.update(" │ ".join(parts))

    # --- tab switching ----------------------------------------------------------------

    @on(TabbedContent.TabActivated)
    def on_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        pane = event.pane
        if not isinstance(pane, ChatBuffer):
            return
        # Leaving the previous tab: remember its draft and mark it read.
        if self._active_id and self._active_id != pane.id:
            self._drafts[self._active_id] = self.input.value
            prev = next((b for b in self.buffers.values() if b.id == self._active_id), None)
            if prev is not None:
                self.store.set_read_marker(prev.network, prev.buffer_name, prev.last_db_id)
        self._active_id = pane.id
        self.input.value = self._drafts.get(pane.id, "")
        self.clear_suggestions()
        self.scheduler.cancel("suggest")

        pane.catchup_from = self.store.read_marker(pane.network, pane.buffer_name)
        unread = pane.unread
        pane.unread, pane.highlight = 0, False
        self.refresh_label(pane)
        if unread >= self.cfg.features.summary_threshold and self.llm_ready:
            pane.write_info(f"{unread} new messages while you were away. "
                            "Press F5 or /summary for a catch-up.", style="bold cyan")
        elif (self.cfg.features.auto_suggest and pane.history
              and not self._is_own(pane, pane.history[-1])):
            self.request_suggestions(pane, Priority.SUGGEST, delay=0.5)
        self.input.focus()
        self.update_statusline()

    def _cycle_tab(self, step: int) -> None:
        panes = [p for p in self.tabs.query(ChatBuffer)]
        if not panes:
            return
        ids = [p.id for p in panes]
        current = ids.index(self.tabs.active) if self.tabs.active in ids else 0
        self.tabs.active = ids[(current + step) % len(ids)]

    def action_next_tab(self) -> None:
        self._cycle_tab(1)

    def action_prev_tab(self) -> None:
        self._cycle_tab(-1)

    # --- IRC events -------------------------------------------------------------------

    def handle_irc_event(self, network: str, event: ev.Event) -> None:
        status = self.status_pane(network)
        match event:
            case ev.Connected(nick=nick):
                status.write_info(f"Connected as {nick}", style="bold green")
            case ev.Disconnected(reason=reason, will_retry=retry):
                status.write_error(f"Disconnected: {reason}")
                for pane in self.buffers.values():
                    if pane.network == network and pane.kind == "channel":
                        pane.joined = False
                        pane.write_error("Disconnected" + (" (will rejoin)" if retry else ""))
                        self.refresh_label(pane)
            case ev.Status(text=text, buffer=buffer, error=error):
                target = (self.find(network, buffer) if buffer else None) or status
                (target.write_error if error else target.write_info)(text)
            case ev.ChatMessage():
                kind = "channel" if is_channel(event.buffer) else "query"
                self.handle_chat(self.ensure_buffer(network, event.buffer, kind), event)
            case ev.Joined(channel=channel, nick=nick, own=own):
                if own:
                    key = self._key(network, channel)
                    pane = self.ensure_buffer(network, channel, "channel",
                                              focus=key in self._focus_on_join)
                    self._focus_on_join.discard(key)
                    pane.joined = True
                    pane.write_info(f"You joined {channel}", style="green")
                    self.refresh_label(pane)
                elif pane := self.find(network, channel):
                    pane.write_info(f"→ {nick} joined")
            case ev.Parted(channel=channel, nick=nick, reason=reason, own=own):
                self._handle_leave(network, channel, nick, own, f"left ({reason})" if reason else "left")
            case ev.Kicked(channel=channel, nick=nick, by=by, reason=reason, own=own):
                self._handle_leave(network, channel, nick, own, f"was kicked by {by} ({reason})")
            case ev.UserQuit(nick=nick, reason=reason, channels=channels):
                for name in channels + [nick]:
                    if pane := self.find(network, name):
                        pane.write_info(f"← {nick} quit ({reason})")
            case ev.NickChanged(old=old, new=new, channels=channels, own=own):
                for name in channels + [old]:
                    if pane := self.find(network, name):
                        pane.write_info(f"{old} is now known as {new}")
                if own:
                    status.write_info(f"You are now known as {new}")
            case ev.TopicChanged(channel=channel, topic=topic, by=by):
                if pane := self.find(network, channel):
                    pane.set_topic(topic)
                    if by:
                        pane.write_info(f"{by} changed the topic to: {topic}")
        self._refresh_members(network, event)
        self.update_statusline()

    def _handle_leave(self, network: str, channel: str, nick: str, own: bool, what: str) -> None:
        pane = self.find(network, channel)
        if pane is None:
            return
        if not own:
            pane.write_info(f"← {nick} {what}")
            return
        pane.joined = False
        key = self._key(network, channel)
        if key in self._closing:
            self._closing.discard(key)
            self.remove_buffer(pane)
        else:
            pane.write_info(f"You {what}. /join to rejoin, /close to close the tab.",
                            style="yellow")
            self.refresh_label(pane)

    def _refresh_members(self, network: str, event: ev.Event) -> None:
        names: list[str] = []
        if isinstance(event, (ev.Joined, ev.Parted, ev.Kicked, ev.NamesUpdated)):
            names = [event.channel]
        elif isinstance(event, (ev.UserQuit, ev.NickChanged)):
            names = event.channels
        client = self.clients[network]
        for name in names:
            chan = client.state.channel(name)
            if chan is not None and (pane := self.find(network, name)):
                pane.set_members(chan.sorted_members())

    def _is_own(self, pane: ChatBuffer, line: Line) -> bool:
        return self.clients[pane.network].state.is_me(line.nick)

    # --- incoming / outgoing chat -----------------------------------------------------

    def handle_chat(self, pane: ChatBuffer, msg: ev.ChatMessage) -> None:
        net = pane.network
        my_nick = self.clients[net].state.nick
        pane.last_db_id = self.store.add_message(
            net, pane.buffer_name, msg.nick, msg.text, msg.kind, msg.own, msg.ts)
        line = Line(msg.nick, msg.text, msg.kind, msg.ts)
        pane.history.append(line)

        mentioned = (not msg.own and (pane.kind == "query" or features.mentions(
            msg.text, my_nick, self.cfg.features.triage_keywords)))
        pane.write_chat(msg.nick, msg.text, msg.kind, own=msg.own, ts=msg.ts,
                        highlight=mentioned and pane.kind == "channel")
        active = pane is self.active_buffer()
        if active:
            self.store.set_read_marker(net, pane.buffer_name, pane.last_db_id)
        elif not msg.own:
            pane.unread += 1
            if mentioned:
                pane.highlight = True
                self.bell()
            self.refresh_label(pane)

        if msg.own:
            if active:
                self.scheduler.cancel("suggest")
                self.clear_suggestions()
            return

        # Profiles count messages even while the LLM is offline.
        if self.cfg.features.profiles and msg.kind in ("msg", "action"):
            self.maybe_update_profile(net, msg.nick)
        if not self.llm_ready or msg.kind == "notice":
            return
        if pane.translate and features.looks_foreign(msg.text,
                                                     self.cfg.features.translate_target):
            self.request_translation(pane, line)
        if active and self.cfg.features.auto_suggest:
            self.request_suggestions(pane, Priority.SUGGEST,
                                     delay=self.cfg.llm.suggest_debounce_s)
        elif (not active and not mentioned and pane.kind == "channel"
              and self.cfg.features.llm_triage and features.looks_like_question(msg.text)):
            self.queue_triage(pane, line)

    @on(Input.Submitted, "#input")
    async def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value
        self.input.value = ""
        self._rephrase_original = None
        pane = self.active_buffer()
        if pane is None or not value.strip():
            return
        try:
            cmd = c.parse_input(value)
            if cmd is None:
                self.send_text(pane, c.plain_text(value))
            else:
                await self.run_command(pane, cmd)
        except c.CommandError as exc:
            pane.write_error(str(exc))

    def send_text(self, pane: ChatBuffer, text: str, *, action: bool = False) -> None:
        if pane.kind == "status":
            raise c.CommandError("This is the server tab. /join a channel or /query someone.")
        if pane.kind == "channel" and not pane.joined:
            raise c.CommandError(f"You're not in {pane.buffer_name}. /join it first.")
        client = self.clients[pane.network]
        if not client.connected:
            raise c.CommandError("Not connected.")
        client.privmsg(pane.buffer_name, text, action=action)

    # --- commands ---------------------------------------------------------------------

    async def run_command(self, pane: ChatBuffer, cmd: c.Command) -> None:
        net = pane.network
        client = self.clients[net]
        match cmd.name:
            case "join":
                c.require(cmd, 1)
                channel = cmd.args[0] if is_channel(cmd.args[0]) else "#" + cmd.args[0]
                existing = self.find(net, channel)
                if existing is not None and existing.joined:
                    self.tabs.active = existing.id
                    return
                self._focus_on_join.add(self._key(net, channel))
                client.join(channel, cmd.args[1] if len(cmd.args) > 1 else "")
            case "part":
                if cmd.args and is_channel(cmd.args[0]):
                    channel, reason = c.split_first(cmd.rest)
                else:
                    channel, reason = pane.buffer_name, cmd.rest
                if not is_channel(channel):
                    raise c.CommandError("Not a channel. Use /close for this tab.")
                client.part(channel, reason)
            case "msg":
                c.require(cmd, 2)
                target, text = c.split_first(cmd.rest)
                client.privmsg(target, text)
            case "query":
                c.require(cmd, 1)
                nick, text = c.split_first(cmd.rest)
                query = self.ensure_buffer(net, nick, "query", focus=True)
                if text:
                    self.send_text(query, text)
            case "me":
                c.require(cmd, 1)
                self.send_text(pane, cmd.rest, action=True)
            case "nick":
                c.require(cmd, 1)
                client.set_nick(cmd.args[0])
            case "topic":
                if pane.kind != "channel":
                    raise c.CommandError("/topic only works in a channel tab.")
                if cmd.rest:
                    client.topic(pane.buffer_name, cmd.rest)
                else:
                    chan = client.state.channel(pane.buffer_name)
                    pane.write_info(f"Topic: {chan.topic if chan and chan.topic else '(none)'}")
            case "close":
                if pane.kind == "status":
                    raise c.CommandError("Can't close the server tab. Use /quit to exit.")
                if pane.kind == "channel" and pane.joined and client.connected:
                    self._closing.add(self._key(net, pane.buffer_name))
                    client.part(pane.buffer_name, cmd.rest)
                else:
                    self.remove_buffer(pane)
            case "auth":
                client.login()
            case "raw":
                c.require(cmd, 1)
                client.send_raw(cmd.rest)
            case "quit":
                await self.action_quit_client(cmd.rest or "Leaving")
            case "suggest":
                self.action_suggest_now()
            case "summary":
                self.action_summary()
            case "rephrase":
                self.rephrase(cmd.args[0] if cmd.args else "polite")
            case "translate":
                if pane.kind == "status":
                    raise c.CommandError("/translate works in channel and query tabs.")
                arg = cmd.args[0].lower() if cmd.args else ""
                pane.translate = (arg == "on") if arg in ("on", "off") else not pane.translate
                pane.write_info(f"Translation into {self.cfg.features.translate_target} "
                                f"{'on' if pane.translate else 'off'} for this tab.")
                self.update_statusline()
            case "profile":
                c.require(cmd, 1)
                self.show_profile(net, cmd.args[0])
            case "forget":
                c.require(cmd, 1)
                gone = self.store.forget_profile(net, cmd.args[0])
                pane.write_info(f"Profile for {cmd.args[0]} "
                                f"{'deleted' if gone else 'not found'}.")
            case "llm":
                self.run_worker(self.connect_llm(), group="llm")
            case "help":
                if cmd.args:
                    name = c.ALIASES.get(cmd.args[0].lstrip("/"), cmd.args[0].lstrip("/"))
                    pane.write_info(c.HELP.get(name, f"No such command: {name}"))
                else:
                    pane.write_info("Commands:", style="bold")
                    for text in c.HELP.values():
                        pane.write_info("  " + text, style="")
                    pane.write_info("Keys: Ctrl+Space/F2 suggest · Tab, F6-F8 or Alt+1-3 use suggestion "
                                    "or complete nick · Ctrl+R rephrase · Ctrl+Z undo rephrase · "
                                    "F5 catch-up · Alt+←/→ switch tabs · Ctrl+Q quit", style="")
            case _:
                raise c.CommandError(f"Unknown command /{cmd.name}. Try /help.")

    async def action_quit_client(self, reason: str = "Leaving") -> None:
        self.scheduler.cancel_all()
        await asyncio.gather(*(client.quit(reason) for client in self.clients.values()),
                             return_exceptions=True)
        if self.llm is not None:
            await self.llm.close()
        self.store.close()
        self.exit()

    # --- LLM: setup and job plumbing --------------------------------------------------

    async def connect_llm(self) -> None:
        self.llm_status = "connecting..."
        self.update_statusline()
        if self.llm is None:
            self.llm = LLMProvider(self.cfg.llm)
        try:
            model = await self.llm.discover()
        except LLMUnavailable as exc:
            self.llm_ready = False
            self.llm_status = "offline (/llm to retry)"
            self.status_pane().write_error(str(exc))
        else:
            self.llm_ready = True
            self.llm_status = model
            self.status_pane().write_info(
                f"LLM ready: {model} ({self.cfg.llm.max_concurrency} parallel slots)",
                style="green")
        self.update_statusline()

    def _when_done(self, future: asyncio.Future, on_ok: Callable[[Any], None],
                   on_error: Callable[[Exception], None] | None = None) -> None:
        def done(f: asyncio.Future) -> None:
            if f.cancelled():
                return
            exc = f.exception()
            if exc is not None:
                (on_error or self._llm_error)(exc)
            else:
                on_ok(f.result())
        future.add_done_callback(done)

    def _llm_error(self, exc: Exception) -> None:
        self.status_pane().write_error(f"LLM request failed: {type(exc).__name__}: {exc}")

    def _require_llm(self) -> None:
        if not self.llm_ready or self.llm is None:
            raise c.CommandError(f"LLM not available ({self.llm_status}).")

    def _llm_context(self, pane: ChatBuffer) -> list[Line]:
        return list(pane.history)[-self.cfg.llm.context_lines:]

    # --- suggestions ------------------------------------------------------------------

    def request_suggestions(self, pane: ChatBuffer, priority: Priority,
                            delay: float = 0.0) -> None:
        if not self.llm_ready or pane.kind == "status":
            return
        if priority == Priority.USER:
            self.show_bar(Text("Thinking of replies...", style="italic dim"))

        async def job() -> list[str]:
            client = self.clients[pane.network]
            lines = self._llm_context(pane)
            chan = client.state.channel(pane.buffer_name)
            return await features.suggest(
                self.llm, nick=client.state.nick, channel=pane.buffer_name,
                topic=chan.topic if chan else "", lines=lines,
                n=self.cfg.features.suggestion_count,
                draft=self.input.value.strip() if priority == Priority.USER else "",
                profiles=self._profiles_for(pane.network, lines))

        future = self.scheduler.submit(priority, job, key="suggest", delay=delay)

        def show(options: list[str]) -> None:
            if pane is self.active_buffer():
                self.show_suggestions(pane, options)

        def failed(exc: Exception) -> None:
            if priority == Priority.USER:
                self.show_bar(Text(f"Suggestion failed: {exc}", style="red"))
            else:
                self.clear_suggestions()

        self._when_done(future, show, failed)

    def _profiles_for(self, network: str, lines: list[Line]) -> dict[str, str]:
        if not self.cfg.features.profiles:
            return {}
        client = self.clients[network]
        speakers: list[str] = []
        for line in reversed(lines):
            if not client.state.is_me(line.nick) and line.nick not in speakers:
                speakers.append(line.nick)
            if len(speakers) == 4:
                break
        sketches = {}
        for nick in speakers:
            prof = self.store.get_profile(network, nick)
            if prof and prof.profile:
                lines_ = [ln.strip() for ln in prof.profile.splitlines() if ln.strip()]
                sketches[nick] = " ".join(lines_[:2])[:250]
        return sketches

    def show_bar(self, content: Text) -> None:
        self.bar.update(content)
        self.bar.add_class("visible")

    def show_suggestions(self, pane: ChatBuffer, options: list[str]) -> None:
        if not options:
            self.clear_suggestions()
            return
        self.suggestions = options
        self._suggest_cursor = -1
        self._suggest_pane = pane.id
        text = Text()
        for i, option in enumerate(options):
            if i:
                text.append("\n")
            text.append(f"F{i + 6} ", style="bold cyan")
            text.append(option)
        text.append("\nTab cycles · F6-F8 (or Alt+1-3) pick · Esc dismisses", style="dim")
        self.show_bar(text)

    def clear_suggestions(self) -> None:
        self.suggestions = []
        self._suggest_cursor = -1
        bar = getattr(self, "bar", None)
        if bar is None:
            return
        bar.update("")
        bar.remove_class("visible")

    def action_suggest_now(self) -> None:
        pane = self.active_buffer()
        if pane is None or pane.kind == "status":
            return
        if not self.llm_ready:
            self.show_bar(Text(f"LLM not available ({self.llm_status})", style="red"))
            return
        self.request_suggestions(pane, Priority.USER)

    def action_accept(self, index: int) -> None:
        if index >= len(self.suggestions):
            raise SkipAction()
        self._suggest_cursor = index
        self._put_input(self.suggestions[index])

    def action_tab_key(self) -> None:
        """Cycle suggestions when the input is empty or holds one; otherwise complete a nick."""
        if not self.input.has_focus:
            raise SkipAction()
        value = self.input.value
        if self.suggestions and (not value or value in self.suggestions):
            self._suggest_cursor = (self._suggest_cursor + 1) % len(self.suggestions)
            self._put_input(self.suggestions[self._suggest_cursor])
            return
        self.complete_nick()

    def complete_nick(self) -> None:
        pane = self.active_buffer()
        if pane is None or pane.kind != "channel":
            return
        value = self.input.value[:self.input.cursor_position]
        match = re.search(r"(\S+)$", value)
        if not match:
            return
        partial_nick = match.group(1).lower()
        chan = self.clients[pane.network].state.channel(pane.buffer_name)
        if chan is None:
            return
        # Prefer people who spoke recently.
        recent = [ln.nick for ln in reversed(pane.history)]
        candidates = sorted(
            (nick for nick, _ in chan.members.values() if nick.lower().startswith(partial_nick)),
            key=lambda n: (recent.index(n) if n in recent else len(recent), n.lower()))
        if not candidates:
            return
        nick = candidates[0]
        suffix = ": " if match.start() == 0 else " "
        new_value = value[:match.start()] + nick + suffix
        rest = self.input.value[self.input.cursor_position:]
        self.input.value = new_value + rest
        self.input.cursor_position = len(new_value)

    def _put_input(self, text: str) -> None:
        self.input.value = text
        self.input.cursor_position = len(text)
        self.input.focus()

    def action_dismiss_suggestions(self) -> None:
        if len(self.screen_stack) > 1 or (
                not self.suggestions and not self.bar.has_class("visible")):
            raise SkipAction()  # let Escape close modals / reach other widgets
        self.scheduler.cancel("suggest")
        self.clear_suggestions()

    # --- rephrase ---------------------------------------------------------------------

    def action_rephrase(self) -> None:
        self.rephrase("polite")

    def rephrase(self, style: str) -> None:
        pane = self.active_buffer()
        draft = self.input.value.strip()
        if pane is None:
            return
        if not draft:
            pane.write_error("Type a draft first, then Ctrl+R or /rephrase <style>.")
            return
        try:
            self._require_llm()
        except c.CommandError as exc:
            pane.write_error(str(exc))
            return
        self.show_bar(Text(f"Rephrasing ({style})...", style="italic dim"))
        lines = self._llm_context(pane)
        future = self.scheduler.submit(
            Priority.USER,
            lambda: features.rephrase(self.llm, draft=draft, style=style, lines=lines),
            key="rephrase")

        def done(result: str) -> None:
            self.clear_suggestions()
            if self.input.value.strip() == draft and result:
                self._rephrase_original = self.input.value
                self._put_input(result)
                self.show_bar(Text("Rephrased. Ctrl+Z restores your original.", style="dim"))
            elif result:
                self.show_suggestions(pane, [result])

        def failed(exc: Exception) -> None:
            self.show_bar(Text(f"Rephrase failed: {exc}", style="red"))

        self._when_done(future, done, failed)

    def action_undo_rephrase(self) -> None:
        if self._rephrase_original is None:
            raise SkipAction()
        self._put_input(self._rephrase_original)
        self._rephrase_original = None
        self.clear_suggestions()

    # --- catch-up summary -------------------------------------------------------------

    def action_summary(self) -> None:
        pane = self.active_buffer()
        if pane is None or pane.kind == "status":
            return
        try:
            self._require_llm()
        except c.CommandError as exc:
            pane.write_error(str(exc))
            return
        rows = self.store.recent(pane.network, pane.buffer_name, 200,
                                 after_id=pane.catchup_from)
        what = "messages since you last read this tab"
        if len(rows) < 5:
            rows = self.store.recent(pane.network, pane.buffer_name, 60)
            what = "recent messages"
        if not rows:
            pane.write_info("Nothing to summarize yet.")
            return
        lines = [Line(r.nick, r.text, r.kind, r.ts) for r in rows]
        pane.write_info(f"Summarizing {len(lines)} {what}...", style="italic cyan")
        nick = self.clients[pane.network].state.nick
        future = self.scheduler.submit(
            Priority.USER,
            lambda: features.summarize(self.llm, nick=nick, channel=pane.buffer_name,
                                       lines=lines),
            key=f"summary:{pane.id}")

        def done(summary: str) -> None:
            pane.write_info("── Catch-up summary ──", style="bold cyan")
            for text in summary.splitlines():
                if text.strip():
                    # Models answer in Markdown; the terminal shows it raw.
                    text = re.sub(r"^\s*[-*•]\s+", "• ", text.replace("**", ""))
                    pane.log_view.write(Text("   " + text.replace("`", ""), style="cyan"))
            pane.write_info("──────────────────────", style="bold cyan")

        self._when_done(future, done, lambda e: pane.write_error(f"Summary failed: {e}"))

    # --- translation ------------------------------------------------------------------

    def request_translation(self, pane: ChatBuffer, line: Line) -> None:
        target = self.cfg.features.translate_target
        future = self.scheduler.submit(
            Priority.TRANSLATE,
            lambda: features.translate(self.llm, text=line.text, target=target))

        def done(result: str | None) -> None:
            if result:
                pane.write_note(f"{line.nick}: {result}")

        self._when_done(future, done, lambda e: None)

    # --- mention triage ---------------------------------------------------------------

    def queue_triage(self, pane: ChatBuffer, line: Line) -> None:
        """Batch question-like lines from background tabs, then ask the LLM once."""
        queue = self._triage_queue.setdefault(pane.id, [])
        queue.append(line)
        if len(queue) > 1:
            return  # a job is already waiting for this tab and will take this line too

        async def job() -> tuple[list[Line], list[int]]:
            candidates = self._triage_queue.pop(pane.id, [])[-10:]
            if not candidates:
                return [], []
            history = list(pane.history)
            first = history.index(candidates[0]) if candidates[0] in history else len(history)
            context = history[max(0, first - 8):first]
            nick = self.clients[pane.network].state.nick
            return candidates, await features.triage(self.llm, nick=nick, context=context,
                                                     candidates=candidates)

        future = self.scheduler.submit(Priority.TRIAGE, job, delay=4.0)

        def done(result: tuple[list[Line], list[int]]) -> None:
            candidates, picked = result
            if not picked or pane is self.active_buffer():
                return
            for i in picked:
                pane.write_note(f"{candidates[i].nick} may need your reply: "
                                f"{candidates[i].text}", style="bold #e5c07b")
            pane.highlight = True
            self.refresh_label(pane)
            self.bell()

        self._when_done(future, done, lambda e: None)

    # --- user profiles ----------------------------------------------------------------

    def maybe_update_profile(self, network: str, nick: str) -> None:
        prof = self.store.bump_profile(network, nick)
        due = prof.message_count - prof.analyzed_count >= self.cfg.features.profile_batch
        if due and self.llm_ready and not self.scheduler.has(self._profile_key(network, nick)):
            self._when_done(self.submit_profile(network, nick, Priority.PROFILE),
                            lambda _: None, lambda e: None)

    def _profile_key(self, network: str, nick: str) -> str:
        return f"profile:{network}:{irc_lower(nick)}"

    def submit_profile(self, network: str, nick: str, priority: Priority) -> asyncio.Future:
        async def job() -> str:
            prof = self.store.get_profile(network, nick)
            new = prof.message_count - prof.analyzed_count
            lines = [Line(r.nick, r.text, r.kind, r.ts)
                     for r in self.store.messages_by(network, nick, min(max(new, 10), 80))]
            text = await features.update_profile(self.llm, nick=prof.nick,
                                                 previous=prof.profile, lines=lines)
            self.store.save_profile(network, nick, text, prof.message_count)
            return text
        return self.scheduler.submit(priority, job, key=self._profile_key(network, nick))

    def show_profile(self, network: str, nick: str) -> None:
        prof = self.store.get_profile(network, nick)
        if prof is not None and prof.profile:
            self.push_screen(ProfileScreen(nick, prof))
            return
        if prof is None or prof.message_count < 5:
            self.push_screen(ProfileScreen(nick, prof, "Not enough messages yet (need 5)."))
            return
        self._require_llm()
        pane = self.active_buffer()
        if pane is not None:
            pane.write_info(f"Building a profile of {nick} from "
                            f"{prof.message_count} messages...", style="italic cyan")
        self.scheduler.cancel(self._profile_key(network, nick))
        self._when_done(
            self.submit_profile(network, nick, Priority.USER),
            lambda _: self.push_screen(
                ProfileScreen(nick, self.store.get_profile(network, nick))),
            lambda e: pane and pane.write_error(f"Profile failed: {e}"))
