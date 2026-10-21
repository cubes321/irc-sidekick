"""Services authentication helpers.

QuakeNet has no NickServ; accounts live with the Q bot. QuakeNet offers no TLS, so a
plain `AUTH user pass` would cross the network in cleartext. CHALLENGEAUTH instead
proves knowledge of the password with an HMAC over a one-time challenge from Q.
Spec: https://www.quakenet.org/development/challengeauth
"""
from __future__ import annotations

import hashlib
import hmac

from .state import irc_lower

Q_TARGET = "Q@CServe.quakenet.org"  # the full form makes sure it reaches the real Q
Q_PREFIX = "q!theqbot@cserve.quakenet.org"

# Strongest first. LEGACY-MD5 is deprecated and deliberately not supported.
_ALGORITHMS = {
    "HMAC-SHA-256": hashlib.sha256,
    "HMAC-SHA-1": hashlib.sha1,
    "HMAC-MD5": hashlib.md5,
}


def pick_algorithm(offered: list[str]) -> str | None:
    offered_upper = {a.upper() for a in offered}
    return next((name for name in _ALGORITHMS if name in offered_upper), None)


def challengeauth_response(username: str, password: str, challenge: str,
                           algorithm: str = "HMAC-SHA-256") -> str:
    digest = _ALGORITHMS[algorithm.upper()]
    password_hash = digest(password[:10].encode()).hexdigest()
    key = digest(f"{irc_lower(username)}:{password_hash}".encode()).hexdigest()
    return hmac.new(key.encode(), challenge.encode(), digest).hexdigest()
