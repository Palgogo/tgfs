"""Cursor progress across successive calls, each bounded by limit."""

from tgfs.core.outbox.consumer import OutboxConsumer
from tgfs.core.outbox.types import Cursor

from .fakes import FakeOutboxSource, FakeTransport, record


async def test_a_second_call_resumes_from_the_returned_cursor() -> None:
    source = FakeOutboxSource(
        [record(1, "op-1"), record(2, "op-2"), record(3, "op-3"), record(4, "op-4")]
    )
    transport = FakeTransport()
    consumer = OutboxConsumer(source=source, transport=transport)

    first = await consumer.consume_batch(after=Cursor(0), limit=2)
    second = await consumer.consume_batch(after=first.next_cursor, limit=2)

    assert first.acknowledged == ("op-1", "op-2")
    assert second.acknowledged == ("op-3", "op-4")
    assert source.read_calls == [(Cursor(0), 2), (Cursor(2), 2)]
    assert second.next_cursor == Cursor(4)


async def test_a_cursor_past_every_record_reads_an_empty_batch() -> None:
    source = FakeOutboxSource([record(1, "op-1")])
    consumer = OutboxConsumer(source=source, transport=FakeTransport())

    outcome = await consumer.consume_batch(after=Cursor(1), limit=10)

    assert outcome.attempted == ()
    assert outcome.acknowledged == ()
    assert outcome.next_cursor == Cursor(1)
