"""A transient failure stops the batch and later events cannot overtake it."""

from tgfs.core.outbox.consumer import OutboxConsumer
from tgfs.core.outbox.transport import TransientTransportError
from tgfs.core.outbox.types import Cursor

from .fakes import FakeOutboxSource, FakeTransport, record


async def test_a_transient_failure_stops_the_ordered_batch() -> None:
    source = FakeOutboxSource(
        [record(1, "op-1"), record(2, "op-2"), record(3, "op-3")]
    )
    transport = FakeTransport({"op-2": TransientTransportError("timed out")})
    consumer = OutboxConsumer(source=source, transport=transport)

    outcome = await consumer.consume_batch(after=Cursor(0), limit=10)

    assert [event_id for event_id, _ in transport.calls] == ["op-1", "op-2"]
    assert outcome.attempted == ("op-1", "op-2")
    assert outcome.acknowledged == ("op-1",)
    assert len(outcome.retryable_failures) == 1
    assert outcome.retryable_failures[0].event_id == "op-2"
    assert outcome.retryable_failures[0].sequence == Cursor(2)
    assert outcome.terminal_failures == ()
    assert outcome.next_cursor == Cursor(1)


async def test_a_transient_failure_preserves_the_same_event_key_on_retry() -> None:
    source = FakeOutboxSource([record(1, "op-1")])
    transport = FakeTransport({"op-1": TransientTransportError("timed out")})
    consumer = OutboxConsumer(source=source, transport=transport)

    outcome = await consumer.consume_batch(after=Cursor(0), limit=10)

    assert outcome.retry_hint is not None
    assert outcome.retry_hint.event_id == "op-1"
    assert outcome.retry_hint.resume_after == Cursor(0)
