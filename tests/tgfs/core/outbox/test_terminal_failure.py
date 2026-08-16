"""A terminal failure is reported and the batch stops without advancing past it.

Terminal failures must not be silently discarded: the event that failed is
named in the outcome, and nothing after it in sequence order is attempted.
"""

from tgfs.core.outbox.consumer import OutboxConsumer
from tgfs.core.outbox.transport import TerminalTransportError
from tgfs.core.outbox.types import Cursor

from .fakes import FakeOutboxSource, FakeTransport, record


async def test_a_terminal_failure_stops_the_batch_and_is_reported() -> None:
    source = FakeOutboxSource(
        [record(1, "op-1"), record(2, "op-2"), record(3, "op-3")]
    )
    transport = FakeTransport({"op-2": TerminalTransportError("payload rejected")})
    consumer = OutboxConsumer(source=source, transport=transport)

    outcome = await consumer.consume_batch(after=Cursor(0), limit=10)

    assert [event_id for event_id, _ in transport.calls] == ["op-1", "op-2"]
    assert outcome.attempted == ("op-1", "op-2")
    assert outcome.acknowledged == ("op-1",)
    assert outcome.retryable_failures == ()
    assert len(outcome.terminal_failures) == 1
    assert outcome.terminal_failures[0].event_id == "op-2"
    assert outcome.terminal_failures[0].sequence == Cursor(2)
    assert outcome.next_cursor == Cursor(1)
    assert outcome.retry_hint is None


async def test_an_unclassified_transport_exception_is_treated_as_terminal() -> None:
    source = FakeOutboxSource([record(1, "op-1")])
    transport = FakeTransport({"op-1": RuntimeError("boom")})
    consumer = OutboxConsumer(source=source, transport=transport)

    outcome = await consumer.consume_batch(after=Cursor(0), limit=10)

    assert outcome.retryable_failures == ()
    assert len(outcome.terminal_failures) == 1
    assert outcome.terminal_failures[0].event_id == "op-1"
    assert outcome.acknowledged == ()
