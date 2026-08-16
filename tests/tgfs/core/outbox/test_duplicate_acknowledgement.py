"""A transport that reports a duplicate delivery is treated as a success.

The event was already delivered by the transport's own account, so the
consumer acknowledges it in durable state exactly as it would a fresh
delivery - it must not be classified as a failure or retried.
"""

from tgfs.core.outbox.consumer import OutboxConsumer
from tgfs.core.outbox.transport import DeliveryOutcome
from tgfs.core.outbox.types import Cursor

from .fakes import FakeOutboxSource, FakeTransport, record


async def test_a_duplicate_delivery_acknowledgement_counts_as_success() -> None:
    source = FakeOutboxSource([record(1, "op-1"), record(2, "op-2")])
    transport = FakeTransport({"op-1": DeliveryOutcome.ALREADY_DELIVERED})
    consumer = OutboxConsumer(source=source, transport=transport)

    outcome = await consumer.consume_batch(after=Cursor(0), limit=10)

    assert outcome.acknowledged == ("op-1", "op-2")
    assert outcome.retryable_failures == ()
    assert outcome.terminal_failures == ()
    assert source.acknowledge_calls == [Cursor(1), Cursor(2)]
