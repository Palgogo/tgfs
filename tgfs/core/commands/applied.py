"""What a durable store recorded when it accepted a command.

A command says what a caller wants; an AppliedCommand says what actually became
true, at which revision, and is the only thing anything downstream is allowed to
act on. It is a value all the way down - frozen, tuples, ids and plain fields -
because it outlives the call that produced it: it is written to a log, read back
after a restart and handed to readers that must see exactly what was accepted,
not a live model object that has moved on since.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping, Optional

from tgfs.core.model.common import validate_name
from tgfs.errors import InvalidCommandPayload, InvalidName, RootNodeNotRemovable

# One version number covers the whole durable vocabulary: a command and the
# record of its acceptance are read back by the same reader and there is no
# point at which one could be understood without the other.
from .commands import COMMAND_VERSION
from .ids import ROOT_NODE_ID, NodeId, OperationId, parse_node_id, parse_operation_id


class NodeKind(Enum):
    DIRECTORY = "D"
    FILE_REF = "FR"


def _parse_kind(value: Any) -> NodeKind:
    try:
        return NodeKind(value)
    except ValueError:
        raise InvalidCommandPayload(f"{value!r} is not a node kind")


def _require_positive_int(value: Any, what: str) -> int:
    # bool is an int in Python and True would silently become revision 1.
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InvalidCommandPayload(f"{value!r} is not {what}")
    return value


@dataclass(frozen=True)
class NodeSnapshot:
    """One node exactly as the store left it.

    Flat and self-contained on purpose: a snapshot names its parent by id rather
    than pointing at it, so nothing here can be walked back into the live tree.
    """

    node_id: NodeId
    parent_id: NodeId
    name: str
    kind: NodeKind
    message_id: Optional[int] = None

    def __post_init__(self) -> None:
        parse_node_id(self.node_id)
        parse_node_id(self.parent_id)
        if self.node_id == ROOT_NODE_ID:
            raise RootNodeNotRemovable("described by a change set")

        if not isinstance(self.name, str) or not self.name:
            raise InvalidName(self.name)
        validate_name(self.name)

        if not isinstance(self.kind, NodeKind):
            raise InvalidCommandPayload(f"{self.kind!r} is not a node kind")

        if self.kind is NodeKind.FILE_REF:
            _require_positive_int(self.message_id, "a message id")
        elif self.message_id is not None:
            raise InvalidCommandPayload("a directory points at no message")

    def to_dict(self) -> dict:
        data = dict(
            type=self.kind.value,
            nodeId=self.node_id,
            parentId=self.parent_id,
            name=self.name,
        )
        if self.kind is NodeKind.FILE_REF:
            data["messageId"] = self.message_id  # type: ignore[assignment]
        return data


def node_snapshot_from_dict(data: Any) -> NodeSnapshot:
    kind = _parse_kind(_field(data, "type"))
    expected = {"type", "nodeId", "parentId", "name"} | (
        {"messageId"} if kind is NodeKind.FILE_REF else set()
    )
    _reject_unknown_fields(data, expected)

    return NodeSnapshot(
        node_id=parse_node_id(_field(data, "nodeId")),
        parent_id=parse_node_id(_field(data, "parentId")),
        name=_field(data, "name"),
        kind=kind,
        message_id=_field(data, "messageId") if kind is NodeKind.FILE_REF else None,
    )


def _snapshots(value: Any, what: str) -> tuple[NodeSnapshot, ...]:
    # Only a tuple: a list handed in would stay aliased to the caller, and a
    # record of what was durably accepted that can still change is worth
    # nothing to the reader who gets it after the fact.
    if not isinstance(value, tuple):
        raise InvalidCommandPayload(f"{what} must be a tuple, not {type(value).__name__}")
    for snapshot in value:
        if not isinstance(snapshot, NodeSnapshot):
            raise InvalidCommandPayload(
                f"{what} holds {type(snapshot).__name__}, not a snapshot"
            )
    return value


def _node_ids(value: Any, what: str) -> tuple[NodeId, ...]:
    if not isinstance(value, tuple):
        raise InvalidCommandPayload(f"{what} must be a tuple, not {type(value).__name__}")
    for node_id in value:
        parse_node_id(node_id)
        if node_id == ROOT_NODE_ID:
            raise RootNodeNotRemovable("deleted")
    return value


@dataclass(frozen=True)
class AppliedCommand:
    """A durably accepted change, addressed by revision.

    The change set is spelled out as whole snapshots rather than as the command
    that caused them: a reader replaying this does not have to know what
    CopySubtree meant, only which nodes ended up existing.
    """

    revision: int
    operation_id: OperationId
    created: tuple[NodeSnapshot, ...] = ()
    updated: tuple[NodeSnapshot, ...] = ()
    deleted: tuple[NodeId, ...] = ()
    version: int = COMMAND_VERSION

    def __post_init__(self) -> None:
        _require_positive_int(self.revision, "a revision")
        parse_operation_id(self.operation_id)
        _snapshots(self.created, "created")
        _snapshots(self.updated, "updated")
        _node_ids(self.deleted, "deleted")

        if not (self.created or self.updated or self.deleted):
            raise InvalidCommandPayload("a revision that changes nothing")

        named = [s.node_id for s in self.created + self.updated] + list(self.deleted)
        if len(set(named)) != len(named):
            raise InvalidCommandPayload("a node named twice in one change set")

    @property
    def touched(self) -> frozenset[NodeId]:
        return frozenset(
            [s.node_id for s in self.created + self.updated] + list(self.deleted)
        )

    def to_dict(self) -> dict:
        return dict(
            version=self.version,
            revision=self.revision,
            operationId=self.operation_id,
            created=[s.to_dict() for s in self.created],
            updated=[s.to_dict() for s in self.updated],
            deleted=list(self.deleted),
        )


_APPLIED_FIELDS = frozenset(
    {"version", "revision", "operationId", "created", "updated", "deleted"}
)


def applied_command_from_dict(data: Any) -> AppliedCommand:
    """The record as a value, or InvalidCommandPayload if it cannot be one.

    Strict in both directions: a missing field and an unexpected one are both
    refusals, because this is read back from durable storage written by some
    other version of this code, and a record that is quietly repaired is a
    record that no longer says what the store accepted.
    """
    _reject_unknown_fields(data, _APPLIED_FIELDS)

    version = _field(data, "version")
    if version != COMMAND_VERSION:
        raise InvalidCommandPayload(f"version {version!r}, expected {COMMAND_VERSION}")

    return AppliedCommand(
        revision=_field(data, "revision"),
        operation_id=parse_operation_id(_field(data, "operationId")),
        created=_snapshots_from(_field(data, "created"), "created"),
        updated=_snapshots_from(_field(data, "updated"), "updated"),
        deleted=tuple(parse_node_id(i) for i in _sequence(_field(data, "deleted"))),
        version=version,
    )


def _snapshots_from(value: Any, what: str) -> tuple[NodeSnapshot, ...]:
    return tuple(node_snapshot_from_dict(item) for item in _sequence(value))


def _sequence(value: Any) -> Iterable[Any]:
    if not isinstance(value, list):
        raise InvalidCommandPayload(f"{value!r} is not a list of changes")
    return value


def _field(data: Any, name: str) -> Any:
    if not isinstance(data, Mapping):
        raise InvalidCommandPayload(f"{type(data).__name__} is not a record")
    if name not in data:
        raise InvalidCommandPayload(f"missing {name!r}")
    return data[name]


def _reject_unknown_fields(data: Any, expected: Iterable[str]) -> None:
    if not isinstance(data, Mapping):
        raise InvalidCommandPayload(f"{type(data).__name__} is not a record")
    unknown = set(data) - set(expected)
    if unknown:
        raise InvalidCommandPayload(f"unexpected {sorted(unknown)}")
