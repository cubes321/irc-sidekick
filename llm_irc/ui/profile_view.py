"""Modal showing what the LLM has learned about a user."""
from __future__ import annotations

import time

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from ..store.db import Profile


class ProfileScreen(ModalScreen[None]):
    BINDINGS = [("escape", "dismiss", "Close"), ("q", "dismiss", "Close")]
    DEFAULT_CSS = """
    ProfileScreen { align: center middle; }
    ProfileScreen > Vertical {
        width: 76; max-width: 95%; height: auto; max-height: 90%;
        border: thick $primary; background: $panel; padding: 1 2;
    }
    ProfileScreen .meta { color: $text-muted; margin-bottom: 1; }
    ProfileScreen .body { height: auto; }
    ProfileScreen .hint { color: $text-muted; margin-top: 1; }
    """

    def __init__(self, nick: str, profile: Profile | None, note: str = "") -> None:
        super().__init__()
        self.nick = nick
        self.profile = profile
        self.note = note

    def compose(self) -> ComposeResult:
        p = self.profile
        with Vertical():
            yield Static(Text(f"Profile: {self.nick}", style="bold"))
            if p is None:
                meta = "No messages from this user have been seen yet."
            else:
                updated = (time.strftime("%Y-%m-%d %H:%M", time.localtime(p.updated))
                           if p.updated else "never")
                first = time.strftime("%Y-%m-%d", time.localtime(p.first_seen))
                meta = (f"{p.message_count} messages seen since {first} · "
                        f"profile covers {p.analyzed_count} · updated {updated}")
            yield Static(meta, classes="meta")
            body = (p.profile if p and p.profile else
                    self.note or "Not enough messages yet for a profile.")
            yield Static(Text(body), classes="body")
            yield Static("Built by your local LLM from public channel messages; stored only "
                         "in your local database. /forget nick deletes it. Esc to close.",
                         classes="hint")
