"""IRCClient login flows against the fake server (no UI)."""
import asyncio

import pytest

from llm_irc.config import NetworkConfig
from llm_irc.irc import client as ev
from llm_irc.irc.client import IRCClient
from tests.fake_ircd import FakeIRCd

PASSWORD = "s3cret-password!"


@pytest.fixture
async def ircd():
    server = FakeIRCd()
    port = await server.start()
    server.q_accounts["myaccount"] = PASSWORD
    yield server, port
    await server.stop()


async def run_client(network: NetworkConfig, until, timeout=5.0):
    events: list[ev.Event] = []
    client = IRCClient(network, events.append)
    task = asyncio.create_task(client.run())
    try:
        for _ in range(int(timeout / 0.02)):
            if until(events):
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError(f"timed out; events: {events}")
    finally:
        await client.quit()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return client, events


def joined(events):
    return any(isinstance(e, ev.Joined) and e.own for e in events)


def network(port, **kw):
    return NetworkConfig(name="qnet", host="127.0.0.1", port=port, tls=False, nick="me",
                         channels=["#chan"], auth="q", auth_username="myaccount",
                         auth_password_env="TEST_Q_PASS", **kw)


async def test_q_challengeauth_then_hidden_host_then_join(ircd, monkeypatch):
    server, port = ircd
    monkeypatch.setenv("TEST_Q_PASS", PASSWORD)
    client, events = await run_client(network(port, hide_host=True), joined)

    assert client.account == "myaccount"
    texts = [e.text for e in events if isinstance(e, ev.Status)]
    assert "Logged in to Q as myaccount" in texts
    assert any("hidden as myaccount.users.quakenet.org" in t for t in texts)
    # The password itself never crosses the wire, and no JOIN goes out before +x applies.
    assert not any(PASSWORD in line for line in server.received)
    order = [line.split()[0] for line in server.received]
    assert order.index("JOIN") > order.index("MODE")
    assert any(line.startswith("PRIVMSG Q@CServe.quakenet.org :CHALLENGEAUTH myaccount ")
               for line in server.received)


async def test_q_wrong_password_still_joins(ircd, monkeypatch):
    server, port = ircd
    monkeypatch.setenv("TEST_Q_PASS", "wrong")
    client, events = await run_client(network(port, hide_host=True), joined)
    assert client.account is None
    assert any(isinstance(e, ev.Status) and e.error and "Q login failed" in e.text
               for e in events)


async def test_missing_password_env_reports_and_joins(ircd, monkeypatch):
    server, port = ircd
    monkeypatch.delenv("TEST_Q_PASS", raising=False)
    client, events = await run_client(network(port), joined)
    assert any(isinstance(e, ev.Status) and "TEST_Q_PASS" in e.text for e in events)
    assert not any("CHALLENGE" in line for line in server.received)
