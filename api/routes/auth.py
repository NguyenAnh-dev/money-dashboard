from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Cookie, HTTPException, Response, status

from api.deps import Db, UNAUTHENTICATED
from api.schemas import LoginIn, SignupIn, UserOut
from api.settings import COOKIE_NAME, cookie_params, max_age_seconds
from core import sessions, users

router = APIRouter(prefix="/auth", tags=["auth"])

SessionCookie = Annotated[str | None, Cookie(alias=COOKIE_NAME)]


def _issue_session(response: Response, db, user_id: str) -> None:
    token = sessions.create_session(db, user_id)
    response.set_cookie(
        value=token, max_age=max_age_seconds(), **cookie_params()
    )


@router.post(
    "/signup", response_model=UserOut, status_code=status.HTTP_201_CREATED
)
def signup(body: SignupIn, response: Response, db: Db) -> UserOut:
    try:
        user_id = users.create_user(db, body.email, body.password)
    except users.EmailAlreadyRegistered:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That email is already registered",
        )

    _issue_session(response, db, user_id)
    return UserOut(id=user_id, email=users.normalize_email(body.email))


@router.post("/login", response_model=UserOut)
def login(body: LoginIn, response: Response, db: Db) -> UserOut:
    user_id = users.authenticate(db, body.email, body.password)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )

    _issue_session(response, db, user_id)
    return UserOut(id=user_id, email=users.normalize_email(body.email))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response, db: Db, session: SessionCookie = None) -> None:
    if session:
        sessions.delete_session(db, session)
    response.delete_cookie(**cookie_params())


@router.get("/me", response_model=UserOut)
def me(db: Db, session: SessionCookie = None) -> UserOut:
    if not session:
        raise UNAUTHENTICATED

    user_id = sessions.resolve_session(db, session)
    if user_id is None:
        raise UNAUTHENTICATED

    row = db.execute(
        "SELECT id, email FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    return UserOut(id=row["id"], email=row["email"])
