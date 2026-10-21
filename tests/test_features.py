import re

from llm_irc.llm import features as f
from llm_irc.llm.provider import strip_reasoning


def test_parse_options_handles_list_styles():
    answer = """Here are some replies:
1. sure, sounds good
2) "what version are you on?"
- me: lol same
"""
    assert f.parse_options(answer, "me", 3) == [
        "sure, sounds good", "what version are you on?", "lol same"]


def test_parse_options_limits_and_dedupes():
    assert f.parse_options("1. a\n2. a\n3. b\n4. c", "x", 2) == ["a", "b"]


def test_strip_reasoning():
    assert strip_reasoning("<think>hmm</think>\n1. hi") == "1. hi"
    assert strip_reasoning("<think>never closed") == ""


def test_mentions_whole_word():
    assert f.mentions("hey Bob, you there?", "bob", [])
    assert not f.mentions("bobby is here", "bob", [])
    assert f.mentions("the deploy failed", "me", ["deploy"])


def test_looks_foreign():
    assert not f.looks_foreign("is anyone here using the new release?", "English")
    assert f.looks_foreign("kann mir jemand bitte helfen", "English")
    assert f.looks_foreign("こんにちは みなさん", "English")
    assert not f.looks_foreign("ok", "English")
    assert f.looks_foreign("hola a todos, ¿qué tal está la gente?", "English")
    assert not f.looks_foreign("I am not sure that is the case", "English")


def test_looks_like_question():
    assert f.looks_like_question("anyone know how to fix this")
    assert f.looks_like_question("it broke?")
    assert not f.looks_like_question("I fixed it.")


def test_style_examples_filters_and_limits():
    texts = ["ok", "lol yeah same", "LOL YEAH SAME", "already in the log", "x" * 200 + " y",
             "honestly no idea mate", "one more line"]
    picked = f.style_examples(texts, 3, exclude=["Already in the log"])
    assert picked == ["lol yeah same", ("x" * 200 + " y")[:120], "honestly no idea mate"]
    assert f.style_examples(texts, 0) == []


class CapturingLLM:
    def __init__(self):
        self.user = ""

    async def chat(self, system, user, **kwargs):
        self.user = user
        return "1. a\n2. b"


async def test_suggest_includes_style_examples():
    llm = CapturingLLM()
    lines = [f.Line("bob", "the build broke?")]
    await f.suggest(llm, nick="me", channel="#c", topic="", lines=lines, n=2,
                    style=["lol yeah same"])
    assert "How me usually writes" in llm.user
    assert "- lol yeah same" in llm.user
    await f.suggest(llm, nick="me", channel="#c", topic="", lines=lines, n=2)
    assert "usually writes" not in llm.user


def test_keywords():
    assert f.keywords("when is the release for py 3.12?") == ["release", "3", "12"]
    assert f.keywords("did anyone mention the deploy script?") == ["deploy", "script"]
    assert f.keywords("what did they say?") == []


def test_render_with_dates():
    line = f.Line("bob", "hi", ts=1_700_000_000)
    assert re.match(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\] <bob> hi", line.render(dates=True))
    assert re.match(r"\[\d{2}:\d{2}\] <bob> hi", line.render())


async def test_ask_prompt_has_dated_excerpts():
    llm = CapturingLLM()
    await f.ask(llm, nick="me", question="when is the release?",
                excerpts=[("#home", [f.Line("bob", "friday", ts=1_700_000_000)])])
    assert "--- #home ---" in llm.user
    assert re.search(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\] <bob> friday", llm.user)
    assert "when is the release?" in llm.user
