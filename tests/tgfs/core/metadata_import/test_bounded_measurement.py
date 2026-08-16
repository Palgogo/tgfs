"""The bounded, three-run local characterization artifact for P1d.

Not a production benchmark - one process, one disk, a fixed generated tree
shape - but a real, reproducible measurement of what this importer actually
does with something bigger than a handful of nodes, run three times so a
single outlier is not mistaken for the truth. No live GitHub, no SLO claim.
"""

from pathlib import Path

from tgfs.core.metadata_import.measurement import characterize
from tgfs.core.metadata_import.provenance import SourceDescriptor
from tgfs.core.model import TGFSDirectory

DEPTH = 3
BREADTH = 4
FILES_PER_DIR = 2
BOUND_SECONDS = 30.0

_SOURCE_DESCRIPTOR = SourceDescriptor(
    repository="local/synthetic-fixture", ref="fixed-generated-tree-v1"
)


def _generate(parent: TGFSDirectory, depth: int, prefix: str, counts: dict) -> None:
    if depth == 0:
        return

    for i in range(BREADTH):
        child = parent.create_dir(f"{prefix}dir{i}", None)
        counts["dirs"] += 1

        for j in range(FILES_PER_DIR):
            counts["files"] += 1
            child.create_file_ref(f"file{j}.bin", counts["dirs"] * 1000 + j + 1)

        _generate(child, depth - 1, f"{prefix}{i}_", counts)


def _tree_factory():
    root = TGFSDirectory.root_dir()
    counts = {"dirs": 0, "files": 0}
    _generate(root, DEPTH, "", counts)
    return root, counts["dirs"], counts["files"]


class TestBoundedThreeRunCharacterization:
    async def test_three_runs_report_individual_and_min_median_max_timings(
        self, tmp_path: Path
    ):
        result = await characterize(_tree_factory, tmp_path, _SOURCE_DESCRIPTOR)

        assert len(result.runs) == 3
        assert result.dir_count > 20
        assert all(run.reconciliation_equal for run in result.runs)
        assert all(
            0 <= run.cold_start_seconds < BOUND_SECONDS for run in result.runs
        )
        assert result.min_seconds <= result.median_seconds <= result.max_seconds
        assert result.min_seconds == min(r.cold_start_seconds for r in result.runs)
        assert result.max_seconds == max(r.cold_start_seconds for r in result.runs)

        # Each run got its own fresh target: a real path collision would have
        # raised `TargetPathExists` from inside `characterize` already.
        assert len({run.target_descriptor.path for run in result.runs}) == 3

        print(
            f"\nsource: {_SOURCE_DESCRIPTOR}\n"
            f"python: {result.python_version}\n"
            f"platform: {result.platform}\n"
            f"cache condition: {result.cache_condition}\n"
            f"dirs={result.dir_count} files={result.file_count}\n"
            + "\n".join(
                f"run {i}: target={run.target_descriptor.path} "
                f"cold={run.cold_start_seconds:.3f}s "
                f"import={run.import_seconds:.3f}s "
                f"reopen={run.reopen_seconds:.3f}s "
                f"reconciled={run.reconciliation_equal}"
                for i, run in enumerate(result.runs)
            )
            + f"\nmin={result.min_seconds:.3f}s median={result.median_seconds:.3f}s "
            f"max={result.max_seconds:.3f}s"
        )
