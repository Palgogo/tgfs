"""Test doubles for export remote failure paths.

These types exist only under tests and model terminal, retryable, scripted,
and unclassified remote behaviour for orchestrator coverage.
"""

from typing import Tuple

from tgfs.core.export.envelope import ExportEnvelope
from tgfs.core.export.protocol import (
    ApplyResult,
    RemoteApplyError,
    RemoteUnavailableError,
    RemoteSnapshotProtocol,
    SnapshotId,
)


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


class UnavailableRemote:
    def __init__(
        self,
        *,
        reason: str = "remote_unavailable",
        snapshot: SnapshotId = SnapshotId("genesis"),
    ) -> None:
        self._error = RemoteUnavailableError(reason)
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
        raise self._error


class FailThenDelegateRemote:
    def __init__(
        self,
        inner: RemoteSnapshotProtocol,
        *,
        fail_count: int = 0,
        fail_after_commit: bool = False,
        reason: str = "remote_unavailable",
    ) -> None:
        self._inner = inner
        self._fail_count = fail_count
        self._fail_after_commit = fail_after_commit
        self._error = RemoteUnavailableError(reason)
        self._attempts = 0
        self._did_raise_after_commit = False

    async def current_snapshot(self) -> SnapshotId:
        return await self._inner.current_snapshot()

    async def apply(
        self,
        *,
        batch_id: str,
        expected_snapshot: SnapshotId,
        envelopes: Tuple[ExportEnvelope, ...],
    ) -> ApplyResult:
        self._attempts += 1
        if self._fail_after_commit:
            result = await self._inner.apply(
                batch_id=batch_id,
                expected_snapshot=expected_snapshot,
                envelopes=envelopes,
            )
            if not self._did_raise_after_commit:
                self._did_raise_after_commit = True
                raise self._error
            return result
        if self._attempts <= self._fail_count:
            raise self._error
        return await self._inner.apply(
            batch_id=batch_id,
            expected_snapshot=expected_snapshot,
            envelopes=envelopes,
        )


class PropagatingRemote:
    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def current_snapshot(self) -> SnapshotId:
        return SnapshotId("genesis")

    async def apply(
        self,
        *,
        batch_id: str,
        expected_snapshot: SnapshotId,
        envelopes: Tuple[ExportEnvelope, ...],
    ) -> ApplyResult:
        raise self._error
