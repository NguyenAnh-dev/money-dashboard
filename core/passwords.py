from __future__ import annotations

ALGORITHM_PREFIX = "$argon2"

MIN_PASSWORD_LENGTH = 8

DUMMY_PASSWORD = "dummy-password-for-constant-work"

_hasher = None
_dummy_hash: str | None = None


def hasher():
    global _hasher
    if _hasher is None:
        from argon2 import PasswordHasher

        _hasher = PasswordHasher()
    return _hasher


def _failures() -> tuple[type[Exception], ...]:
    from argon2.exceptions import (
        InvalidHashError,
        VerificationError,
        VerifyMismatchError,
    )

    return (VerifyMismatchError, VerificationError, InvalidHashError)


def hash_password(password: str) -> str:
    if not isinstance(password, str):
        raise TypeError(
            f"password must be str, got {type(password).__name__}"
        )
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters, "
            f"got {len(password)}"
        )
    return hasher().hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return hasher().verify(password_hash, password)
    except _failures():
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return hasher().check_needs_rehash(password_hash)
    except _failures():
        return False


def is_argon2_hash(password_hash: str) -> bool:
    return password_hash.startswith(ALGORITHM_PREFIX)


def waste_a_verification(password: str = DUMMY_PASSWORD) -> None:
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hasher().hash(DUMMY_PASSWORD)
    verify_password(_dummy_hash, password)
