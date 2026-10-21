"""Slash-command parsing. Pure functions, so they're easy to test."""
from __future__ import annotations

from dataclasses import dataclass

from ..irc.state import is_channel

HELP = {
    "join": "/join #channel [key]  - join a channel",
    "part": "/part [#channel] [reason]  - leave a channel (default: current)",
    "msg": "/msg nick text  - send a private message",
    "query": "/query nick  - open a private chat tab",
    "me": "/me action  - send an action",
    "nick": "/nick newnick  - change nickname",
    "topic": "/topic [text]  - show or set the channel topic",
    "close": "/close  - close the current tab (parts channels)",
    "away": "/away [message]  - mark yourself away (no message: back)",
    "whois": "/whois [nick]  - show who someone is (default: this query's nick)",
    "ignore": "/ignore [nick]  - hide and don't store someone's messages (no nick: list)",
    "unignore": "/unignore nick  - stop ignoring someone",
    "auth": "/auth  - retry the services login (Q or NickServ) from the config",
    "raw": "/raw LINE  - send a raw IRC line",
    "quit": "/quit [reason]  - disconnect and exit",
    "suggest": "/suggest  - ask the LLM for reply suggestions now (Ctrl+Space)",
    "summary": "/summary  - summarize unread messages in this tab",
    "rephrase": "/rephrase [polite|concise|casual|formal|lang:<language>]  - rewrite your draft (Ctrl+R)",
    "translate": "/translate [on|off]  - toggle translating foreign messages in this tab",
    "search": "/search [#channel|-all] words  - find logged messages (default: this tab)",
    "ask": "/ask [#channel|-all] question  - answer a question from the logged history",
    "profile": "/profile nick  - show the LLM's profile of a user",
    "forget": "/forget nick  - delete a stored user profile",
    "stats": "/stats  - how often you use the reply suggestions",
    "llm": "/llm  - reconnect to LM Studio",
    "help": "/help [command]  - list commands",
}

ALIASES = {"j": "join", "leave": "part", "q": "query", "m": "msg", "exit": "quit", "wc": "close"}


@dataclass
class Command:
    name: str
    args: list[str]
    rest: str  # everything after the command name, unsplit


class CommandError(ValueError):
    pass


def parse_input(line: str) -> Command | None:
    """Return a Command for '/...' lines, or None for plain text.

    '//text' sends a literal message starting with '/'.
    """
    if not line.startswith("/") or line.startswith("//"):
        return None
    name, _, rest = line[1:].partition(" ")
    name = name.lower()
    name = ALIASES.get(name, name)
    if not name:
        raise CommandError("empty command")
    return Command(name, rest.split(), rest.strip())


def plain_text(line: str) -> str:
    return line[1:] if line.startswith("//") else line


def require(cmd: Command, count: int) -> None:
    if len(cmd.args) < count:
        raise CommandError(f"usage: {HELP.get(cmd.name, '/' + cmd.name)}")


def split_first(rest: str) -> tuple[str, str]:
    """'nick some text' -> ('nick', 'some text')."""
    first, _, remainder = rest.partition(" ")
    return first, remainder.strip()


def split_scope(rest: str, default: str | None) -> tuple[str | None, str]:
    """'-all words' -> (None, 'words'); '#chan words' -> ('#chan', 'words'); else default."""
    first, remainder = split_first(rest)
    if first == "-all":
        return None, remainder
    if is_channel(first) and len(first) > 1:
        return first, remainder
    return default, rest.strip()
