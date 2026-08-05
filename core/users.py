from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime

from core import passwords

LOCAL_USER_ID = "00000000-0000-4000-8000-000000000001"
LOCAL_USER_EMAIL = "local@localhost"

UNUSABLE_PASSWORD_HASH = "!"

USER_ID_LENGTH = 36


def new_user_id() -> str:
    return str(uuid.uuid4())


def normalize_email(email: str) -> str:
    cleaned = email.strip().lower()
    if not cleaned:
        raise ValueError("email must not be empty")
    return cleaned


def is_usable_password_hash(password_hash: str) -> bool:
    return bool(password_hash) and not password_hash.startswith(
        UNUSABLE_PASSWORD_HASH
    )


class EmailAlreadyRegistered(ValueError):
    def __init__(self, email: str) -> None:
        super().__init__(f"{email} is already registered")
        self.email = email


def create_user(
    conn: sqlite3.Connection,
    email: str,
    password: str,
    user_id: str | None = None,
    created_at: datetime | None = None,
) -> str:
    return create_user_with_hash(
        conn,
        email,
        passwords.hash_password(password),
        user_id=user_id,
        created_at=created_at,
    )


def create_user_with_hash(
    conn: sqlite3.Connection,
    email: str,
    password_hash: str,
    user_id: str | None = None,
    created_at: datetime | None = None,
) -> str:
    if not password_hash:
        raise ValueError(
            "password_hash must not be empty -- use UNUSABLE_PASSWORD_HASH for "
            "an account that cannot be logged into"
        )

    email = normalize_email(email)
    user_id = user_id or new_user_id()
    when = (created_at or datetime.now()).replace(microsecond=0)

    try:
        with conn:
            conn.execute(
                "INSERT INTO users (id, email, password_hash, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    user_id,
                    email,
                    password_hash,
                    when.isoformat(timespec="seconds"),
                ),
            )
    except sqlite3.IntegrityError as exc:
        if "users.email" in str(exc):
            raise EmailAlreadyRegistered(email) from exc
        raise

    return user_id


def authenticate(
    conn: sqlite3.Connection, email: str, password: str
) -> str | None:
    try:
        normalized = normalize_email(email)
    except ValueError:
        passwords.waste_a_verification(password)
        return None

    row = conn.execute(
        "SELECT id, password_hash FROM users WHERE email = ?", (normalized,)
    ).fetchone()

    if row is None or not is_usable_password_hash(row["password_hash"]):
        passwords.waste_a_verification(password)
        return None

    if not passwords.verify_password(row["password_hash"], password):
        return None

    return row["id"]


def user_exists(conn: sqlite3.Connection, user_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    return row is not None


def user_id_for_email(conn: sqlite3.Connection, email: str) -> str | None:
    row = conn.execute(
        "SELECT id FROM users WHERE email = ?", (normalize_email(email),)
    ).fetchone()
    return None if row is None else row["id"]


def ensure_local_user(conn: sqlite3.Connection) -> str:
    if not user_exists(conn, LOCAL_USER_ID):
        create_user_with_hash(
            conn,
            LOCAL_USER_EMAIL,
            UNUSABLE_PASSWORD_HASH,
            user_id=LOCAL_USER_ID,
        )
    return LOCAL_USER_ID
