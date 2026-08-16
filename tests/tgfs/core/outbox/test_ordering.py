"""A batch is published once, in the sequence order it was read in."""

from tgfs.core.outbox.consumer import OutboxConsumer
from tgfs.core.outbox.types import Cursor

from .fakes import FakeOutboxSource, FakeTransport, record


async def test_a_bounded_batch_is_published_in_sequence_order() -> None:
    source = FakeOutboxSource(
        [record(1, "op-1"), record(2, "op-2"), record(3, "op-3")]
    )
    transport = FakeTransport()
    consumer = OutboxConsumer(source=source, transport=transport)

    outcome = await consumer.consume_batch(after=Cursor(0), limit=10)

    assert [event_id for event_id, _ in transport.calls] == ["op-1", "op-2", "op-3"]
    assert outcome.attempted == ("op-1", "op-2", "op-3")
    assert outcome.acknowledged == ("op-1", "op-2", "op-3")
    assert outcome.next_cursor == Cursor(3)


async def test_the_batch_read_is_bounded_by_limit() -> None:
    source = FakeOutboxSource([record(1), record(2), record(3), record(4)])
    consumer = OutboxConsumer(source=source, transport=FakeTransport())

    await consumer.consume_batch(after=Cursor(0), limit=2)

    assert source.read_calls == [(Cursor(0), 2)]
