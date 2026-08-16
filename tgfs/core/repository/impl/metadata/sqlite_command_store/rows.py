"""Turning a database row into the value the rest of the system reads.

A row is a record of what was stored; a NodeSnapshot is what a reader is allowed
to act on. Going through here means a row that cannot be a snapshot - an
unknown kind, a file ref with no message - is caught at the boundary rather
than travelling on as a half-valid value.
"""

import dataclasses
import sqlite3

from tgfs.core.commands import NodeKind, NodeSnapshot, OperationId, parse_node_id
from tgfs.errors import DurableStoreError, InvalidCommandPayload


@dataclasses.dataclass(frozen=True)
class OutboxEntry:
    """One durable outbox row, as a reader outside the store is allowed to see it.

    `acknowledged` is read off the same row a batch is read from, so a
    consumer never has to make a second call to ask what it just asked for.
    """

    sequence: int
    operation_id: OperationId
    command_json: str
    acknowledged: bool


def outbox_entry_from_row(row: sqlite3.Row) -> OutboxEntry:
    return OutboxEntry(
        sequence=row["sequence"],
        operation_id=OperationId(row["operation_id"]),
        command_json=row["command_json"],
        acknowledged=row["acknowledged"] is not None,
    )


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
