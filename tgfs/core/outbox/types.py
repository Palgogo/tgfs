"""The shape a durable outbox is read through.

`Cursor` is a durable outbox sequence number: the same value a store like
P1b's `SqliteCommandStore` hands out as `outbox.sequence`, so a consumer's
resume point is always something the durable side already understands. An
`OutboxRecord` is one durable entry as a reader is allowed to see it - a
stable event identity, its serialized payload, and whether durable state
already says it was acknowledged. A `BatchResult` is a bounded read in
sequence order, plus the cursor a following read would start from if this
whole batch were consumed.

`OutboxSource` is the one abstraction the consumer depends on to reach durable
data. It is deliberately narrow: read a batch, acknowledge a sequence. A
concrete implementation may be backed by SQLite or by an in-memory fake, but
the consumer never sees the difference and never bypasses this to touch a
store's internals itself.
"""

from dataclasses import dataclass
from typing import NewType, Protocol

Cursor = NewType("Cursor", int)


@dataclass(frozen=True)
class OutboxRecord:
    sequence: Cursor
    event_id: str
    payload: str
    acknowledged: bool


@dataclass(frozen=True)
class BatchResult:
    records: tuple[OutboxRecord, ...]
    next_cursor: Cursor


class OutboxSource(Protocol):
    async def read_batch(self, after: Cursor, limit: int) -> BatchResult: ...

    async def acknowledge(self, sequence: Cursor) -> None: ...
