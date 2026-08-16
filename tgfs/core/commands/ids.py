"""Durable node identity.

A node id is an opaque, durable value: it is minted once, stored with the node
and never derived from anything that can change (a path, a position, a counter
that restarts with the process). That is what lets a command that was written
down before a restart still name the same node after it.
"""

import uuid
from typing import Any, NewType

from tgfs.errors import InvalidNodeId, InvalidOperationId

NodeId = NewType("NodeId", str)

# The root exists in every store and is created by no command, so it is the one
# node whose id has to be agreed on in advance rather than minted.
ROOT_NODE_ID: NodeId = NodeId("00000000-0000-0000-0000-000000000000")


def new_node_id() -> NodeId:
    return NodeId(str(uuid.uuid4()))


OperationId = NewType("OperationId", str)


def new_operation_id() -> OperationId:
    return OperationId(str(uuid.uuid4()))


def parse_operation_id(value: Any) -> OperationId:
    """The value as an operation id, or InvalidOperationId if it cannot be one.

    An operation id is what tells a retry from a second, genuine mutation, so it
    has to come from a space wide enough that two callers never pick the same
    one by accident.
    """
    if not _is_canonical_uuid(value):
        raise InvalidOperationId(value)
    return OperationId(value)


def parse_node_id(value: Any) -> NodeId:
    """The value as a node id, or InvalidNodeId if it cannot be one.

    Ids arrive from serialized commands, so anything that is not the exact
    canonical form is refused rather than repaired: a value that only looks like
    an id would silently name a different node than the writer meant.
    """
    if not _is_canonical_uuid(value):
        raise InvalidNodeId(value)
    return NodeId(value)


def _is_canonical_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False
