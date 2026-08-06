from __future__ import annotations

import sqlite3
from typing import Annotated, Iterator

from fastapi import Cookie, Depends, HTTPException, status

from api.settings import COOKIE_NAME
from core import sessions, store

UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
)


def get_db() -> Iterator[sqlite3.Connection]:
    conn = store.open_db()
    try:
        yield conn
    finally:
        conn.close()


Db = Annotated[sqlite3.Connection, Depends(get_db)]


def current_user_id(
    db: Db,
    session: Annotated[str | None, Cookie(alias=COOKIE_NAME)] = None,
) -> str:
    if not session:
        raise UNAUTHENTICATED

    user_id = sessions.resolve_session(db, session)
    if user_id is None:
        raise UNAUTHENTICATED

    return user_id


CurrentUser = Annotated[str, Depends(current_user_id)]
