import pytest

from llm_irc.irc.auth import challengeauth_response, pick_algorithm

CHALLENGE = "12345678901234567890123456789012"

# Test vectors from https://www.quakenet.org/development/challengeauth
VECTORS = [
    ("HMAC-MD5", "mooking", "0000000000", "2ed1a1f1d2cd5487d2e18f27213286b9"),
    ("HMAC-MD5", "fishking", "ZZZZZZZZZZ", "8990cb478218b6c0063daf08dd7e1a72"),
    ("HMAC-SHA-1", "mooking", "0000000000", "d0328d41426bd2ace183467ce0a6305445e3d497"),
    ("HMAC-SHA-1", "fishking", "ZZZZZZZZZZ", "4de3f1c86dd0f59da44852d507e193c339c4b108"),
    ("HMAC-SHA-256", "mooking", "0000000000",
     "f6eced34321a69c270472d06c50e959c48e9fd323b2c5d3194f44b50a118a7ea"),
    ("HMAC-SHA-256", "fishking", "ZZZZZZZZZZ",
     "504056d53b2fc4fd783dc4f086dabc59f845d201e650b96dfa95dacc8cac2892"),
]


@pytest.mark.parametrize("algorithm,user,password,expected", VECTORS)
def test_quakenet_vectors(algorithm, user, password, expected):
    assert challengeauth_response(user, password, CHALLENGE, algorithm) == expected


def test_worked_example_lowercases_and_truncates():
    # "[fishking]" lowercases to "{fishking}"; the password is cut to 10 characters.
    assert challengeauth_response("[fishking]", "iLOVEfish12345",
                                  "3afabede5c2859fd821e315f889d9a6c", "HMAC-SHA-1") \
        == "e683c83fd16a03b6d690ea231b4f346c32ae0aaa"


def test_pick_strongest_offered():
    assert pick_algorithm(["HMAC-MD5", "HMAC-SHA-1", "HMAC-SHA-256", "LEGACY-MD5"]) == "HMAC-SHA-256"
    assert pick_algorithm(["HMAC-MD5"]) == "HMAC-MD5"
    assert pick_algorithm(["LEGACY-MD5"]) is None
