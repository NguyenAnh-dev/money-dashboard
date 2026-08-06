from __future__ import annotations

import os

from core.sessions import SESSION_LIFETIME

COOKIE_NAME = "session"

COOKIE_PATH = "/"

COOKIE_SAMESITE = "lax"


def cookie_secure() -> bool:
    return os.environ.get("COOKIE_SECURE", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def cookie_params() -> dict:
    return {
        "key": COOKIE_NAME,
        "path": COOKIE_PATH,
        "httponly": True,
        "samesite": COOKIE_SAMESITE,
        "secure": cookie_secure(),
    }


def max_age_seconds() -> int:
    return int(SESSION_LIFETIME.total_seconds())
