import pytest

from llm_irc.irc import message as m
from llm_irc.irc.state import ChannelState, ConnectionState, irc_lower


def test_parse_privmsg_with_prefix_and_trailing():
    msg = m.parse(":alice!a@host PRIVMSG #chan :hello there :)\r\n")
    assert msg.prefix == "alice!a@host"
    assert msg.nick == "alice"
    assert msg.command == "PRIVMSG"
    assert msg.params == ["#chan", "hello there :)"]


def test_parse_numeric_without_prefix_trailing():
    msg = m.parse("PING :irc.example.net")
    assert msg.command == "PING" and msg.params == ["irc.example.net"]
    msg = m.parse(":srv 001 me Welcome")
    assert msg.params == ["me", "Welcome"]


def test_parse_tags_are_tolerated():
    msg = m.parse(r"@time=2024-01-01T00:00:00Z;msg=a\sb :n!u@h PRIVMSG #c :hi")
    assert msg.tags == {"time": "2024-01-01T00:00:00Z", "msg": "a b"}
    assert msg.text == "hi"


def test_parse_ctcp_action():
    msg = m.parse(":bob!b@h PRIVMSG #c :\x01ACTION waves\x01")
    assert msg.ctcp == ("ACTION", "waves")
    assert m.parse(":bob!b@h PRIVMSG #c :plain").ctcp is None


def test_parse_empty_raises():
    with pytest.raises(ValueError):
        m.parse("   ")


def test_serialize():
    assert m.serialize("PRIVMSG", "#c", "hello world") == "PRIVMSG #c :hello world"
    assert m.serialize("JOIN", "#c") == "JOIN #c"
    assert m.serialize("PRIVMSG", "#c", ":)") == "PRIVMSG #c ::)"
    assert m.serialize("PRIVMSG", "#c", "") == "PRIVMSG #c :"
    with pytest.raises(ValueError):
        m.serialize("JOIN", "bad param", "x")


def test_split_text_respects_bytes_and_words():
    text = "word " * 100
    chunks = m.split_text(text.strip(), 50)
    assert all(len(ch.encode()) <= 50 for ch in chunks)
    assert " ".join(chunks) == text.strip()


def test_split_text_never_cuts_multibyte():
    text = "é" * 100  # 2 bytes each
    chunks = m.split_text(text, 51)
    assert all(len(ch.encode()) <= 51 for ch in chunks)
    assert "".join(chunks) == text


def test_split_text_short():
    assert m.split_text("hi", 100) == ["hi"]
    assert m.split_text("", 100) == [""]


def test_casemapping():
    assert irc_lower("Nick[]\\~") == "nick{}|^"


def test_names_replace_member_list():
    chan = ChannelState("#c")
    chan.add("old")
    chan.names_chunk("@op +voice plain")
    chan.names_chunk("other")
    chan.names_end()
    assert [n for n, _ in chan.sorted_members()] == ["op", "voice", "other", "plain"]
    assert not chan.has("old")



def test_names_keep_every_prefix_and_show_the_highest():
    chan = ChannelState("#c")
    chan.names_chunk("+@both plain")
    chan.names_end()
    assert chan.members["both"] == ("both", "@+")
    assert chan.sorted_members() == [("both", "@"), ("plain", "")]


def test_set_prefix_keeps_other_prefixes():
    chan = ChannelState("#c")
    chan.add("bob")
    assert chan.set_prefix("Bob", "+", True)
    assert chan.set_prefix("bob", "@", True)
    assert chan.members["bob"][1] == "@+"
    chan.set_prefix("bob", "@", False)
    assert chan.sorted_members() == [("bob", "+")]
    assert not chan.set_prefix("ghost", "@", True)


def test_parse_modes():
    state = ConnectionState()
    assert state.parse_modes("+o", ["alice"]) == [("+o", "alice")]
    assert state.parse_modes("+ov-l", ["a", "b"]) == [("+o", "a"), ("+v", "b"), ("-l", None)]
    assert state.parse_modes("+lo", ["10", "bob"]) == [("+l", "10"), ("+o", "bob")]
    assert state.parse_modes("+b-k", ["*!*@spam", "*"]) == [("+b", "*!*@spam"), ("-k", "*")]
    assert state.parse_modes("+imnt", []) == [("+i", None), ("+m", None), ("+n", None),
                                              ("+t", None)]
    assert state.parse_modes("+Ro", ["bob"]) == [("+R", None), ("+o", "bob")]  # unknown: no arg
    assert state.parse_modes("+oo", ["bob"]) == [("+o", "bob"), ("+o", None)]


def test_isupport_changes_prefixes_and_mode_args():
    state = ConnectionState()
    state.isupport(["PREFIX=(ov)@+", "CHANMODES=b,k,l,imnst", "NETWORK=Fake", "PREFIX=bogus"])
    assert (state.prefix_modes, state.prefix_chars) == ("ov", "@+")
    assert state.prefix_char("o") == "@" and state.prefix_char("h") is None
    assert state.parse_modes("+h", ["x"]) == [("+h", None)]
    assert state.ensure_channel("#c").prefixes == "@+"
