"""Turning a database row into the value the rest of the system reads.

A row is a record of what was stored; a NodeSnapshot is what a reader is allowed
to act on. Going through here means a row that cannot be a snapshot - an
unknown kind, a file ref with no message - is caught at the boundary rather
than travelling on as a half-valid value.
"""

import sqlite3

from tgfs.core.commands import NodeKind, NodeSnapshot, parse_node_id
from tgfs.errors import DurableStoreError, InvalidCommandPayload


def snapshot_from_row(row: sqlite3.Row) -> NodeSnapshot:
    try:
        return NodeSnapshot(
            node_id=parse_node_id(row["node_id"]),
            parent_id=parse_node_id(row["parent_id"]),
            name=row["name"],
            kind=NodeKind(row["kind"]),
            message_id=row["message_id"],
        )
    except (ValueError, InvalidCommandPayload) as e:
        raise DurableStoreError(
            f"stored node {row['node_id']!r} is not a node this code understands",
            cause=str(e),
        ) from e
