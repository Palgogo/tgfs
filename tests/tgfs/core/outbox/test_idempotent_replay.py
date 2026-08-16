"""Replaying an already acknowledged record must not publish it again."""

from tgfs.core.outbox.consumer import OutboxConsumer
from tgfs.core.outbox.types import Cursor

from .fakes import FakeOutboxSource, FakeTransport, record


async def test_an_acknowledged_record_is_not_republished() -> None:
    source = FakeOutboxSource(
        [record(1, "op-1", acknowledged=True), record(2, "op-2")]
    )
    transport = FakeTransport()
    consumer = OutboxConsumer(source=source, transport=transport)

    outcome = await consumer.consume_batch(after=Cursor(0), limit=10)

    assert [event_id for event_id, _ in transport.calls] == ["op-2"]
    assert outcome.attempted == ("op-2",)
    assert outcome.acknowledged == ("op-1", "op-2")


async def test_replaying_a_fully_acknowledged_batch_makes_no_publish_attempt() -> None:
    source = FakeOutboxSource([record(1, "op-1", acknowledged=True)])
    transport = FakeTransport()
    consumer = OutboxConsumer(source=source, transport=transport)

    outcome = await consumer.consume_batch(after=Cursor(0), limit=10)

    assert transport.calls == []
    assert outcome.attempted == ()
    assert outcome.acknowledged == ("op-1",)
