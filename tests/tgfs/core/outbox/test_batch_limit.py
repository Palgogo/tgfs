"""A batch limit that is not a positive, finite bound is refused outright.

`limit` becomes a SQL `LIMIT` at the SQLite boundary, where zero or negative
values do not mean "nothing" - SQLite treats a negative `LIMIT` as no limit
at all, which would turn a bounded batch read into an unbounded one. The
consumer refuses such a limit before the source or transport is ever
touched, so the contract is enforced at the call the caller actually made.
"""

import pytest

from tgfs.core.outbox.consumer import OutboxConsumer
from tgfs.core.outbox.types import Cursor

from .fakes import FakeOutboxSource, FakeTransport, record


async def test_a_zero_limit_is_rejected_before_reading_the_source() -> None:
    source = FakeOutboxSource([record(1, "op-1")])
    transport = FakeTransport()
    consumer = OutboxConsumer(source=source, transport=transport)

    with pytest.raises(ValueError):
        await consumer.consume_batch(after=Cursor(0), limit=0)

    assert source.read_calls == []
    assert transport.calls == []


async def test_a_negative_limit_is_rejected_before_reading_the_source() -> None:
    source = FakeOutboxSource([record(1, "op-1")])
    transport = FakeTransport()
    consumer = OutboxConsumer(source=source, transport=transport)

    with pytest.raises(ValueError):
        await consumer.consume_batch(after=Cursor(0), limit=-1)

    assert source.read_calls == []
    assert transport.calls == []
