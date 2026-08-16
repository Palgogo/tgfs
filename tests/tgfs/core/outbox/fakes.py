"""Local-only test doubles for the outbox consumer.

`FakeOutboxSource` holds records in memory and answers `read_batch` the way a
durable source must: bounded, in sequence order, honoring what has been
acknowledged. `FakeTransport` is scripted per event id - the tests decide
whether a given event succeeds, fails transiently, fails terminally, or comes
back as a duplicate-delivery acknowledgement.
"""

import dataclasses
from typing import Dict, List, Optional, Tuple, Union

from tgfs.core.outbox.transport import DeliveryOutcome
from tgfs.core.outbox.types import BatchResult, Cursor, OutboxRecord

Script = Union[DeliveryOutcome, Exception]


def record(
    sequence: int, event_id: str = "", payload: str = "{}", acknowledged: bool = False
) -> OutboxRecord:
    return OutboxRecord(
        sequence=Cursor(sequence),
        event_id=event_id or f"op-{sequence}",
        payload=payload,
        acknowledged=acknowledged,
    )


class FakeOutboxSource:
    def __init__(self, records: List[OutboxRecord]):
        self._records: Dict[Cursor, OutboxRecord] = {r.sequence: r for r in records}
        self._acknowledged: set = {r.sequence for r in records if r.acknowledged}
        self.read_calls: List[Tuple[Cursor, int]] = []
        self.acknowledge_calls: List[Cursor] = []

    async def read_batch(self, after: Cursor, limit: int) -> BatchResult:
        self.read_calls.append((after, limit))
        selected = sorted(
            (r for r in self._records.values() if r.sequence > after),
            key=lambda r: r.sequence,
        )[:limit]
        resolved = tuple(
            dataclasses.replace(r, acknowledged=r.sequence in self._acknowledged)
            for r in selected
        )
        next_cursor = resolved[-1].sequence if resolved else after
        return BatchResult(records=resolved, next_cursor=next_cursor)

    async def acknowledge(self, sequence: Cursor) -> None:
        self.acknowledge_calls.append(sequence)
        self._acknowledged.add(sequence)


class FakeTransport:
    def __init__(self, scripts: Optional[Dict[str, Script]] = None):
        self._scripts = dict(scripts or {})
        self.calls: List[Tuple[str, str]] = []

    async def publish(self, event_id: str, payload: str) -> DeliveryOutcome:
        self.calls.append((event_id, payload))
        outcome = self._scripts.get(event_id, DeliveryOutcome.DELIVERED)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
