"""A tiny IRC server for tests and local smoke runs: python -m tests.fake_ircd [port]

Supports registration, JOIN/PART/PRIVMSG/NICK/QUIT/PING/TOPIC between connected
clients. `inject(line)` sends a raw line to every client, for simulating other users.
"""
from __future__ import annotations

import asyncio
import secrets
import sys

from llm_irc.irc import message as m
from llm_irc.irc.auth import challengeauth_response
from llm_irc.irc.state import irc_lower

SERVER = "fake.irc"


class Conn:
    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.nick = "*"
        self.challenge: str | None = None
        self.account: str | None = None
        self.wants_x = False

    @property
    def prefix(self) -> str:
        return f"{self.nick}!{self.nick}@localhost"

    def send(self, line: str) -> None:
        self.writer.write(f"{line}\r\n".encode())


class FakeIRCd:
    def __init__(self) -> None:
        self.conns: list[Conn] = []
        self.channels: dict[str, set[Conn]] = {}
        self.topics: dict[str, str] = {}
        self.received: list[str] = []
        self.q_accounts: dict[str, str] = {}  # QuakeNet-style Q accounts: name -> password
        self.server: asyncio.base_events.Server | None = None

    async def start(self, port: int = 0) -> int:
        self.server = await asyncio.start_server(self._client, "127.0.0.1", port)
        return self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for conn in self.conns:
            conn.writer.close()
        if self.server:
            self.server.close()

    def inject(self, line: str) -> None:
        for conn in self.conns:
            conn.send(line)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = Conn(writer)
        self.conns.append(conn)
        try:
            while raw := await reader.readline():
                line = raw.decode().rstrip("\r\n")
                self.received.append(line)
                self._handle(conn, m.parse(line))
                await writer.drain()
        except (ConnectionError, ValueError):
            pass
        finally:
            self.conns.remove(conn)
            for members in self.channels.values():
                members.discard(conn)

    def _handle(self, conn: Conn, msg: m.Message) -> None:
        cmd, p = msg.command, msg.params
        if cmd == "NICK":
            if any(c is not conn and irc_lower(c.nick) == irc_lower(p[0]) for c in self.conns):
                conn.send(f":{SERVER} 433 {conn.nick} {p[0]} :Nickname is already in use")
                return
            old = conn.prefix
            first = conn.nick == "*"
            conn.nick = p[0]
            if not first:
                conn.send(f":{old} NICK :{conn.nick}")
        elif cmd == "USER":
            conn.send(f":{SERVER} 001 {conn.nick} :Welcome to the fake network {conn.nick}")
        elif cmd == "PING":
            conn.send(f":{SERVER} PONG {SERVER} :{p[0]}")
        elif cmd == "JOIN":
            chan = p[0]
            members = self.channels.setdefault(irc_lower(chan), set())
            members.add(conn)
            for c in members:
                c.send(f":{conn.prefix} JOIN {chan}")
            if topic := self.topics.get(irc_lower(chan)):
                conn.send(f":{SERVER} 332 {conn.nick} {chan} :{topic}")
            names = " ".join(c.nick for c in members)
            conn.send(f":{SERVER} 353 {conn.nick} = {chan} :{names}")
            conn.send(f":{SERVER} 366 {conn.nick} {chan} :End of /NAMES list.")
        elif cmd == "PART":
            chan = p[0]
            members = self.channels.get(irc_lower(chan), set())
            for c in members:
                c.send(f":{conn.prefix} PART {chan}" + (f" :{p[1]}" if len(p) > 1 else ""))
            members.discard(conn)
        elif cmd == "TOPIC" and len(p) > 1:
            self.topics[irc_lower(p[0])] = p[1]
            for c in self.channels.get(irc_lower(p[0]), set()):
                c.send(f":{conn.prefix} TOPIC {p[0]} :{p[1]}")
        elif cmd == "MODE" and len(p) > 1 and p[1] == "+x":
            conn.wants_x = True
            self._maybe_hide(conn)
        elif cmd == "PRIVMSG" and p[0].lower() == "q@cserve.quakenet.org":
            self._q(conn, p[1])
        elif cmd in ("PRIVMSG", "NOTICE"):
            target, text = p[0], p[1]
            if target.startswith("#"):
                recipients = self.channels.get(irc_lower(target), set()) - {conn}
            else:
                recipients = {c for c in self.conns if irc_lower(c.nick) == irc_lower(target)}
            for c in recipients:
                c.send(f":{conn.prefix} {cmd} {target} :{text}")
        elif cmd == "QUIT":
            conn.writer.close()


    def _q(self, conn: Conn, text: str) -> None:
        """A minimal QuakeNet Q: CHALLENGE / CHALLENGEAUTH only."""
        def reply(line: str) -> None:
            conn.send(f":Q!TheQBot@CServe.quakenet.org NOTICE {conn.nick} :{line}")
        parts = text.split()
        if parts[:1] == ["CHALLENGE"]:
            conn.challenge = secrets.token_hex(16)
            reply(f"CHALLENGE {conn.challenge} HMAC-MD5 HMAC-SHA-1 HMAC-SHA-256 LEGACY-MD5")
        elif parts[:1] == ["CHALLENGEAUTH"] and len(parts) == 4 and conn.challenge:
            _, user, response, algorithm = parts
            password = self.q_accounts.get(user.lower())
            expected = (challengeauth_response(user, password, conn.challenge, algorithm)
                        if password else None)
            conn.challenge = None
            if response == expected:
                conn.account = user
                reply(f"You are now logged in as {user}.")
                self._maybe_hide(conn)
            else:
                reply("Username or password incorrect.")
        else:
            reply("Unknown command.")

    def _maybe_hide(self, conn: Conn) -> None:
        if conn.wants_x and conn.account:
            conn.send(f":{SERVER} 396 {conn.nick} {conn.account}.users.quakenet.org "
                      ":is now your hidden host")


async def _main(port: int) -> None:
    ircd = FakeIRCd()
    port = await ircd.start(port)
    print(f"fake ircd listening on 127.0.0.1:{port}")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(_main(int(sys.argv[1]) if len(sys.argv) > 1 else 6667))
