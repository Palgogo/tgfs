"""Consuming a batch with nothing in it does nothing.

No record means no publish attempt, no acknowledgement, and no failure of
either kind. The fakes below raise if the consumer touches them at all, so a
consumer that tried to do something with an empty batch would fail loudly
rather than merely being unasserted.
"""

from tgfs.core.outbox.consumer import OutboxConsumer
from tgfs.core.outbox.transport import DeliveryOutcome
from tgfs.core.outbox.types import BatchResult, Cursor


class _EmptySource:
    async def read_batch(self, after: Cursor, limit: int) -> BatchResult:
        return BatchResult(records=(), next_cursor=after)

    async def acknowledge(self, sequence: Cursor) -> None:
        raise AssertionError("acknowledge should not be called for an empty batch")


class _NoCallTransport:
    async def publish(self, event_id: str, payload: str) -> DeliveryOutcome:
        raise AssertionError("publish should not be called for an empty batch")


async def test_consuming_an_empty_batch_reports_nothing_attempted() -> None:
    consumer = OutboxConsumer(source=_EmptySource(), transport=_NoCallTransport())

    outcome = await consumer.consume_batch(after=Cursor(0), limit=10)

    assert outcome.attempted == ()
    assert outcome.acknowledged == ()
    assert outcome.next_cursor == Cursor(0)
