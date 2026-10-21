"""Modal listing every key and slash command (F1)."""
from __future__ import annotations

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from . import commands as c


def _grid(rows: list[tuple[str, str]], first_width: int) -> Table:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold cyan", max_width=first_width, overflow="fold")
    table.add_column()
    for left, right in rows:
        # Text, not str: Rich would read "[#channel]" as a markup tag and drop it.
        table.add_row(Text(left), Text(right))
    return table


class HelpScreen(ModalScreen[None]):
    # F1 also closes it, via IRCApp.action_show_help.
    BINDINGS = [("escape", "dismiss", "Close"), ("q", "dismiss", "Close")]
    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    HelpScreen > VerticalScroll {
        width: 100; max-width: 95%; height: auto; max-height: 90%;
        border: thick $primary; background: $panel; padding: 1 2;
    }
    HelpScreen .heading { text-style: bold; margin-top: 1; }
    HelpScreen .hint { color: $text-muted; margin-top: 1; }
    """

    def compose(self) -> ComposeResult:
        commands = [c.usage_and_description(text) for text in c.HELP.values()]
        aliases = ", ".join(f"/{alias} = /{name}" for alias, name in c.ALIASES.items())
        with VerticalScroll():
            yield Static(Text("Help", style="bold"))
            yield Static("Keys", classes="heading")
            yield Static(_grid(c.KEYS, 22))
            yield Static("Commands", classes="heading")
            yield Static(_grid(commands, 34))
            yield Static(f"Aliases: {aliases}. Start a line with // to send a "
                         "message that begins with /.", classes="hint")
            yield Static("↑/↓ or PgUp/PgDn to scroll · Esc or F1 to close · "
                         "/help command shows one command in the chat.", classes="hint")
