"""Comparing a source tree against a target tree, without changing either.

A path is the unit of comparison rather than a node identity, because the
source and the target were never the same object graph: a directory that
exists on both sides is only "the same" in the sense that it sits at the same
place, and a rename or a move is nothing this can tell apart from one node
disappearing and a different one appearing - which is exactly how a human
reading the report would want to see it too.
"""

from dataclasses import dataclass
from typing import Optional

from tgfs.core.model import TGFSDirectory

from .provenance import SourceDescriptor, TargetDescriptor, utc_now

_DIRECTORY = "D"
_FILE_REF = "FR"


class AmbiguousPath(Exception):
    """A tree assigns more than one node to the same logical path."""


@dataclass(frozen=True)
class _Entry:
    kind: str
    message_id: Optional[int]


@dataclass(frozen=True)
class NodeMismatch:
    path: str
    field: str
    source_value: object
    target_value: object


@dataclass(frozen=True)
class ReconciliationReport:
    equal: bool
    source_dir_count: int
    source_file_count: int
    target_dir_count: int
    target_file_count: int
    missing_in_target: tuple[str, ...]
    extra_in_target: tuple[str, ...]
    mismatches: tuple[NodeMismatch, ...]
    source_descriptor: Optional[SourceDescriptor] = None
    target_descriptor: Optional[TargetDescriptor] = None
    completed_at: Optional[str] = None


def reconcile(
    source: TGFSDirectory,
    target: TGFSDirectory,
    *,
    source_descriptor: Optional[SourceDescriptor] = None,
    target_descriptor: Optional[TargetDescriptor] = None,
) -> ReconciliationReport:
    """A structured diff of two directory trees, read-only on both sides."""
    source_entries = flatten(source)
    target_entries = flatten(target)

    missing = tuple(sorted(set(source_entries) - set(target_entries)))
    extra = tuple(sorted(set(target_entries) - set(source_entries)))
    mismatches = tuple(
        mismatch
        for path in sorted(set(source_entries) & set(target_entries))
        for mismatch in _compare(path, source_entries[path], target_entries[path])
    )

    return ReconciliationReport(
        equal=not missing and not extra and not mismatches,
        source_dir_count=_count(source_entries, _DIRECTORY),
        source_file_count=_count(source_entries, _FILE_REF),
        target_dir_count=_count(target_entries, _DIRECTORY),
        target_file_count=_count(target_entries, _FILE_REF),
        missing_in_target=missing,
        extra_in_target=extra,
        mismatches=mismatches,
        source_descriptor=source_descriptor,
        target_descriptor=target_descriptor,
        completed_at=utc_now(),
    )


def _compare(path: str, source: _Entry, target: _Entry) -> tuple[NodeMismatch, ...]:
    if source.kind != target.kind:
        return (NodeMismatch(path, "kind", source.kind, target.kind),)
    if source.message_id != target.message_id:
        return (
            NodeMismatch(path, "message_id", source.message_id, target.message_id),
        )
    return ()


def _count(entries: dict[str, _Entry], kind: str) -> int:
    return sum(1 for entry in entries.values() if entry.kind == kind)


def flatten(directory: TGFSDirectory, path: str = "") -> dict[str, _Entry]:
    """Every node under `directory`, by its logical path.

    Raises `AmbiguousPath` rather than letting a later entry silently
    overwrite an earlier one: a tree that assigns two nodes to the same path
    is not a tree reconciliation (or an import) can read unambiguously.
    """
    entries: dict[str, _Entry] = {}

    for child in directory.find_dirs():
        child_path = f"{path}/{child.name}"
        _record(entries, child_path, _Entry(kind=_DIRECTORY, message_id=None))
        entries.update(flatten(child, child_path))

    for file_ref in directory.find_files():
        file_path = f"{path}/{file_ref.name}"
        _record(
            entries, file_path, _Entry(kind=_FILE_REF, message_id=file_ref.message_id)
        )

    return entries


def _record(entries: dict[str, _Entry], path: str, entry: _Entry) -> None:
    if path in entries:
        raise AmbiguousPath(
            f"{path!r} is assigned to more than one node: cannot reconcile "
            "an ambiguous tree"
        )
    entries[path] = entry


__all__ = ["AmbiguousPath", "NodeMismatch", "ReconciliationReport", "flatten", "reconcile"]
