"""Conditional-apply rejection paths, all of them atomic.

A stale expected snapshot, a batch whose sequences are not strictly
ascending and contiguous, and a batch outside the remote's bounded
cardinality limit are each refused without mutating remote state - the
snapshot identity and committed event count are unchanged by a call that
does not return `ACCEPTED`/`IDEMPOTENT`.
"""

import pytest

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


async def test_a_stale_expected_snapshot_is_a_conflict_with_no_mutation() -> None:
    remote = InMemoryFakeRemote()
    starting = await remote.current_snapshot()
    await remote.apply(
        batch_id="batch-1", expected_snapshot=starting, envelopes=(_envelope(1),)
    )
    current = await remote.current_snapshot()

    result = await remote.apply(
        batch_id="batch-2", expected_snapshot=starting, envelopes=(_envelope(2),)
    )

    assert result.status is ApplyStatus.CONFLICT
    assert result.snapshot == current
    assert await remote.current_snapshot() == current
    assert len(remote.committed_events) == 1


async def test_a_gapped_sequence_batch_is_rejected_without_mutation() -> None:
    remote = InMemoryFakeRemote()
    starting = await remote.current_snapshot()

    result = await remote.apply(
        batch_id="batch-1",
        expected_snapshot=starting,
        envelopes=(_envelope(1), _envelope(3)),
    )

    assert result.status is ApplyStatus.REJECTED
    assert await remote.current_snapshot() == starting
    assert remote.committed_events == ()


async def test_a_reversed_sequence_batch_is_rejected_without_mutation() -> None:
    remote = InMemoryFakeRemote()
    starting = await remote.current_snapshot()

    result = await remote.apply(
        batch_id="batch-1",
        expected_snapshot=starting,
        envelopes=(_envelope(2), _envelope(1)),
    )

    assert result.status is ApplyStatus.REJECTED
    assert await remote.current_snapshot() == starting
    assert remote.committed_events == ()


async def test_a_duplicate_sequence_batch_is_rejected_without_mutation() -> None:
    remote = InMemoryFakeRemote()
    starting = await remote.current_snapshot()

    result = await remote.apply(
        batch_id="batch-1",
        expected_snapshot=starting,
        envelopes=(_envelope(1), _envelope(1, event_id="op-1b")),
    )

    assert result.status is ApplyStatus.REJECTED
    assert await remote.current_snapshot() == starting
    assert remote.committed_events == ()


async def test_an_empty_batch_is_rejected_before_touching_state() -> None:
    remote = InMemoryFakeRemote()
    starting = await remote.current_snapshot()

    with pytest.raises(ValueError):
        await remote.apply(batch_id="batch-1", expected_snapshot=starting, envelopes=())

    assert await remote.current_snapshot() == starting
    assert remote.committed_events == ()


async def test_a_batch_over_the_cardinality_limit_is_rejected_before_touching_state() -> (
    None
):
    remote = InMemoryFakeRemote(max_batch_size=2)
    starting = await remote.current_snapshot()
    envelopes = (_envelope(1), _envelope(2), _envelope(3))

    with pytest.raises(ValueError):
        await remote.apply(
            batch_id="batch-1", expected_snapshot=starting, envelopes=envelopes
        )

    assert await remote.current_snapshot() == starting
    assert remote.committed_events == ()


async def test_a_batch_at_exactly_the_cardinality_limit_is_accepted() -> None:
    remote = InMemoryFakeRemote(max_batch_size=2)
    starting = await remote.current_snapshot()

    result = await remote.apply(
        batch_id="batch-1",
        expected_snapshot=starting,
        envelopes=(_envelope(1), _envelope(2)),
    )

    assert result.status is ApplyStatus.ACCEPTED
    assert len(remote.committed_events) == 2
