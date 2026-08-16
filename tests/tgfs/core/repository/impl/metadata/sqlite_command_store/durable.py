"""Reading the database file itself, not the store that wrote it.

Every assertion about what is durable goes through here on its own connection:
asking the store what it stored would let a store that only ever kept the write
in memory pass. These are the tests' eyes on the file.
"""

import sqlite3
from pathlib import Path
from typing import Any, Optional


def query(path: Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        return list(connection.execute(sql, params))
    finally:
        connection.close()


def one(path: Path, sql: str, params: tuple = ()) -> Any:
    rows = query(path, sql, params)
    return rows[0][0] if rows else None


def file_pragma(path: Path, name: str) -> Any:
    """A pragma stored in the file, so a fresh connection reports it faithfully."""
    return one(path, f"PRAGMA {name}")


def schema_version(path: Path) -> Optional[int]:
    return one(path, "SELECT version FROM schema_version")


def node_row(path: Path, node_id: str) -> Optional[sqlite3.Row]:
    return one_row(path, "SELECT * FROM nodes WHERE node_id = ?", (node_id,))


def require_node_row(path: Path, node_id: str) -> sqlite3.Row:
    """The node's row, for a test that has already established it is there.

    Reading a field off `node_row` directly says the row cannot be missing
    without being able to show it. Going through here means a store that lost
    the node fails saying which one, instead of raising a TypeError about
    subscripting None several lines later.
    """
    row = node_row(path, node_id)
    assert row is not None, f"expected a row for node {node_id}"
    return row


def one_row(path: Path, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    rows = query(path, sql, params)
    return rows[0] if rows else None


def node_rows(path: Path) -> list[sqlite3.Row]:
    return query(path, "SELECT * FROM nodes ORDER BY node_id")


def node_count(path: Path) -> int:
    return one(path, "SELECT COUNT(*) FROM nodes")


def child_named(path: Path, parent_id: str, name: str) -> Optional[sqlite3.Row]:
    return one_row(
        path,
        "SELECT * FROM nodes WHERE parent_id = ? AND name = ?",
        (parent_id, name),
    )


def children_of(path: Path, parent_id: str) -> list[sqlite3.Row]:
    return query(
        path, "SELECT * FROM nodes WHERE parent_id = ? ORDER BY name", (parent_id,)
    )


def operation_rows(path: Path) -> list[sqlite3.Row]:
    return query(path, "SELECT * FROM operations ORDER BY revision")


def outbox_rows(path: Path) -> list[sqlite3.Row]:
    return query(path, "SELECT * FROM outbox ORDER BY sequence")


def snapshot(path: Path) -> dict:
    """Everything a mutation is allowed to touch, in one comparable value."""
    return dict(
        nodes=[dict(row) for row in node_rows(path)],
        operations=[dict(row) for row in operation_rows(path)],
        outbox=[dict(row) for row in outbox_rows(path)],
    )
