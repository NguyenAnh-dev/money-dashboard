from __future__ import annotations

import os

KEY_VARIABLE = "ENCRYPTION_KEY"

FERNET_KEY_LENGTH = 44

TOKEN_PREFIX = "gAAAAA"

HOW_TO_GENERATE = (
    "Generate one with:\n"
    "    ./venv/Scripts/python.exe -c "
    "\"from cryptography.fernet import Fernet; "
    "print(Fernet.generate_key().decode())\"\n"
    f"then put it in .env as {KEY_VARIABLE}=<the 44-character key>."
)

_fernet = None
_key_used: str | None = None


class EncryptionKeyMissing(RuntimeError):
    def __init__(self) -> None:
        super().__init__(
            f"{KEY_VARIABLE} is not set, so stored access tokens can be "
            f"neither written nor read.\n{HOW_TO_GENERATE}\n"
            f"Losing this key makes every stored token permanently "
            f"unreadable; the accounts have to be re-linked."
        )


class EncryptionKeyInvalid(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(
            f"{KEY_VARIABLE} is not a valid Fernet key: {reason}.\n"
            f"{HOW_TO_GENERATE}"
        )


def configured_key() -> str:
    return os.environ.get(KEY_VARIABLE, "").strip()


def is_configured() -> bool:
    return bool(configured_key())


def require_key() -> str:
    key = configured_key()
    if not key:
        raise EncryptionKeyMissing()
    return key


def fernet():
    global _fernet, _key_used

    key = require_key()
    if _fernet is not None and _key_used == key:
        return _fernet

    from cryptography.fernet import Fernet

    try:
        _fernet = Fernet(key)
    except (ValueError, TypeError) as exc:
        raise EncryptionKeyInvalid(str(exc)) from exc

    _key_used = key
    return _fernet


def encrypt(plaintext: str) -> str:
    if not plaintext:
        raise ValueError(
            "refusing to encrypt an empty string -- an empty access token is "
            "a caller that lost the value, and the ciphertext would decrypt "
            "back to something unusable with no sign of where it went"
        )
    return fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    from cryptography.fernet import InvalidToken

    try:
        return fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise EncryptionKeyInvalid(
            "the stored value does not decrypt with this key -- either the "
            "key was rotated without re-encrypting, or the row was written "
            "by a different deployment"
        ) from exc


def looks_encrypted(value: str) -> bool:
    return value.startswith(TOKEN_PREFIX)


def generate_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode("ascii")
