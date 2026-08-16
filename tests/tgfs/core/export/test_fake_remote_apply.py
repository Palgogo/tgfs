"""The in-memory fake remote accepts a valid batch and moves the snapshot on.

This is the base success path the rest of the contract's error cases are
defined against: given the remote's own current snapshot identity as the
expected one, and a well-formed, contiguous, in-limit batch of envelopes,
`apply` commits them, returns `ACCEPTED`, and the remote's snapshot identity
that `current_snapshot()` reports afterward is the new one from the result -
never the same value twice for two different accepted batches.
"""

from tgfs.core.export.envelope import ExportEnvelope, SourceEvent, build_envelope
from tgfs.core.export.fake_remote import InMemoryFakeRemote
from tgfs.core.export.protocol import ApplyStatus


def _envelope(sequence: int, event_id: str = "") -> ExportEnvelope:
    return build_envelope(
        SourceEvent(
            event_id=event_id or f"op-{sequence}",
            sequence=sequence,
            event_type="metadata.command",
            payload="{}",
        )
    )


async def test_a_fresh_remote_has_a_starting_snapshot_identity() -> None:
    remote = InMemoryFakeRemote()

    snapshot = await remote.current_snapshot()

    assert snapshot is not None


async def test_a_valid_batch_against_the_current_snapshot_is_accepted() -> None:
    remote = InMemoryFakeRemote()
    starting = await remote.current_snapshot()

    result = await remote.apply(
        batch_id="batch-1",
        expected_snapshot=starting,
        envelopes=(_envelope(1), _envelope(2)),
    )

    assert result.status is ApplyStatus.ACCEPTED
    assert result.snapshot != starting


async def test_the_remote_snapshot_advances_after_a_successful_apply() -> None:
    remote = InMemoryFakeRemote()
    starting = await remote.current_snapshot()

    result = await remote.apply(
        batch_id="batch-1", expected_snapshot=starting, envelopes=(_envelope(1),)
    )

    assert await remote.current_snapshot() == result.snapshot


async def test_two_successive_accepted_batches_produce_two_distinct_snapshots() -> (
    None
):
    remote = InMemoryFakeRemote()
    first_expected = await remote.current_snapshot()

    first = await remote.apply(
        batch_id="batch-1", expected_snapshot=first_expected, envelopes=(_envelope(1),)
    )
    second = await remote.apply(
        batch_id="batch-2",
        expected_snapshot=first.snapshot,
        envelopes=(_envelope(2),),
    )

    assert first.snapshot != second.snapshot
    assert await remote.current_snapshot() == second.snapshot
