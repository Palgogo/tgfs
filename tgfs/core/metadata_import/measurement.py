"""A local, synthetic, three-run timing characterization of `import_metadata`.

Three separate fresh SQLite targets, the same generated source tree shape
each time, one process. This is not a production benchmark - it is what this
one machine actually measured, reported with enough detail (counts, per-run
timings, min/median/max) that nobody downstream mistakes it for one.
"""

import platform
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from tgfs.core.model import TGFSDirectory

from .importer import import_metadata
from .provenance import SourceDescriptor, TargetDescriptor

TreeFactory = Callable[[], tuple[TGFSDirectory, int, int]]


@dataclass(frozen=True)
class RunTiming:
    target_descriptor: TargetDescriptor
    # Cold-start boundary: before the target store is opened/loaded, through
    # the reopen-and-reconcile that proves the projection is ready to read.
    cold_start_seconds: float
    import_seconds: float
    reopen_seconds: float
    reconciliation_equal: bool


@dataclass(frozen=True)
class BoundedCharacterization:
    source_descriptor: SourceDescriptor
    dir_count: int
    file_count: int
    python_version: str
    platform: str
    cache_condition: str
    runs: tuple[RunTiming, ...]
    min_seconds: float
    median_seconds: float
    max_seconds: float


async def characterize(
    tree_factory: TreeFactory,
    target_dir: Path,
    source_descriptor: SourceDescriptor,
    *,
    run_count: int = 3,
) -> BoundedCharacterization:
    """Import the same generated tree shape into `run_count` fresh targets.

    Each run gets its own fresh path under `target_dir`: a target that
    already exists is refused by `import_metadata` itself, so reusing one
    path across runs is not a mistake this could make even by accident.
    """
    runs = []
    dir_count = file_count = 0

    for i in range(run_count):
        source, dir_count, file_count = tree_factory()
        target_path = target_dir / f"characterization-{i}.sqlite3"

        cold_started = time.monotonic()
        result = await import_metadata(
            source, target_path, source_descriptor=source_descriptor
        )
        cold_elapsed = time.monotonic() - cold_started

        runs.append(
            RunTiming(
                target_descriptor=result.target_descriptor,
                cold_start_seconds=cold_elapsed,
                import_seconds=result.elapsed_seconds,
                reopen_seconds=result.reopen_elapsed_seconds,
                reconciliation_equal=result.reconciliation.equal,
            )
        )

    totals = [run.cold_start_seconds for run in runs]

    return BoundedCharacterization(
        source_descriptor=source_descriptor,
        dir_count=dir_count,
        file_count=file_count,
        python_version=sys.version,
        platform=platform.platform(),
        cache_condition=(
            "single process, sequential runs against separate fresh SQLite "
            "files on local disk - later runs may benefit from OS page cache "
            "warmed by earlier ones; this is not a cold-machine measurement"
        ),
        runs=tuple(runs),
        min_seconds=min(totals),
        median_seconds=statistics.median(totals),
        max_seconds=max(totals),
    )


__all__ = ["BoundedCharacterization", "RunTiming", "TreeFactory", "characterize"]
