"""Opaque tokens: 32 random bytes, no raw-token persistence."""

import hashlib
import re
import secrets

TOKEN_PATTERN = re.compile(r"lv_[A-Za-z0-9_-]{43}")


def new_token() -> str:
    return "lv_" + secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()
