"""
core/store.py
-------------
The database, and nothing else. This module OPENS a connection, pins the
pragmas that make SQLite behave the way the schema assumes, and creates the
tables if they are absent. It reads no rows and writes no rows -- that is 5.4.

WHY THIS FILE IS IN core/ AND STILL HAS NO THIRD-PARTY IMPORTS
`sqlite3` ships with Python, so the core/models.py rule ("no third-party
imports, ever") is not strained by it. But note what this module does NOT do:
it does not import Account or Transaction, and it does not know a merchant from
a balance. It owns the FILE. The translation between rows and model objects
belongs above it, the same way an adapter owns the translation between a
provider payload and the model.

THE SPLIT BETWEEN THE FOUR FUNCTIONS
    db_path()      -- where the file is
    connect()      -- an open connection with the pragmas set, no schema
    init_schema()  -- the tables, the meta row, and the version check
    open_db()      -- both of the above; the one everything should call

`connect` and `init_schema` are separate because a test that wants a schemaless
connection (or a second connection to a database another call already created)
should not have to re-run DDL to get one. Everything in the app calls
`open_db`.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

#: Bumped whenever the DDL below changes in a way an existing file cannot
#: satisfy -- a new column, a new CHECK, a renamed table. It is the entire
#: reason this is not just `CREATE TABLE IF NOT EXISTS` and a shrug:
#:
#: IF NOT EXISTS does not compare shapes. Point it at a database whose
#: `transactions` table predates the `kind` column and it silently does
#: NOTHING, reports success, and hands back a connection that will fail on the
#: first INSERT naming a column that is not there -- or worse, succeed against
#: a table missing a CHECK we now depend on. The version is the thing that
#: notices, and it notices at open time rather than mid-sync.
SCHEMA_VERSION = 5

#: Where the database lives when DB_PATH is unset.
#:
#: Anchored to THIS FILE (core/store.py -> project root), not to the working
#: directory. That is a deliberate difference from csv_source.DEFAULT_PATH,
#: which is cwd-relative and consequently only works when the app is run from
#: the project root -- the exact trap tests/test_conformance.py has to pin
#: CSV_PATH to avoid. A database is worse than a CSV in that situation: a wrong
#: relative path does not raise "file not found", it CREATES an empty database
#: wherever you happened to be standing, and the dashboard renders zero
#: accounts as though you had never synced.
DEFAULT_DB_NAME = "money.db"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


#: The whole schema, as one script. Every column here is one field of
#: core.models; see the mapping rules at the top of each table.
#:
#: The types SQLite does not have, and what we store instead:
#:     bool     -> INTEGER 0/1
#:     date     -> TEXT, ISO "2026-07-27"  (sorts correctly as text, which is
#:     datetime -> TEXT, ISO               (the only reason that format works)
#:     Enum     -> TEXT, the .value; the str mixin makes it free and the file
#:                 stays readable in a DB browser
#:     dict     -> TEXT holding JSON
#:
#: Deliberately NOT using sqlite3's detect_types/converters for any of that.
#: They are per-connection and keyed on column declared-type strings, so they
#: work in the app and quietly do nothing in a `sqlite3` shell or a DB browser.
#: Conversion happens in one explicit place at 5.4 instead.
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY NOT NULL,
    email           TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    created_at      TEXT NOT NULL,

    CHECK (length(id) = 36),
    CHECK (length(email) > 0 AND email = lower(email)),
    CHECK (length(password_hash) > 0),
    CHECK (length(created_at) = 19)
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash      TEXT PRIMARY KEY NOT NULL,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,

    CHECK (length(token_hash) = 64),
    CHECK (length(created_at) = 19),
    CHECK (length(expires_at) = 19),
    CHECK (expires_at > created_at)
);

CREATE TABLE IF NOT EXISTS accounts (
    -- make_id(): "provider:connection_id:provider_id". Plain TEXT PRIMARY KEY
    -- is already case-sensitive in SQLite; never add COLLATE NOCASE, it would
    -- merge "aBc123" and "abc123" into one surviving row.
    id                TEXT PRIMARY KEY NOT NULL,

    -- SourceRef, flattened into three columns rather than stored as JSON.
    -- provider and connection_id are technically recoverable from `id`, and we
    -- store them anyway: the rule is NEVER PARSE AN ID, and "everything from
    -- this connection" has to be a WHERE clause rather than a LIKE on a prefix
    -- that breaks the moment a segment contains an escaped %3A.
    --
    -- `fetched_at` is the FOURTH field of SourceRef and is deliberately NOT
    -- here: it changes on every sync and is identical for every row of one
    -- fetch, so storing it per row wrote the same timestamp 76 times and made
    -- a re-sync rewrite every row to change one value. It lives in
    -- `connections` (5.8), which is where core/models.py always said it would
    -- end up.
    provider          TEXT NOT NULL,
    provider_id       TEXT NOT NULL,
    connection_id     TEXT NOT NULL,

    name              TEXT NOT NULL,
    institution       TEXT NOT NULL,
    type              TEXT NOT NULL,
    balance_cents     INTEGER NOT NULL,
    currency          TEXT NOT NULL DEFAULT 'USD',

    mask              TEXT,
    available_cents   INTEGER,
    as_of             TEXT,

    -- typeof() is not belt-and-braces. INTEGER in SQLite is an AFFINITY: store
    -- 12.34 in that column and you get a REAL back, no error, and invariant 1
    -- is breached in the one layer everybody assumes is typed. __post_init__
    -- cannot help here -- the database is a second door in, and a hand-written
    -- UPDATE never passes through it.
    CHECK (typeof(balance_cents) = 'integer'),
    CHECK (available_cents IS NULL OR typeof(available_cents) = 'integer'),
    CHECK (length(currency) = 3 AND currency = upper(currency)),
    CHECK (provider IN ('fake', 'plaid', 'csv')),
    CHECK (type IN ('checking', 'savings', 'credit', 'investment', 'loan', 'other')),

    FOREIGN KEY (provider, connection_id)
        REFERENCES connections(provider, connection_id)

    -- DELIBERATELY ABSENT: a CHECK that debt accounts are non-positive.
    --
    -- It looks like the DEBT_TYPES invariant and it is not. A credit card with
    -- a credit balance -- you overpaid, or a refund landed before the statement
    -- -- genuinely has money owed TO you; Plaid reports it as a negative
    -- `current`, our flip makes it positive, and this write would abort mid-sync
    -- on completely legitimate data.
    --
    -- What is worth catching is an INVERTED FLIP, which is a property of the
    -- adapter and not of any one row, and no row-level CHECK can tell the two
    -- apart. So it stays where it can be investigated instead of thrown:
    -- STRUCTURE reports it, tests/test_conformance.py asserts it.
);

CREATE TABLE IF NOT EXISTS transactions (
    id                TEXT PRIMARY KEY NOT NULL,

    provider          TEXT NOT NULL,
    provider_id       TEXT NOT NULL,
    connection_id     TEXT NOT NULL,

    -- OUR account id, never the provider's. This FK is the STRUCTURE gate's
    -- "every account_id resolves" check moved into the schema, where it holds
    -- continuously instead of once per gate run. It is also why sync order is
    -- load-bearing: accounts before transactions, in one transaction.
    account_id        TEXT NOT NULL REFERENCES accounts(id),

    date              TEXT NOT NULL,
    name              TEXT NOT NULL,
    merchant          TEXT NOT NULL,
    amount_cents      INTEGER NOT NULL,
    category          TEXT NOT NULL,
    currency          TEXT NOT NULL DEFAULT 'USD',
    kind              TEXT NOT NULL DEFAULT 'purchase',
    pending           INTEGER NOT NULL DEFAULT 0,

    -- JSON text. Matches Transaction.raw's field(default_factory=dict): fake
    -- and CSV rows sit at 2 bytes, Plaid rows carry kilobytes. That skew is
    -- payload, not a design choice.
    --
    -- NOT symmetric on types: the encoder writes a date out as "2026-07-27"
    -- and json.loads hands back the STRING. `raw` is an archive to read fields
    -- out of (5.7 wants pending_transaction_id), never a dict to reconstruct
    -- and compare against the original.
    raw               TEXT NOT NULL DEFAULT '{}',

    CHECK (typeof(amount_cents) = 'integer'),
    CHECK (length(merchant) > 0),
    CHECK (length(currency) = 3 AND currency = upper(currency)),
    CHECK (provider IN ('fake', 'plaid', 'csv')),
    CHECK (kind IN ('purchase', 'transfer', 'income', 'fee', 'refund', 'other')),
    CHECK (pending IN (0, 1)),
    -- A date, never a datetime. "2026-07-27T00:00:00" would sort fine and then
    -- break every equality comparison against a bare date.
    CHECK (date = substr(date, 1, 10) AND length(date) = 10),

    FOREIGN KEY (provider, connection_id)
        REFERENCES connections(provider, connection_id)
);

-- One row, and the CHECK is what keeps it that way: a second row would make
-- "how stale is this?" ambiguous, which is the only question it exists to
-- answer.
CREATE TABLE IF NOT EXISTS meta (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version  INTEGER NOT NULL,

    -- NULL until the first sync, and that is the honest state rather than a
    -- placeholder: a database that has been created but never synced has no
    -- "last synced" time, and 0 or "" would both read as a real answer.
    last_synced_at  TEXT
);

-- One row per linked source: one Plaid Item, one CSV import batch, one
-- enrollment. The home the 5.7 scaffolding was explicitly waiting for.
--
-- KEYED ON (provider, connection_id), NOT on a surrogate id. That pair IS the
-- identity -- it is the first two segments of every id the connection mints --
-- so a synthetic key would be a second name for a thing that already has one,
-- plus a lookup to translate between them.
CREATE TABLE IF NOT EXISTS connections (
    provider        TEXT NOT NULL,
    connection_id   TEXT NOT NULL,
    user_id         TEXT NOT NULL REFERENCES users(id),

    -- The provider's resume token. NULL for a source with no such concept,
    -- which is every source except Plaid, and NULL for a Plaid connection that
    -- has never synced. Both mean "start from the beginning", which is the
    -- correct and safe reading.
    cursor          TEXT,

    -- SourceRef.fetched_at, at last. One value per fetch, stored once.
    fetched_at      TEXT,

    PRIMARY KEY (provider, connection_id),
    CHECK (provider IN ('fake', 'plaid', 'csv')),
    CHECK (length(connection_id) > 0)
);

-- Each index exists for a query we already know we make.
CREATE INDEX IF NOT EXISTS idx_txn_account     ON transactions(account_id);
CREATE INDEX IF NOT EXISTS idx_txn_date        ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_txn_merchant    ON transactions(merchant);
CREATE INDEX IF NOT EXISTS idx_txn_connection  ON transactions(provider, connection_id);
CREATE INDEX IF NOT EXISTS idx_acct_connection ON accounts(provider, connection_id);
CREATE INDEX IF NOT EXISTS idx_conn_user       ON connections(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_user   ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);
"""


def db_path() -> str:
    """Where the database file is.

    Reads DB_PATH per call rather than at import, for the reason data_source.py
    gives: a module-level `PATH = os.getenv(...)` freezes the choice at first
    import, so a test that sets the variable afterwards is talking to nobody.
    """
    configured = os.environ.get("DB_PATH", "").strip()
    if configured:
        return configured
    return str(PROJECT_ROOT / DEFAULT_DB_NAME)


def connect(path: str | None = None) -> sqlite3.Connection:
    """An open connection with the pragmas this schema assumes. No DDL.

    `PRAGMA foreign_keys` is PER-CONNECTION, not per-database, which is exactly
    why it lives here and not in SCHEMA. Written into the schema script it would
    apply to whichever connection ran the CREATE TABLEs and to nothing
    afterwards -- so the FK on transactions.account_id would be decoration, and
    the one invariant STRUCTURE exists to catch would go unenforced while
    looking enforced. Putting it in the function that opens the file is what
    means nobody has to remember it.

    It is also a no-op inside a transaction, so it is issued here, before
    anything else touches the connection.
    """
    conn = sqlite3.connect(path or db_path())
    # Rows by column name. Cheap, and it means the 5.4 reader indexes by
    # `row["merchant"]` instead of `row[6]` -- a positional read is a bug
    # waiting for the next column to be added in the middle.
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create the tables if absent, then verify the version.

    Order matters and is not arbitrary: the DDL runs first so that a brand-new
    file HAS a meta table to be asked about, and the version check runs second
    so that an existing file is judged before anything is written to it.
    """
    # Asked BEFORE the DDL runs, because afterwards every database looks like it
    # has always had these tables. This is the only moment the difference
    # between "brand new file" and "existing file" is still visible.
    is_new = _database_is_empty(conn)

    # executescript issues an implicit COMMIT before it runs. Harmless here --
    # this is called on a fresh connection with nothing pending -- but it is the
    # reason this function must not be called in the middle of a sync.
    conn.executescript(SCHEMA)

    # ONLY a brand-new database gets stamped, and the narrowness is the point.
    # An unconditional INSERT OR IGNORE looks equivalent and is not: it would
    # also fire on a database whose meta row is missing -- one somebody edited
    # by hand, or one written before this table existed -- and stamp it with the
    # CURRENT version. That is a guess recorded as a fact, and every check
    # downstream would then trust it. Nothing here ever writes a version onto a
    # file that already had rows; changing that number is a migration's job.
    if is_new:
        conn.execute(
            "INSERT INTO meta (id, schema_version, last_synced_at) "
            "VALUES (1, ?, NULL)",
            (SCHEMA_VERSION,),
        )
    conn.commit()

    _require_supported_version(conn)


def _database_is_empty(conn: sqlite3.Connection) -> bool:
    """True only for a file nothing has ever been written to.

    "No meta TABLE" would be the tempting test and it is the wrong one: a
    database written before the meta table existed has accounts and
    transactions in it and no meta, so that test would call it new and stamp a
    version onto data whose shape we have not verified. Any table at all means
    this file has a history, and its version has to come from the file rather
    than from us.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
    ).fetchone()
    return row[0] == 0


def _require_supported_version(conn: sqlite3.Connection) -> None:
    """Refuse a database this code cannot speak to. Both directions.

    OLDER than us means the file is missing whatever the bump was for, and the
    failure it would otherwise produce is a column-level error mid-sync, three
    layers from the cause.

    NEWER than us means a later version of this app wrote it. That is the more
    dangerous one, because the extra columns are invisible to every SELECT we
    write: the app would read the file happily, render numbers that look fine,
    and drop whatever the new version added on the next write. Refusing is not
    caution, it is the only way that data survives.

    There is no migration path yet, on purpose -- writing one before a second
    version exists means guessing at the shape of a change that has not
    happened.
    """
    row = conn.execute("SELECT schema_version FROM meta WHERE id = 1").fetchone()
    if row is None:
        # The meta table exists (the DDL just created it) but holds no row 1,
        # and the database was not new -- so this file has tables in it whose
        # shape nothing records. Either somebody deleted the row, or the file
        # predates this table. Both are unknown-version, and inventing one is
        # exactly what init_schema refuses to do.
        raise RuntimeError(
            "meta table has no row 1 -- this database has tables but no version "
            "record, so there is no way to tell what shape it is in. Restore "
            "the row if you know the version it was written at; otherwise "
            "delete the file and re-sync."
        )

    found = row["schema_version"]
    if found != SCHEMA_VERSION:
        direction = "older than" if found < SCHEMA_VERSION else "NEWER than"
        raise RuntimeError(
            f"database schema version {found} is {direction} this code's "
            f"{SCHEMA_VERSION} ({db_path()}). There is no migration path yet: "
            f"delete the file and re-sync. Do NOT delete it if the version is "
            f"newer -- that file was written by a later version of this app and "
            f"holds columns this code cannot see, so a re-sync would discard "
            f"them."
        )


def open_db(path: str | None = None) -> sqlite3.Connection:
    """The one everything calls: connected, pragmas set, schema present."""
    conn = connect(path)
    init_schema(conn)
    return conn
