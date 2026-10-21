"""A chat tab: message log, optional member list, and per-tab state."""
from __future__ import annotations

import time
import zlib
from collections import deque

from rich.text import Text
from textual.containers import Horizontal
from textual.widgets import RichLog, Static, TabPane

from ..llm.features import Line

NICK_COLORS = ["#e06c75", "#98c379", "#e5c07b", "#61afef", "#c678dd", "#56b6c2",
               "#d19a66", "#be5046", "#7ec699", "#f08d49", "#a9a1e1", "#4fb8cc"]

HISTORY = 300  # lines kept in memory per tab for LLM context


def nick_style(nick: str) -> str:
    return NICK_COLORS[zlib.crc32(nick.lower().encode()) % len(NICK_COLORS)]


class ChatBuffer(TabPane):
    """One tab. kind is 'status', 'channel' or 'query'."""

    DEFAULT_CSS = """
    ChatBuffer { padding: 0; }
    ChatBuffer > Horizontal { height: 1fr; }
    ChatBuffer RichLog { width: 1fr; background: $surface; scrollbar-size-vertical: 1; }
    ChatBuffer .members {
        width: 20; border-left: solid $primary-background; padding: 0 1;
        color: $text-muted; overflow-y: auto;
    }
    ChatBuffer .topic { height: auto; max-height: 2; color: $text-muted; background: $boost;
        padding: 0 1; }
    """

    def __init__(self, network: str, name: str, kind: str, pane_id: str) -> None:
        super().__init__(name, id=pane_id)
        self.network = network
        self.buffer_name = name
        self.kind = kind
        self.unread = 0
        self.highlight = False
        self.translate = False
        self.catchup_from = 0     # db id of the last message read before this visit
        self.last_db_id = 0       # db id of the newest message in this tab
        self.joined = kind != "channel"
        self.history: deque[Line] = deque(maxlen=HISTORY)
        self.log_view = RichLog(wrap=True, markup=False, auto_scroll=True, min_width=20)
        self.topic_view = Static("", classes="topic")
        self.members_view = Static("", classes="members")
        self.topic_view.display = False
        self.members_view.display = kind == "channel"

    def compose(self):
        yield self.topic_view
        with Horizontal():
            yield self.log_view
            yield self.members_view

    # --- rendering ---

    def _stamp(self, ts: float | None = None) -> Text:
        return Text(time.strftime("%H:%M ", time.localtime(ts or time.time())), style="dim")

    def write_chat(self, nick: str, text: str, kind: str, *, own: bool,
                   highlight: bool = False, ts: float | None = None) -> None:
        line = self._stamp(ts)
        color = "bold" if own else nick_style(nick)
        if kind == "action":
            line.append(f"* {nick} ", style=color)
            line.append(text, style="italic")
        elif kind == "notice":
            line.append(f"-{nick}- ", style=color)
            line.append(text)
        else:
            line.append(f"<{nick}> ", style=color)
            line.append(text)
        if highlight:
            line.stylize("on #4a2b2b")
        self.log_view.write(line)

    def write_info(self, text: str, *, style: str = "dim", ts: float | None = None) -> None:
        line = self._stamp(ts)
        line.append(text, style=style)
        self.log_view.write(line)

    def write_error(self, text: str) -> None:
        self.write_info(f"! {text}", style="bold red")

    def write_found(self, ts: float, where: str, nick: str, text: str, kind: str,
                    words: list[str]) -> None:
        """A search hit: full date, the tab it came from, matched words highlighted."""
        line = Text(time.strftime("%Y-%m-%d %H:%M ", time.localtime(ts)), style="dim")
        if where:
            line.append(f"{where} ", style="dim italic")
        body = Text(text, style="italic" if kind == "action" else "")
        body.highlight_words(words, "bold reverse", case_sensitive=False)
        frame = {"action": "* {} ", "notice": "-{}- "}.get(kind, "<{}> ")
        line.append(frame.format(nick), style=nick_style(nick))
        line.append_text(body)
        self.log_view.write(line)

    def write_note(self, text: str, style: str = "italic #8a9bb0") -> None:
        """Indented annotation for the line above (translations, triage)."""
        self.log_view.write(Text(f"      ↳ {text}", style=style))

    def set_topic(self, topic: str) -> None:
        self.topic_view.update(Text(topic))
        self.topic_view.display = bool(topic)

    def set_members(self, members: list[tuple[str, str]]) -> None:
        text = Text(f"{len(members)} users\n", style="bold")
        for nick, prefix in members:
            text.append(prefix or " ", style="bold green" if prefix else "")
            text.append(nick + "\n", style=nick_style(nick))
        self.members_view.update(text)

    # --- tab label ---

    def tab_label(self) -> Text:
        text = Text(self.buffer_name)
        if self.kind == "channel" and not self.joined:
            text.stylize("strike dim")
        if self.unread:
            text.append(f" {self.unread}", style="bold yellow" if not self.highlight else "")
        if self.highlight:
            text.stylize("bold reverse red")
        return text
