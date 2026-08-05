from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta

TOKEN_BYTES = 32

SESSION_LIFETIME = timedelta(days=14)


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _stamp(when: datetime) -> str:
    return when.replace(microsecond=0).isoformat(timespec="seconds")


def create_session(
    conn: sqlite3.Connection,
    user_id: str,
    now: datetime | None = None,
    lifetime: timedelta | None = None,
) -> str:
    issued = (now if now is not None else datetime.now()).replace(microsecond=0)
    expires = issued + (
        lifetime if lifetime is not None else SESSION_LIFETIME
    )

    if expires <= issued:
        raise ValueError(
            f"lifetime must be positive, got {lifetime!r} -- a session that "
            f"expires when it is issued is a login that never works"
        )

    token = new_token()
    with conn:
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (token_hash(token), user_id, _stamp(issued), _stamp(expires)),
        )
    return token


def resolve_session(
    conn: sqlite3.Connection, token: str, now: datetime | None = None
) -> str | None:
    if not token:
        return None

    row = conn.execute(
        "SELECT user_id, expires_at FROM sessions WHERE token_hash = ?",
        (token_hash(token),),
    ).fetchone()

    if row is None:
        return None
    if row["expires_at"] <= _stamp(now or datetime.now()):
        return None

    return row["user_id"]


def delete_session(conn: sqlite3.Connection, token: str) -> bool:
    with conn:
        cursor = conn.execute(
            "DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),)
        )
    return cursor.rowcount > 0


def delete_sessions_for_user(conn: sqlite3.Connection, user_id: str) -> int:
    with conn:
        cursor = conn.execute(
            "DELETE FROM sessions WHERE user_id = ?", (user_id,)
        )
    return cursor.rowcount


def purge_expired(
    conn: sqlite3.Connection, now: datetime | None = None
) -> int:
    with conn:
        cursor = conn.execute(
            "DELETE FROM sessions WHERE expires_at <= ?",
            (_stamp(now or datetime.now()),),
        )
    return cursor.rowcount


def active_sessions(conn: sqlite3.Connection, user_id: str,
                    now: datetime | None = None) -> int:
    row = conn.execute(
        "SELECT COUNT(*) c FROM sessions WHERE user_id = ? AND expires_at > ?",
        (user_id, _stamp(now or datetime.now())),
    ).fetchone()
    return row["c"]
