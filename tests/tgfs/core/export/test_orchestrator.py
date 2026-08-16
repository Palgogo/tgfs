"""The local orchestrator between P2a's outbox and the fake remote.

`ExportOrchestrator.export_batch` reads a bounded batch through the same
`OutboxSource` port P2a's consumer reads through, maps pending records into
envelopes, and conditionally applies them to a `RemoteSnapshotProtocol`. A
record is acknowledged in the outbox only after the remote confirms
acceptance - never on a conflict, a rejection, a malformed envelope, or a
terminal remote error - so a caller can always retry safely from the
returned cursor.
"""

from tgfs.core.export.envelope import ExportEnvelope, SourceEvent, build_envelope
from tgfs.core.export.fake_remote import InMemoryFakeRemote
from tgfs.core.export.orchestrator import ExportOrchestrator, ExportStatus
from tgfs.core.outbox.types import Cursor

from tests.tgfs.core.outbox.fakes import FakeOutboxSource, record

from .fakes import BrokenRemote

EVENT_TYPE = "metadata.command"


async def test_a_pending_batch_is_applied_and_acknowledged_only_after_acceptance() -> (
    None
):
    source = FakeOutboxSource([record(1, "op-1"), record(2, "op-2")])
    remote = InMemoryFakeRemote()
    orchestrator = ExportOrchestrator(
        source=source, remote=remote, event_type=EVENT_TYPE
    )
    genesis = await remote.current_snapshot()

    outcome = await orchestrator.export_batch(
        after=Cursor(0), limit=10, batch_id="batch-1", expected_snapshot=genesis
    )

    assert outcome.status is ExportStatus.ACCEPTED
    assert outcome.acknowledged_event_ids == ("op-1", "op-2")
    assert source.acknowledge_calls == [Cursor(1), Cursor(2)]
    assert len(remote.committed_events) == 2
    assert outcome.next_snapshot == await remote.current_snapshot()


async def test_a_retry_after_the_local_ack_is_lost_gets_an_idempotent_ack_with_no_duplicate() -> (
    None
):
    records = [record(1, "op-1"), record(2, "op-2")]
    remote = InMemoryFakeRemote()
    genesis = await remote.current_snapshot()

    first_orchestrator = ExportOrchestrator(
        source=FakeOutboxSource(list(records)), remote=remote, event_type=EVENT_TYPE
    )
    first = await first_orchestrator.export_batch(
        after=Cursor(0), limit=10, batch_id="batch-1", expected_snapshot=genesis
    )

    # Simulate a crash before the local ack was durably recorded: a fresh
    # source starts from the same still-unacknowledged records, and the
    # caller still only has the pre-apply snapshot it started with.
    second_source = FakeOutboxSource(list(records))
    second_orchestrator = ExportOrchestrator(
        source=second_source, remote=remote, event_type=EVENT_TYPE
    )
    second = await second_orchestrator.export_batch(
        after=Cursor(0), limit=10, batch_id="batch-1", expected_snapshot=genesis
    )

    assert first.status is ExportStatus.ACCEPTED
    assert second.status is ExportStatus.IDEMPOTENT
    assert second.acknowledged_event_ids == ("op-1", "op-2")
    assert second_source.acknowledge_calls == [Cursor(1), Cursor(2)]
    assert len(remote.committed_events) == 2


async def test_a_stale_expected_snapshot_is_a_conflict_and_acknowledges_nothing() -> (
    None
):
    remote = InMemoryFakeRemote()
    genesis = await remote.current_snapshot()
    await remote.apply(
        batch_id="unrelated",
        expected_snapshot=genesis,
        envelopes=(_raw_envelope(1, "other-op"),),
    )

    source = FakeOutboxSource([record(1, "op-1")])
    orchestrator = ExportOrchestrator(
        source=source, remote=remote, event_type=EVENT_TYPE
    )

    outcome = await orchestrator.export_batch(
        after=Cursor(0), limit=10, batch_id="batch-1", expected_snapshot=genesis
    )

    assert outcome.status is ExportStatus.CONFLICT
    assert outcome.acknowledged_event_ids == ()
    assert source.acknowledge_calls == []


async def test_a_malformed_payload_is_reported_without_reaching_the_remote() -> None:
    source = FakeOutboxSource([record(1, "op-1", payload="not-json")])
    remote = InMemoryFakeRemote()
    orchestrator = ExportOrchestrator(
        source=source, remote=remote, event_type=EVENT_TYPE
    )
    genesis = await remote.current_snapshot()

    outcome = await orchestrator.export_batch(
        after=Cursor(0), limit=10, batch_id="batch-1", expected_snapshot=genesis
    )

    assert outcome.status is ExportStatus.MALFORMED
    assert outcome.acknowledged_event_ids == ()
    assert source.acknowledge_calls == []
    assert remote.committed_events == ()


async def test_a_terminal_remote_error_is_reported_and_acknowledges_nothing() -> None:
    source = FakeOutboxSource([record(1, "op-1")])
    remote = BrokenRemote()
    orchestrator = ExportOrchestrator(
        source=source, remote=remote, event_type=EVENT_TYPE
    )

    outcome = await orchestrator.export_batch(
        after=Cursor(0),
        limit=10,
        batch_id="batch-1",
        expected_snapshot=await remote.current_snapshot(),
    )

    assert outcome.status is ExportStatus.TERMINAL_ERROR
    assert outcome.acknowledged_event_ids == ()
    assert source.acknowledge_calls == []


async def test_a_later_batch_conflict_does_not_disturb_an_earlier_committed_batch() -> (
    None
):
    records = [record(1, "op-1"), record(2, "op-2"), record(3, "op-3")]
    source = FakeOutboxSource(list(records))
    remote = InMemoryFakeRemote()
    orchestrator = ExportOrchestrator(
        source=source, remote=remote, event_type=EVENT_TYPE
    )
    genesis = await remote.current_snapshot()

    first = await orchestrator.export_batch(
        after=Cursor(0), limit=2, batch_id="batch-1", expected_snapshot=genesis
    )
    assert first.next_snapshot is not None
    # A caller bug: it retries the next batch against the pre-first-batch
    # snapshot instead of `first.next_snapshot`.
    second = await orchestrator.export_batch(
        after=first.next_cursor, limit=2, batch_id="batch-2", expected_snapshot=genesis
    )
    third = await orchestrator.export_batch(
        after=second.next_cursor,
        limit=2,
        batch_id="batch-2",
        expected_snapshot=first.next_snapshot,
    )

    assert first.status is ExportStatus.ACCEPTED
    assert first.acknowledged_event_ids == ("op-1", "op-2")
    assert second.status is ExportStatus.CONFLICT
    assert second.acknowledged_event_ids == ()
    assert third.status is ExportStatus.ACCEPTED
    assert third.acknowledged_event_ids == ("op-3",)
    assert source.acknowledge_calls == [Cursor(1), Cursor(2), Cursor(3)]


def _raw_envelope(sequence: int, event_id: str) -> ExportEnvelope:
    return build_envelope(
        SourceEvent(
            event_id=event_id,
            sequence=sequence,
            event_type=EVENT_TYPE,
            payload="{}",
        )
    )
