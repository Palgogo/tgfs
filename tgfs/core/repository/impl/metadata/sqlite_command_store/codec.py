"""Commands as durable text.

Two readers need a command written down: the operations table, to tell a retry
of the same intention from a different one, and the outbox, to tell whoever
publishes changes what was asked for. Both read it back long after the object
that produced it is gone, so the encoding is explicit - a named type, named
fields, no pickled classes and no field that only this version would know how
to interpret.

The encoding is canonical: the same command always produces byte-identical
JSON, which is what makes hashing it a fair comparison. It carries only what the
command itself carries - ids, names, message ids - and nothing about the session,
the channel or the credentials that got it here.
"""

import hashlib
import json
from dataclasses import fields
from typing import Any, Mapping

from tgfs.core.commands import (
    ClearRoot,
    CopySubtree,
    CreateDir,
    CreateFileRef,
    DeleteNode,
    MoveNode,
    MutationCommand,
    OperationId,
    RelinkFileRef,
)
from tgfs.errors import InvalidCommandPayload

_COMMANDS: Mapping[str, type[MutationCommand]] = {
    command.__name__: command
    for command in (
        CreateDir,
        CreateFileRef,
        RelinkFileRef,
        DeleteNode,
        ClearRoot,
        CopySubtree,
        MoveNode,
    )
}

# Spelled out rather than derived: the name a field has on disk is part of the
# format, and renaming an attribute should not silently change what was written.
_KEYS: Mapping[str, str] = {
    "operation_id": "operationId",
    "parent_id": "parentId",
    "new_parent_id": "newParentId",
    "source_id": "sourceId",
    "node_id": "nodeId",
    "name": "name",
    "new_name": "newName",
    "message_id": "messageId",
    "version": "version",
}
_FIELDS: Mapping[str, str] = {key: field for field, key in _KEYS.items()}

_TYPE = "type"


def command_type(command: MutationCommand) -> str:
    return type(command).__name__


def operation_id(command: MutationCommand) -> OperationId:
    """The id a command was sent under.

    Every command carries one and `MutationCommand` validates it, but each
    declares the field itself rather than inheriting it, so there is nothing to
    read on the base type. Reading it through here keeps that in one place
    instead of at every call site that has only a `MutationCommand` in hand.
    """
    return getattr(command, "operation_id")


def command_to_dict(command: MutationCommand) -> dict:
    data: dict[str, Any] = {_TYPE: command_type(command)}
    for field in fields(command):
        data[_KEYS[field.name]] = getattr(command, field.name)
    return data


def command_from_dict(data: Any) -> MutationCommand:
    """The record as the command it was, or InvalidCommandPayload.

    Strict both ways: a missing field and an unknown one are refusals. This is
    read back from storage that some other version of this code wrote, and a
    command quietly repaired on the way in is no longer the command that was
    accepted.
    """
    if not isinstance(data, Mapping):
        raise InvalidCommandPayload(f"{type(data).__name__} is not a command record")

    name = data.get(_TYPE)
    command = _COMMANDS.get(name) if isinstance(name, str) else None
    if command is None:
        raise InvalidCommandPayload(f"{name!r} is not a command type")

    expected = {_KEYS[field.name] for field in fields(command)} | {_TYPE}
    unknown = set(data) - expected
    if unknown:
        raise InvalidCommandPayload(f"unexpected {sorted(unknown)}")
    missing = expected - set(data)
    if missing:
        raise InvalidCommandPayload(f"missing {sorted(missing)}")

    return command(**{_FIELDS[key]: value for key, value in data.items() if key != _TYPE})


def canonical_json(data: Any) -> str:
    """One value, one text. Sorted keys and no incidental whitespace."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def payload_hash(command: MutationCommand) -> str:
    """What the caller asked for, reduced to one comparable value."""
    return hashlib.sha256(
        canonical_json(command_to_dict(command)).encode("utf-8")
    ).hexdigest()
