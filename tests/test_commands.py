import pytest

from llm_irc.ui import commands as c


def test_plain_text_is_not_a_command():
    assert c.parse_input("hello") is None
    assert c.parse_input("//etc/passwd is a path") is None
    assert c.plain_text("//etc") == "/etc"


def test_parse_command_and_alias():
    cmd = c.parse_input("/J #python key")
    assert cmd.name == "join" and cmd.args == ["#python", "key"]
    cmd = c.parse_input("/msg bob hi there")
    assert c.split_first(cmd.rest) == ("bob", "hi there")


def test_require():
    with pytest.raises(c.CommandError):
        c.require(c.parse_input("/join"), 1)


def test_every_command_has_help():
    for alias, target in c.ALIASES.items():
        assert target in c.HELP


def test_split_scope():
    assert c.split_scope("-all foo bar", "#here") == (None, "foo bar")
    assert c.split_scope("#chan foo", None) == ("#chan", "foo")
    assert c.split_scope("foo bar", "#here") == ("#here", "foo bar")
    assert c.split_scope("# foo", "#here") == ("#here", "# foo")
    assert c.split_scope("", None) == (None, "")
