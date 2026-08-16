"""The narrow injected port a remote metadata snapshot is reached through.

`RemoteSnapshotProtocol` is deliberately small: read the remote's current
immutable snapshot identity, and conditionally apply a bounded, ordered batch
of envelopes against an expected snapshot identity, keyed by a caller-chosen
idempotency batch identity. It exposes no GitHub concepts, no credentials,
and no network detail - a concrete implementation (the in-memory fake here,
a real remote later) is free to vary those without this port changing.
"""

from dataclasses import dataclass
from enum import Enum
from typing import NewType, Protocol, Tuple

from .envelope import ExportEnvelope

SnapshotId = NewType("SnapshotId", str)


class ApplyStatus(Enum):
    ACCEPTED = "accepted"
    IDEMPOTENT = "idempotent"
    CONFLICT = "conflict"
    REJECTED = "rejected"


@dataclass(frozen=True)
class ApplyResult:
    status: ApplyStatus
    snapshot: SnapshotId
    reason: str = ""


class RemoteApplyError(Exception):
    """The remote definitely did not apply the batch and will not on retry."""


class RemoteSnapshotProtocol(Protocol):
    async def current_snapshot(self) -> SnapshotId: ...

    async def apply(
        self,
        *,
        batch_id: str,
        expected_snapshot: SnapshotId,
        envelopes: Tuple[ExportEnvelope, ...],
    ) -> ApplyResult: ...
