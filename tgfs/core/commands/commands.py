"""The mutations a caller can ask a metadata store to perform.

A command is a value, not an action: it is frozen, carries its own version and
names every node by a durable id, so the very same command can be written to a
durable log now and replayed after a restart with the same meaning.
"""

from dataclasses import dataclass, fields
from typing import Any

from tgfs.core.model.common import validate_name
from tgfs.errors import InvalidCommandPayload, InvalidName, RootNodeNotRemovable

from .ids import ROOT_NODE_ID, NodeId, OperationId, parse_node_id, parse_operation_id

COMMAND_VERSION = 1

_NODE_ID_FIELDS = frozenset({"node_id", "parent_id", "new_parent_id", "source_id"})
_NAME_FIELDS = frozenset({"name", "new_name"})
_MESSAGE_ID_FIELDS = frozenset({"message_id"})


def _validate_name(value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise InvalidName(value)
    validate_name(value)


def _validate_message_id(value: Any) -> None:
    # bool is an int in Python and True would silently become message 1.
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InvalidCommandPayload(f"{value!r} is not a message id")


@dataclass(frozen=True)
class MutationCommand:
    """Base of every command. Carries no fields of its own."""

    def __post_init__(self) -> None:
        # Checked here rather than where the command is executed: a command that
        # cannot name its nodes is not a mutation anybody can durably record, and
        # the closer the refusal is to the caller the less there is to unwind.
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name == "operation_id":
                parse_operation_id(value)
            elif field.name in _NODE_ID_FIELDS:
                parse_node_id(value)
            elif field.name in _NAME_FIELDS:
                _validate_name(value)
            elif field.name in _MESSAGE_ID_FIELDS:
                _validate_message_id(value)

        self._validate()

    def _validate(self) -> None:
        """Whatever else this particular command requires of its payload"""


def _reject_root_as_target(node_id: NodeId, what: str) -> None:
    if node_id == ROOT_NODE_ID:
        raise InvalidCommandPayload(f"the root id cannot name {what}")


@dataclass(frozen=True)
class CreateDir(MutationCommand):
    operation_id: OperationId
    parent_id: NodeId
    name: str
    node_id: NodeId
    version: int = COMMAND_VERSION

    def _validate(self) -> None:
        _reject_root_as_target(self.node_id, "a new directory")


@dataclass(frozen=True)
class CreateFileRef(MutationCommand):
    operation_id: OperationId
    parent_id: NodeId
    name: str
    message_id: int
    node_id: NodeId
    version: int = COMMAND_VERSION

    def _validate(self) -> None:
        _reject_root_as_target(self.node_id, "a new file ref")


@dataclass(frozen=True)
class RelinkFileRef(MutationCommand):
    """Point an existing file ref at another file descriptor message"""

    operation_id: OperationId
    node_id: NodeId
    message_id: int
    version: int = COMMAND_VERSION

    def _validate(self) -> None:
        _reject_root_as_target(self.node_id, "a file ref")


@dataclass(frozen=True)
class DeleteNode(MutationCommand):
    """Remove a node and everything below it. Never the root: see ClearRoot"""

    operation_id: OperationId
    node_id: NodeId
    version: int = COMMAND_VERSION

    def _validate(self) -> None:
        if self.node_id == ROOT_NODE_ID:
            raise RootNodeNotRemovable("deleted")


@dataclass(frozen=True)
class ClearRoot(MutationCommand):
    """Empty the root.

    Deleting the root is not the same operation as deleting any other node -
    the root has to survive - so it is spelled out as its own command instead of
    being hidden inside DeleteNode.
    """

    operation_id: OperationId
    version: int = COMMAND_VERSION


@dataclass(frozen=True)
class CopySubtree(MutationCommand):
    """Copy a node and everything below it under another parent.

    Only the root of the copy is named here: the ids of the copied descendants
    are minted by the durable store, which is the only place that knows what the
    subtree contained at the moment the copy was accepted, and reports them in
    the resulting change set.
    """

    operation_id: OperationId
    source_id: NodeId
    parent_id: NodeId
    name: str
    node_id: NodeId
    version: int = COMMAND_VERSION

    def _validate(self) -> None:
        _reject_root_as_target(self.node_id, "the copy")


@dataclass(frozen=True)
class MoveNode(MutationCommand):
    operation_id: OperationId
    node_id: NodeId
    new_parent_id: NodeId
    new_name: str
    version: int = COMMAND_VERSION

    def _validate(self) -> None:
        if self.node_id == ROOT_NODE_ID:
            raise RootNodeNotRemovable("moved")
