"""
adapters/fingerprint.py
-----------------------
Stable ids for sources that do not supply one.

Plaid gives every transaction an id and that id is the provider_id -- nothing
here applies. A bank CSV export gives you a date, a descriptor and an amount,
and nothing else. Neither does a hand-written fixture. Those sources need an id
MINTED, and the whole question is what to mint it from.

POSITION IS THE WRONG ANSWER, even though it is the obvious one. Numbering rows
txn_001.. means the id is a function of where the row sits in the file:

  * Insert one transaction in the middle and every id after it shifts by one.
    At Step 5 that upsert does not update 40 rows -- it REWRITES them, pointing
    each id at a different transaction. Any note, category override or
    reconciliation flag a user attached is now on the wrong row.
  * Re-export the same statement with the rows in a different order and you get
    a different id for the same real-world charge, so the re-import duplicates
    everything instead of matching.

CONTENT IS THE RIGHT ANSWER. Hash what the charge IS -- date, descriptor,
amount, account -- and the id follows the transaction rather than its position.
Insert a row, and only the new row gets a new id. Re-export in a different
order, and every id matches. This is the same property make_id() has: an id
that is deterministic on identity is what turns a database write into an upsert.

WHAT THIS DOES NOT SOLVE
Two genuinely identical charges -- same card, same day, same merchant, same
amount, which is two coffees on one morning, not an error -- hash identically,
because by construction they contain no distinguishing information. There is no
way around that; the source did not record anything to tell them apart. The
minter handles it the only honest way: it counts repeats within one fetch and
folds an occurrence number into the second and later copies. That is stable as
long as the export is, which is the best any content-derived scheme can do.
"""

from __future__ import annotations

import hashlib
from collections import Counter

#: Hex characters kept from the digest. 12 hex chars = 48 bits. The relevant
#: population is every row this CONNECTION has ever minted (the id namespace is
#: provider:connection, not per account), and the birthday bound puts a coin
#: flip at 2**24 ~ 17M rows: ~1.8e-3 at a million, ~1.8e-7 at ten thousand.
#: Fine for a personal ledger, and short enough to read in a log line. Changing
#: this REKEYS EVERY ROW ever minted.
DIGEST_CHARS = 12

#: Field separator inside the hashed payload. Escaped on the way in, because
#: plain joining is not injective: ("a|b", "c") and ("a", "b|c") would hash to
#: the same string and become one row. Same failure mode core.models.make_id
#: escapes against, so it gets the same treatment rather than a comment saying
#: "descriptors probably do not contain pipes".
_SEP = "|"


class ContentIdMinter:
    """Mints ids from row content. One instance per fetch, not a global.

    The occurrence counter is per-instance state, so a fresh fetch starts from
    a clean slate and produces exactly the same ids as the last one. A module
    level counter would make the ids depend on how many times you had called
    the adapter in this process, which is precisely the class of bug this whole
    module exists to remove.
    """

    def __init__(self, prefix: str, digest_chars: int = DIGEST_CHARS) -> None:
        self.prefix = prefix
        self.digest_chars = digest_chars
        self._seen: Counter[str] = Counter()

    def mint(self, *parts: object) -> str:
        """Hash the parts into a stable id: mint("2026-05-04", "SBUX", 545, ...).

        Pass the RAW descriptor rather than the normalized merchant. Normalizing
        is our logic, and it changes: improve the merchant cleaner and every id
        derived from it would move, silently rekeying the entire history.
        """
        payload = _SEP.join(_escape(str(part)) for part in parts)

        # Second and later copies of identical content get an occurrence suffix
        # folded into the hash INPUT, not appended to the output, so every id
        # stays the same shape and length.
        #
        # The marker is `\#`, and the backslash is what makes it safe rather
        # than decorative. _escape doubles every literal backslash, so an
        # escaped payload can never contain a lone one -- which means no real
        # content can imitate this suffix. A bare "#" would NOT be safe:
        # mint("x") twice hashes "x|#1", and so does a single mint("x", "#1"),
        # and those are two different rows sharing one id.
        seen = self._seen[payload]
        self._seen[payload] += 1
        material = payload if seen == 0 else f"{payload}{_SEP}\\#{seen}"

        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
        return f"{self.prefix}_{digest[:self.digest_chars]}"


def _escape(part: str) -> str:
    r"""Make a field safe to join with `_SEP`.

    Backslash first, then the separator -- reversing that order would let the
    literal input "a\|b" and the escaped form of "a|b" both render as "a\|b",
    reintroducing the collision being removed.
    """
    return part.replace("\\", r"\\").replace(_SEP, r"\|")
