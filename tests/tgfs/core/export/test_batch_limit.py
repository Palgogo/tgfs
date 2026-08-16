"""A batch limit that is not a positive, finite bound is refused outright.

Mirrors P2a's own `tests/tgfs/core/outbox/test_batch_limit.py`: `limit`
eventually becomes a bounded read at the source, where zero or negative
values must not silently turn into an unbounded scan. The orchestrator
refuses such a limit before the source, the remote, or acknowledgement is
ever touched.
"""

import pytest

from tgfs.core.export.fake_remote import InMemoryFakeRemote
from tgfs.core.export.orchestrator import ExportOrchestrator
from tgfs.core.outbox.types import Cursor

from tests.tgfs.core.outbox.fakes import FakeOutboxSource, record

EVENT_TYPE = "metadata.command"


async def test_a_zero_limit_is_rejected_before_reading_the_source() -> None:
    source = FakeOutboxSource([record(1, "op-1")])
    remote = InMemoryFakeRemote()
    orchestrator = ExportOrchestrator(
        source=source, remote=remote, event_type=EVENT_TYPE
    )
    genesis = await remote.current_snapshot()

    with pytest.raises(ValueError):
        await orchestrator.export_batch(
            after=Cursor(0), limit=0, batch_id="batch-1", expected_snapshot=genesis
        )

    assert source.read_calls == []
    assert source.acknowledge_calls == []
    assert remote.committed_events == ()
    assert await remote.current_snapshot() == genesis


async def test_a_negative_limit_is_rejected_before_reading_the_source() -> None:
    source = FakeOutboxSource([record(1, "op-1")])
    remote = InMemoryFakeRemote()
    orchestrator = ExportOrchestrator(
        source=source, remote=remote, event_type=EVENT_TYPE
    )
    genesis = await remote.current_snapshot()

    with pytest.raises(ValueError):
        await orchestrator.export_batch(
            after=Cursor(0), limit=-1, batch_id="batch-1", expected_snapshot=genesis
        )

    assert source.read_calls == []
    assert source.acknowledge_calls == []
    assert remote.committed_events == ()
    assert await remote.current_snapshot() == genesis
