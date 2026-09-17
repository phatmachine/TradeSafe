"""Single shared-password session auth (single-user personal tool — no accounts table).
Fails closed: the API refuses to start without TRADESAFE_PASSWORD and TRADESAFE_SECRET
set, rather than silently falling open on a public deployment.
"""
from __future__ import annotations

import os

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

COOKIE_NAME = "tradesafe_session"
MAX_AGE_SECONDS = 30 * 24 * 3600


def _password() -> str:
    pw = os.environ.get("TRADESAFE_PASSWORD")
    if not pw:
        raise RuntimeError(
            "TRADESAFE_PASSWORD is not set. This tool is meant to be reachable from the "
            "internet once deployed; refusing to start with no access password rather "
            "than defaulting to open. Set TRADESAFE_PASSWORD (and TRADESAFE_SECRET) in "
            "the environment — see deploy/README.md."
        )
    return pw


def _serializer() -> URLSafeTimedSerializer:
    secret = os.environ.get("TRADESAFE_SECRET")
    if not secret:
        raise RuntimeError("TRADESAFE_SECRET is not set — see deploy/README.md.")
    return URLSafeTimedSerializer(secret, salt="tradesafe-session")


def check_password(candidate: str) -> bool:
    import hmac

    return hmac.compare_digest(candidate, _password())


def create_session_token() -> str:
    return _serializer().dumps({"authenticated": True})


def verify_session_token(token: str | None) -> bool:
    if not token:
        return False
    try:
        data = _serializer().loads(token, max_age=MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return False
    return bool(data.get("authenticated"))
