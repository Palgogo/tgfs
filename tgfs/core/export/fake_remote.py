"""An in-memory fake implementing `RemoteSnapshotProtocol`.

This exists to prove the export contract's semantics in tests; it is not a
GitHub or network client and retains everything only in process memory. Its
committed history and snapshot identity are gone the moment the process
exits.
"""

import hashlib
from typing import Dict, List, Optional, Tuple

from .envelope import ExportEnvelope
from .protocol import ApplyResult, ApplyStatus, SnapshotId

_GENESIS = SnapshotId("genesis")
_CONTINUITY_REJECTION_REASON = (
    "batch first sequence does not continue export history"
)


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


def _validate_initial_events(
    history_begins_at: int, initial_events: Tuple[ExportEnvelope, ...]
) -> None:
    if not initial_events:
        return
    if initial_events[0].sequence != history_begins_at:
        raise ValueError(
            "initial_events must begin at history_begins_at "
            f"{history_begins_at}, got {initial_events[0].sequence!r}"
        )
    if not _is_strictly_contiguous(initial_events):
        raise ValueError("initial_events must be strictly contiguous")


_DEFAULT_MAX_BATCH_SIZE = 500


class InMemoryFakeRemote:
    def __init__(
        self,
        *,
        max_batch_size: int = _DEFAULT_MAX_BATCH_SIZE,
        history_begins_at: int = 1,
        initial_events: Tuple[ExportEnvelope, ...] = (),
        initial_snapshot: Optional[SnapshotId] = None,
        committed_batches: Optional[Dict[str, Tuple[bytes, SnapshotId]]] = None,
    ) -> None:
        if history_begins_at <= 0:
            raise ValueError(
                "history_begins_at must be a positive integer, "
                f"got {history_begins_at!r}"
            )
        _validate_initial_events(history_begins_at, initial_events)

        self._history_begins_at = history_begins_at
        self._max_batch_size = max_batch_size
        self._events: List[ExportEnvelope] = list(initial_events)
        self._committed_batches: Dict[str, Tuple[bytes, SnapshotId]] = (
            dict(committed_batches) if committed_batches is not None else {}
        )

        if initial_events:
            if initial_snapshot is None:
                snapshot = _GENESIS
                snapshot = self._advance_from(snapshot, tuple(initial_events))
                self._snapshot = snapshot
            else:
                self._snapshot = initial_snapshot
        else:
            self._snapshot = _GENESIS

    @property
    def committed_events(self) -> Tuple[ExportEnvelope, ...]:
        return tuple(self._events)

    def _last_sequence(self) -> int:
        if not self._events:
            return self._history_begins_at - 1
        return self._events[-1].sequence

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

        required_first = self._last_sequence() + 1
        if envelopes[0].sequence != required_first:
            return ApplyResult(
                status=ApplyStatus.REJECTED,
                snapshot=self._snapshot,
                reason=_CONTINUITY_REJECTION_REASON,
            )

        self._events.extend(envelopes)
        self._snapshot = self._advance(envelopes)
        self._committed_batches[batch_id] = (fingerprint, self._snapshot)
        return ApplyResult(status=ApplyStatus.ACCEPTED, snapshot=self._snapshot)

    def _advance(self, envelopes: Tuple[ExportEnvelope, ...]) -> SnapshotId:
        return self._advance_from(self._snapshot, envelopes)

    @staticmethod
    def _advance_from(
        snapshot: SnapshotId, envelopes: Tuple[ExportEnvelope, ...]
    ) -> SnapshotId:
        digest = hashlib.sha256(snapshot.encode("utf-8"))
        for envelope in envelopes:
            digest.update(envelope.canonical_bytes())
        return SnapshotId(digest.hexdigest())
