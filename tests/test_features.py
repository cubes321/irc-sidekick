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
