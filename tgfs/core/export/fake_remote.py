"""An in-memory fake implementing `RemoteSnapshotProtocol`.

This exists to prove the export contract's semantics in tests; it is not a
GitHub or network client and retains everything only in process memory. Its
committed history and snapshot identity are gone the moment the process
exits.
"""

import hashlib
from typing import Dict, List, Tuple

from .envelope import ExportEnvelope
from .protocol import ApplyResult, ApplyStatus, SnapshotId

_GENESIS = SnapshotId("genesis")


def _batch_fingerprint(envelopes: Tuple[ExportEnvelope, ...]) -> bytes:
    digest = hashlib.sha256()
    for envelope in envelopes:
        digest.update(envelope.canonical_bytes())
    return digest.digest()


def _is_strictly_contiguous(envelopes: Tuple[ExportEnvelope, ...]) -> bool:
    for previous, current in zip(envelopes, envelopes[1:]):
        if current.sequence != previous.sequence + 1:
            return False
    return True


_DEFAULT_MAX_BATCH_SIZE = 500


class InMemoryFakeRemote:
    def __init__(self, *, max_batch_size: int = _DEFAULT_MAX_BATCH_SIZE) -> None:
        self._snapshot: SnapshotId = _GENESIS
        self._events: List[ExportEnvelope] = []
        self._committed_batches: Dict[str, Tuple[bytes, SnapshotId]] = {}
        self._max_batch_size = max_batch_size

    @property
    def committed_events(self) -> Tuple[ExportEnvelope, ...]:
        return tuple(self._events)

    async def current_snapshot(self) -> SnapshotId:
        return self._snapshot

    async def apply(
        self,
        *,
        batch_id: str,
        expected_snapshot: SnapshotId,
        envelopes: Tuple[ExportEnvelope, ...],
    ) -> ApplyResult:
        if len(envelopes) <= 0 or len(envelopes) > self._max_batch_size:
            raise ValueError(
                "envelopes must be a non-empty batch bounded by "
                f"{self._max_batch_size}, got {len(envelopes)!r}"
            )

        fingerprint = _batch_fingerprint(envelopes)

        previous = self._committed_batches.get(batch_id)
        if previous is not None:
            previous_fingerprint, resulting_snapshot = previous
            if fingerprint == previous_fingerprint:
                return ApplyResult(
                    status=ApplyStatus.IDEMPOTENT, snapshot=resulting_snapshot
                )
            return ApplyResult(
                status=ApplyStatus.CONFLICT,
                snapshot=self._snapshot,
                reason=f"batch id {batch_id!r} was already committed with different content",
            )

        if not _is_strictly_contiguous(envelopes):
            return ApplyResult(
                status=ApplyStatus.REJECTED,
                snapshot=self._snapshot,
                reason="envelope sequences are not strictly ascending and contiguous",
            )

        if expected_snapshot != self._snapshot:
            return ApplyResult(
                status=ApplyStatus.CONFLICT,
                snapshot=self._snapshot,
                reason="expected snapshot is stale",
            )

        self._events.extend(envelopes)
        self._snapshot = self._advance(envelopes)
        self._committed_batches[batch_id] = (fingerprint, self._snapshot)
        return ApplyResult(status=ApplyStatus.ACCEPTED, snapshot=self._snapshot)

    def _advance(self, envelopes: Tuple[ExportEnvelope, ...]) -> SnapshotId:
        digest = hashlib.sha256(self._snapshot.encode("utf-8"))
        for envelope in envelopes:
            digest.update(envelope.canonical_bytes())
        return SnapshotId(digest.hexdigest())
