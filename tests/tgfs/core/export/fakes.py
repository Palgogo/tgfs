"""A local-only test double for a remote that fails in an unrecoverable way.

`BrokenRemote` always raises `RemoteApplyError` from `apply` - it models a
genuinely terminal remote failure, distinct from the ordinary `CONFLICT`/
`REJECTED` outcomes `InMemoryFakeRemote` itself produces. It is not part of
the export contract's deliverable; it exists only to drive the orchestrator
through its terminal-error path in tests.
"""

from typing import Tuple

from tgfs.core.export.envelope import ExportEnvelope
from tgfs.core.export.protocol import ApplyResult, RemoteApplyError, SnapshotId


class BrokenRemote:
    def __init__(self, *, snapshot: SnapshotId = SnapshotId("genesis")) -> None:
        self._snapshot = snapshot

    async def current_snapshot(self) -> SnapshotId:
        return self._snapshot

    async def apply(
        self,
        *,
        batch_id: str,
        expected_snapshot: SnapshotId,
        envelopes: Tuple[ExportEnvelope, ...],
    ) -> ApplyResult:
        raise RemoteApplyError("remote is unavailable")
