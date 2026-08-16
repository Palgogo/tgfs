"""Loading a fully loaded source tree into an empty SQLite target.

Read-only on the source, on purpose: the caller may well be pointing this at
the object `GithubRepoMetadataRepository.get()` returns, and that object's own
mutating methods reach out to GitHub. Only `TGFSDirectory`'s plain read API
(`find_dirs`/`find_files`) is used here, so a source backed by anything at all
is walked without it ever being asked to change.

The target is P1c/P1b exactly as they already are - a `SqliteMetadataRepository`
over a path this module is handed, not one the running application knows about.
Nothing here is reachable from `Client.create` or any other startup path.
"""

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Union

from tgfs.core.model import TGFSDirectory
from tgfs.core.repository.impl.metadata.sqlite_command_store import SqliteCommandStore
from tgfs.core.repository.impl.metadata.sqlite_metadata import SqliteMetadataRepository

from .reconcile import ReconciliationReport, reconcile


class TargetNotEmpty(Exception):
    """Refused: the target already holds directories or files."""


class ImportFailed(Exception):
    """Refused or aborted: what the import got through before it stopped."""


@dataclass(frozen=True)
class ImportResult:
    source_dir_count: int
    source_file_count: int
    target_dir_count: int
    target_file_count: int
    elapsed_seconds: float
    reopen_elapsed_seconds: float
    durable_revision: int
    reconciliation: ReconciliationReport


@dataclass
class _Progress:
    dirs: int = 0
    files: int = 0


async def import_metadata(
    source: TGFSDirectory, target_path: Union[str, Path]
) -> ImportResult:
    """Translate every node of `source` into P1b commands against a fresh store.

    Refuses a target that is not empty, refuses to leave a failure looking like
    a partial success, and reopens what it wrote before reporting it as done -
    the durable projection, not the in-memory tree just built, is what is
    checked against the source.
    """
    repository = SqliteMetadataRepository(target_path)
    try:
        await repository.init()
        root = repository.root()
        if root.find_dirs() or root.find_files():
            raise TargetNotEmpty(str(target_path))

        progress = _Progress()
        started = time.monotonic()
        try:
            _import_children(source, root, progress)
            await repository.push()
        except Exception as ex:
            raise ImportFailed(
                f"import into {target_path} failed after {progress.dirs} "
                f"directories and {progress.files} file refs: {ex}"
            ) from ex
        elapsed = time.monotonic() - started
    finally:
        await repository.close()

    reopen_started = time.monotonic()
    reopened = SqliteMetadataRepository(target_path)
    try:
        await reopened.init()
        report = reconcile(source, reopened.root())
        reopen_elapsed = time.monotonic() - reopen_started
    finally:
        await reopened.close()

    if not report.equal:
        raise ImportFailed(
            f"the reopened target {target_path} does not match the source: "
            f"{report}"
        )

    revision = await _durable_revision(target_path)

    return ImportResult(
        source_dir_count=report.source_dir_count,
        source_file_count=report.source_file_count,
        target_dir_count=report.target_dir_count,
        target_file_count=report.target_file_count,
        elapsed_seconds=elapsed,
        reopen_elapsed_seconds=reopen_elapsed,
        durable_revision=revision,
        reconciliation=report,
    )


def _import_children(
    source: TGFSDirectory, target: TGFSDirectory, progress: _Progress
) -> None:
    # Files before subdirectories, both sorted by name: a fixed order is what
    # lets two runs of the same source produce the same command sequence.
    for file_ref in sorted(source.find_files(), key=lambda f: f.name):
        target.create_file_ref(file_ref.name, file_ref.message_id)
        progress.files += 1

    for child in sorted(source.find_dirs(), key=lambda d: d.name):
        target_child = target.create_dir(child.name, None)
        progress.dirs += 1
        _import_children(child, target_child, progress)


async def _durable_revision(target_path: Union[str, Path]) -> int:
    store = SqliteCommandStore(target_path)
    try:
        await store.open()
        projection = await store.load()
        return projection.revision
    finally:
        await store.close()


__all__ = ["ImportFailed", "ImportResult", "TargetNotEmpty", "import_metadata"]
