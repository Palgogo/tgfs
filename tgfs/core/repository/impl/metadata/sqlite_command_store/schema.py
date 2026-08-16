"""The durable shape, and the only way it is allowed to change.

The schema is written as a numbered list of migrations rather than as one CREATE
script, because a store that has already been written to cannot be recreated -
it can only be moved forward. `migrate` is the single entry point: it reads the
version the file is at, applies whatever comes after it, and refuses a file from
a future version rather than guessing what its tables mean.

Every invariant that can be stated to SQLite is stated here rather than in
Python. A constraint in the file holds against every writer the file will ever
have, including a repair script run at three in the morning; a check in the
store only holds against this class.
"""

import sqlite3
from typing import Any

from tgfs.core.commands import ROOT_NODE_ID
from tgfs.errors import DurableStoreError

SCHEMA_VERSION = 1

# Node kinds as they are spelled on disk. They are NodeKind's values, restated
# as a CHECK so the file rejects a kind this code would not recognise.
_KINDS = "'D', 'FR'"

_INITIAL: tuple[str, ...] = (
    """
    CREATE TABLE nodes (
        node_id   TEXT PRIMARY KEY,
        -- Deferred, so one transaction may insert a whole subtree or delete one
        -- in any order and still be checked for dangling parents at COMMIT.
        parent_id TEXT REFERENCES nodes (node_id) DEFERRABLE INITIALLY DEFERRED,
        name      TEXT,
        kind      TEXT NOT NULL CHECK (kind IN (%(kinds)s)),
        message_id INTEGER,
        -- The root is the one node with no parent and no name, and it is the
        -- only node allowed to be either.
        CHECK ((parent_id IS NULL) = (node_id = '%(root)s')),
        CHECK ((parent_id IS NULL) = (name IS NULL)),
        -- A file ref points at exactly one file descriptor message; a directory
        -- points at none.
        CHECK ((kind = 'FR') = (message_id IS NOT NULL))
    ) STRICT
    """
    % dict(kinds=_KINDS, root=ROOT_NODE_ID),
    # One name per parent, enforced by the file rather than by a lookup that
    # another writer could race.
    "CREATE UNIQUE INDEX nodes_by_parent_name ON nodes (parent_id, name)",
    "CREATE INDEX nodes_by_parent ON nodes (parent_id)",
    """
    CREATE TABLE operations (
        operation_id TEXT PRIMARY KEY,
        revision     INTEGER NOT NULL UNIQUE CHECK (revision > 0),
        command_type TEXT NOT NULL,
        -- What the caller asked for, reduced to one value. A retry carrying a
        -- different payload under the same id is a different intention, and
        -- this is what lets the store notice.
        payload_hash TEXT NOT NULL,
        -- The AppliedCommand as it was returned the first time, so a replay is
        -- answered with the original rather than with a fresh one.
        applied_json TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE outbox (
        sequence     INTEGER PRIMARY KEY AUTOINCREMENT,
        -- Exactly one row per accepted operation, enforced by the file: a
        -- publisher reading this table can never see a mutation twice.
        operation_id TEXT NOT NULL UNIQUE
                     REFERENCES operations (operation_id) DEFERRABLE INITIALLY DEFERRED,
        revision     INTEGER NOT NULL UNIQUE,
        command_json TEXT NOT NULL,
        applied_json TEXT NOT NULL
    ) STRICT
    """,
)

# The root row is seeded with a bound parameter rather than an interpolated id.
# Everything above is DDL, which cannot take parameters; this is data, which
# can, and so it does.
_SEED_ROOT = (
    "INSERT INTO nodes (node_id, parent_id, name, kind, message_id) "
    "VALUES (?, NULL, NULL, 'D', NULL)",
    (ROOT_NODE_ID,),
)

# A migration is a numbered list of statements, each with whatever it binds.
Statement = tuple[str, tuple[Any, ...]]

_MIGRATIONS: tuple[tuple[int, tuple[Statement, ...]], ...] = (
    (1, tuple((sql, ()) for sql in _INITIAL) + (_SEED_ROOT,)),
)


def migrate(connection: sqlite3.Connection) -> None:
    """Bring the file up to SCHEMA_VERSION, or refuse to touch it."""
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "    id INTEGER PRIMARY KEY CHECK (id = 1),"
        "    version INTEGER NOT NULL"
        ") STRICT"
    )

    current = _current_version(connection)
    if current > SCHEMA_VERSION:
        raise DurableStoreError(
            f"the database is at schema version {current} and this code "
            f"understands {SCHEMA_VERSION}"
        )

    # One transaction for the whole upgrade: a half-migrated file is worse than
    # an unmigrated one.
    connection.execute("BEGIN IMMEDIATE")
    try:
        for version, statements in _MIGRATIONS:
            if version <= current:
                continue
            for sql, params in statements:
                connection.execute(sql, params)
        connection.execute(
            "INSERT INTO schema_version (id, version) VALUES (1, ?) "
            "ON CONFLICT (id) DO UPDATE SET version = excluded.version",
            (SCHEMA_VERSION,),
        )
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def _current_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT version FROM schema_version").fetchone()
    return row[0] if row else 0
