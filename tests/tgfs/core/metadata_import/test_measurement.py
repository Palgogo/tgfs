"""Actual, local timings for a synthetic namespace of some size.

This does not claim to predict production scaling - it is one process, one
temp SQLite file, one generated tree - but it does exercise `import_metadata`
against something bigger than a handful of nodes, and it records what the
result says about how long that actually took here.
"""

import time
from dataclasses import dataclass
from pathlib import Path

from tgfs.core.metadata_import.importer import import_metadata
from tgfs.core.metadata_import.provenance import SourceDescriptor
from tgfs.core.model import TGFSDirectory

_SOURCE_DESCRIPTOR = SourceDescriptor(repository="octo/demo", ref="deadbeef")

DEPTH = 3
BREADTH = 5
FILES_PER_DIR = 2

# A generous ceiling meant only to catch a runaway regression in this test
# environment, not a promise about production import time.
BOUND_SECONDS = 30.0


@dataclass
class _Counts:
    dirs: int = 0
    files: int = 0


def _generate(
    parent: TGFSDirectory, depth: int, prefix: str, counts: _Counts
) -> None:
    if depth == 0:
        return

    for i in range(BREADTH):
        child = parent.create_dir(f"{prefix}dir{i}", None)
        counts.dirs += 1

        for j in range(FILES_PER_DIR):
            counts.files += 1
            child.create_file_ref(f"file{j}.bin", counts.dirs * 1000 + j + 1)

        _generate(child, depth - 1, f"{prefix}{i}_", counts)


def _synthetic_tree() -> tuple[TGFSDirectory, _Counts]:
    root = TGFSDirectory.root_dir()
    counts = _Counts()
    _generate(root, DEPTH, "", counts)
    return root, counts


class TestBoundedMeasurement:
    async def test_import_and_reopen_of_a_generated_namespace_reports_real_timings(
        self, target_path: Path
    ):
        source, counts = _synthetic_tree()
        assert counts.dirs > 100  # big enough to be a namespace, not a fixture

        test_started = time.monotonic()
        result = await import_metadata(
            source, target_path, source_descriptor=_SOURCE_DESCRIPTOR
        )
        test_elapsed = time.monotonic() - test_started

        assert result.source_dir_count == counts.dirs
        assert result.source_file_count == counts.files
        assert result.target_dir_count == counts.dirs
        assert result.target_file_count == counts.files
        assert result.reconciliation.equal is True

        assert isinstance(result.elapsed_seconds, float)
        assert isinstance(result.reopen_elapsed_seconds, float)
        assert 0 <= result.elapsed_seconds < BOUND_SECONDS
        assert 0 <= result.reopen_elapsed_seconds < BOUND_SECONDS
        assert test_elapsed < BOUND_SECONDS

        print(
            f"\nsynthetic namespace: {counts.dirs} dirs, {counts.files} files\n"
            f"import elapsed: {result.elapsed_seconds:.3f}s\n"
            f"reopen/load elapsed: {result.reopen_elapsed_seconds:.3f}s\n"
            f"durable revision: {result.durable_revision}"
        )
