"""Bridging P2a's durable outbox to a `RemoteSnapshotProtocol` remote.

`ExportOrchestrator.export_batch` reads one bounded batch through the same
narrow `OutboxSource` port P2a's own consumer reads through, maps every
still-pending record into an `ExportEnvelope`, and conditionally applies the
batch to the injected remote. A record is only ever acknowledged in the
outbox after the remote has confirmed acceptance - `ACCEPTED` or
`IDEMPOTENT` - so a caller can always retry a failed or ambiguous call from
the returned cursor without risking a duplicate remote commit or a record
that is acknowledged locally but was never actually exported. Like the
resume cursor, `expected_snapshot` is handed in and out explicitly rather
than tracked internally, so the caller - not this class - owns the
durability of where it left off.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

from tgfs.core.outbox.types import Cursor, OutboxSource

from .envelope import EnvelopeValidationError, SourceEvent, build_envelope
from .protocol import (
    ApplyStatus,
    RemoteApplyError,
    RemoteSnapshotProtocol,
    SnapshotId,
)


class ExportStatus(Enum):
    EMPTY = "empty"
    ACCEPTED = "accepted"
    IDEMPOTENT = "idempotent"
    CONFLICT = "conflict"
    REJECTED = "rejected"
    MALFORMED = "malformed"
    TERMINAL_ERROR = "terminal_error"


_APPLY_STATUS_TO_EXPORT_STATUS = {
    ApplyStatus.ACCEPTED: ExportStatus.ACCEPTED,
    ApplyStatus.IDEMPOTENT: ExportStatus.IDEMPOTENT,
    ApplyStatus.CONFLICT: ExportStatus.CONFLICT,
    ApplyStatus.REJECTED: ExportStatus.REJECTED,
}


@dataclass(frozen=True)
class ExportOutcome:
    status: ExportStatus
    reason: str
    attempted_event_ids: Tuple[str, ...]
    acknowledged_event_ids: Tuple[str, ...]
    next_cursor: Cursor
    next_snapshot: Optional[SnapshotId]


class ExportOrchestrator:
    def __init__(
        self,
        *,
        source: OutboxSource,
        remote: RemoteSnapshotProtocol,
        event_type: str,
    ):
        self._source = source
        self._remote = remote
        self._event_type = event_type

    async def export_batch(
        self,
        *,
        after: Cursor,
        limit: int,
        batch_id: str,
        expected_snapshot: SnapshotId,
    ) -> ExportOutcome:
        batch = await self._source.read_batch(after, limit)
        cursor = batch.records[-1].sequence if batch.records else after
        already_acknowledged = tuple(
            r.event_id for r in batch.records if r.acknowledged
        )
        pending = [r for r in batch.records if not r.acknowledged]

        if not pending:
            return ExportOutcome(
                status=ExportStatus.EMPTY,
                reason="",
                attempted_event_ids=(),
                acknowledged_event_ids=already_acknowledged,
                next_cursor=cursor,
                next_snapshot=None,
            )

        attempted = tuple(r.event_id for r in pending)

        try:
            envelopes = tuple(
                build_envelope(
                    SourceEvent(
                        event_id=r.event_id,
                        sequence=r.sequence,
                        event_type=self._event_type,
                        payload=r.payload,
                    )
                )
                for r in pending
            )
        except EnvelopeValidationError as e:
            return ExportOutcome(
                status=ExportStatus.MALFORMED,
                reason=str(e),
                attempted_event_ids=attempted,
                acknowledged_event_ids=already_acknowledged,
                next_cursor=after,
                next_snapshot=None,
            )

        try:
            result = await self._remote.apply(
                batch_id=batch_id,
                expected_snapshot=expected_snapshot,
                envelopes=envelopes,
            )
        except RemoteApplyError as e:
            return ExportOutcome(
                status=ExportStatus.TERMINAL_ERROR,
                reason=str(e),
                attempted_event_ids=attempted,
                acknowledged_event_ids=already_acknowledged,
                next_cursor=after,
                next_snapshot=None,
            )

        status = _APPLY_STATUS_TO_EXPORT_STATUS[result.status]

        if result.status not in (ApplyStatus.ACCEPTED, ApplyStatus.IDEMPOTENT):
            return ExportOutcome(
                status=status,
                reason=result.reason,
                attempted_event_ids=attempted,
                acknowledged_event_ids=already_acknowledged,
                next_cursor=after,
                next_snapshot=result.snapshot,
            )

        for r in pending:
            await self._source.acknowledge(r.sequence)

        return ExportOutcome(
            status=status,
            reason=result.reason,
            attempted_event_ids=attempted,
            acknowledged_event_ids=already_acknowledged + attempted,
            next_cursor=cursor,
            next_snapshot=result.snapshot,
        )
