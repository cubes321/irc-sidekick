"""End-to-end: the Textual app against the fake IRC server and a fake LLM."""
import asyncio

import pytest
from textual import events

from llm_irc.config import Config, FeatureConfig, LLMConfig, NetworkConfig
from llm_irc.ui import app as app_module
from llm_irc.ui.app import IRCApp
from llm_irc.ui.buffer import ChatBuffer
from llm_irc.ui.profile_view import ProfileScreen
from tests.fake_ircd import FakeIRCd


class FakeLLM:
    def __init__(self, cfg):
        self.calls: list[str] = []
        self.prompts: list[str] = []

    async def discover(self):
        return "fake-model"

    async def chat(self, system, user, **kwargs):
        self.calls.append(system)
        self.prompts.append(user)
        if "propose" in system:
            return "1. sounds good\n2. what happened?\n3. lol"
        if "rewrite" in system:
            return "Could you please help me?"
        if "summarize" in system:
            return "- bob asked about the release"
        if "personality" in system:
            return "Summary: chatty tester\nTone: friendly"
        if "Translate" in system:
            return "Hello everyone"
        return "none"

    async def close(self):
        pass


@pytest.fixture
async def ircd():
    server = FakeIRCd()
    port = await server.start()
    yield server, port
    await server.stop()


def make_config(port, tmp_path):
    return Config(
        networks=[NetworkConfig(name="fake", host="127.0.0.1", port=port, tls=False,
                                nick="me", channels=["#home"])],
        llm=LLMConfig(suggest_debounce_s=0.05),
        features=FeatureConfig(profile_batch=3),
        db_path=str(tmp_path / "test.sqlite3"),
    )


async def wait_for(pilot, condition, timeout=5.0):
    for _ in range(int(timeout / 0.05)):
        if condition():
            return
        await pilot.pause(0.05)
    raise AssertionError("condition not met in time")


def log_text(pane: ChatBuffer) -> str:
    return "\n".join(strip.text for strip in pane.log_view.lines)


async def type_line(pilot, app, text):
    app.input.value = text
    await pilot.press("enter")


async def test_join_chat_suggest_part(ircd, tmp_path, monkeypatch):
    server, port = ircd
    monkeypatch.setattr(app_module, "LLMProvider", FakeLLM)
    app = IRCApp(make_config(port, tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        # Autojoin creates and selects the channel tab.
        await wait_for(pilot, lambda: app.find("fake", "#home") is not None
                       and app.find("fake", "#home").joined)
        assert app.llm_ready

        # /join opens and focuses a second tab.
        app.tabs.active = app.find("fake", "#home").id
        await pilot.pause(0.1)
        await type_line(pilot, app, "/join #second")
        await wait_for(pilot, lambda: app.find("fake", "#second") is not None
                       and app.active_buffer() is app.find("fake", "#second"))

        # Someone talks in the active tab -> auto-suggestions appear.
        server.inject(":bob!b@h JOIN #second")
        server.inject(":bob!b@h PRIVMSG #second :hey me, the build broke?")
        await wait_for(pilot, lambda: app.suggestions)
        assert app.suggestions[0] == "sounds good"

        # Tab puts the first suggestion into the input; nothing is sent yet.
        await pilot.press("f7")
        assert app.input.value == "what happened?"
        await pilot.press("alt+3")
        assert app.input.value == "lol"
        app.input.value = ""
        await pilot.press("tab")
        assert app.input.value == "sounds good"
        await pilot.press("enter")
        await wait_for(pilot, lambda: any("PRIVMSG #second :sounds good" in line
                                          for line in server.received))

        # Background tab gets an unread count and a highlight on mention.
        server.inject(":bob!b@h PRIVMSG #home :me: are you there")
        home = app.find("fake", "#home")
        await wait_for(pilot, lambda: home.unread == 1 and home.highlight)

        # Rephrase replaces the draft; Ctrl+Z restores it.
        app.input.value = "help me"
        await pilot.press("ctrl+r")
        await wait_for(pilot, lambda: app.input.value == "Could you please help me?")
        await pilot.press("ctrl+z")
        assert app.input.value == "help me"
        app.input.value = ""

        # Profiles build after profile_batch messages.
        for i in range(3):
            server.inject(f":bob!b@h PRIVMSG #second :message {i}")
        await wait_for(pilot, lambda: (p := app.store.get_profile("fake", "bob")) is not None
                       and "chatty tester" in p.profile)

        # The profile modal stays usable while IRC traffic keeps arriving.
        await type_line(pilot, app, "/profile bob")
        await wait_for(pilot, lambda: isinstance(app.screen, ProfileScreen))
        second = app.find("fake", "#second")
        before = len(second.history)
        server.inject(":bob!b@h PRIVMSG #second :still here?")
        await wait_for(pilot, lambda: len(second.history) == before + 1)
        assert isinstance(app.screen, ProfileScreen)
        await pilot.press("escape")
        await wait_for(pilot, lambda: not isinstance(app.screen, ProfileScreen))
        await pilot.pause(0.1)

        # /part leaves; /close removes the tab.
        await type_line(pilot, app, "/part")
        second = app.find("fake", "#second")
        await wait_for(pilot, lambda: not second.joined)
        await type_line(pilot, app, "/close")
        await wait_for(pilot, lambda: app.find("fake", "#second") is None)


async def test_style_examples_and_suggestion_feedback(ircd, tmp_path, monkeypatch):
    server, port = ircd
    monkeypatch.setattr(app_module, "LLMProvider", FakeLLM)
    app = IRCApp(make_config(port, tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(pilot, lambda: app.find("fake", "#home") is not None
                       and app.find("fake", "#home").joined)
        app.tabs.active = app.find("fake", "#home").id
        await pilot.pause(0.1)
        await type_line(pilot, app, "honestly no idea mate")
        await type_line(pilot, app, "/join #second")
        await wait_for(pilot, lambda: app.find("fake", "#second") is not None
                       and app.active_buffer() is app.find("fake", "#second"))

        def events():
            return app.store.db.execute(
                "SELECT outcome, message_id FROM suggestion_events ORDER BY id").fetchall()

        async def bob_asks(text):
            server.inject(f":bob!b@h PRIVMSG #second :{text}")
            await wait_for(pilot, lambda: app.suggestions)

        # Your own line from #home reaches the prompt as a style example.
        server.inject(":bob!b@h JOIN #second")
        await bob_asks("hey me, the build broke?")
        suggest_prompts = [p for p in app.llm.prompts if "Suggest" in p]
        assert "- honestly no idea mate" in suggest_prompts[-1]

        await pilot.press("f6")
        await pilot.press("enter")
        await wait_for(pilot, lambda: len(events()) == 1)
        sent_id = app.store.recent("fake", "#second", 1)[-1].id
        assert events()[-1] == ("picked", sent_id)
        # The LLM-written line doesn't become a style example itself.
        assert "sounds good" not in [m.text for m in app.store.own_messages("fake", 10)]

        await bob_asks("anyone?")
        await pilot.press("escape")
        assert events()[-1] == ("dismissed", None)

        await bob_asks("still broken")
        await pilot.press("f6")
        app.input.value += " ok"
        await pilot.press("enter")
        await wait_for(pilot, lambda: events()[-1][0] == "edited")

        await bob_asks("hello?")
        await type_line(pilot, app, "my own words")
        await wait_for(pilot, lambda: events()[-1] == ("ignored", None))

        await type_line(pilot, app, "/stats")
        await wait_for(pilot, lambda: "1 picked, 1 edited, 1 dismissed, 1 ignored"
                       in log_text(app.find("fake", "#second")))


async def test_query_and_translate(ircd, tmp_path, monkeypatch):
    server, port = ircd
    monkeypatch.setattr(app_module, "LLMProvider", FakeLLM)
    app = IRCApp(make_config(port, tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(pilot, lambda: app.find("fake", "#home") is not None
                       and app.find("fake", "#home").joined)
        home = app.find("fake", "#home")
        app.tabs.active = home.id
        await pilot.pause(0.1)

        await type_line(pilot, app, "/translate on")
        assert home.translate
        server.inject(":carla!c@h PRIVMSG #home :hola a todos, ¿qué tal está la gente?")
        await wait_for(pilot, lambda: "Hello everyone" in log_text(home))

        # An incoming private message opens a query tab.
        server.inject(":dave!d@h PRIVMSG me :psst")
        await wait_for(pilot, lambda: app.find("fake", "dave") is not None)
        assert app.find("fake", "dave").kind == "query"

        await type_line(pilot, app, "/bogus")
        await wait_for(pilot, lambda: "Unknown command" in log_text(home))


async def test_terminal_window_events_dont_crash(ircd, tmp_path, monkeypatch):
    """Real terminals send mouse enter/leave and focus events that headless runs don't.

    Regression: a method named `_on_leave` was dispatched as the Leave event handler.
    """
    server, port = ircd
    monkeypatch.setattr(app_module, "LLMProvider", FakeLLM)
    app = IRCApp(make_config(port, tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(pilot, lambda: app.find("fake", "#home") is not None)
        for event in (events.Enter(app), events.Leave(app), events.AppBlur(), events.AppFocus()):
            app.post_message(event)
            await pilot.pause(0.05)
        assert app.is_running
        assert app._exception is None
