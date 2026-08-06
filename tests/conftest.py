"""
tests/conftest.py
-----------------
One job: give the suite an ENCRYPTION_KEY.

`core/crypto.py` refuses to encrypt or decrypt without one, and the API refuses
to start -- both deliberate, since the alternative is storing a live bank
credential in plaintext because a variable was unset. That means almost every
test below Step 6.3 needs a key present, and generating a fresh one per test
would be ~30 pointless key derivations.

The key here is FIXED and committed, which is safe precisely because it is
published: it protects nothing, encrypts only values invented inside a test,
and never reaches a real database. Do not copy it into .env.

Set rather than overridden -- an ENCRYPTION_KEY already in the environment
wins, so a developer can run the suite against their own key if they want to.
The tests that are ABOUT a missing or wrong key manage the variable themselves
via monkeypatch, so they are unaffected by this.
"""

from __future__ import annotations

import os

TEST_ENCRYPTION_KEY = "cCk3_1a3fcbLoWVKkvS_hn6HTbUNBSPLtM1CyGkGkI8="

os.environ.setdefault("ENCRYPTION_KEY", TEST_ENCRYPTION_KEY)
