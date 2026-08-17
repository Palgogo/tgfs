"""Cross-batch export-history continuity and history_begins_at floor.

The fake remote must enforce global sequence continuity across batches and
honour an immutable export-history floor. Overlap under a new batch id is
REJECTED, not idempotent or conflict.
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


async def _apply_accepted(
    remote: InMemoryFakeRemote,
    *,
    batch_id: str,
    envelopes: tuple[ExportEnvelope, ...],
) -> None:
    expected = await remote.current_snapshot()
    result = await remote.apply(
        batch_id=batch_id,
        expected_snapshot=expected,
        envelopes=envelopes,
    )
    assert result.status is ApplyStatus.ACCEPTED


# --- history_begins_at (remediation §1.7) ---


def test_non_positive_history_begins_at_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        InMemoryFakeRemote(history_begins_at=0)
    with pytest.raises(ValueError):
        InMemoryFakeRemote(history_begins_at=-1)


async def test_first_batch_is_accepted_at_a_non_default_floor() -> None:
    remote = InMemoryFakeRemote(history_begins_at=500)
    starting = await remote.current_snapshot()

    result = await remote.apply(
        batch_id="batch-1",
        expected_snapshot=starting,
        envelopes=(_envelope(500), _envelope(501)),
    )

    assert result.status is ApplyStatus.ACCEPTED
    assert tuple(e.sequence for e in remote.committed_events) == (500, 501)
    assert result.snapshot != starting


async def test_first_batch_below_floor_is_rejected_without_mutation() -> None:
    remote = InMemoryFakeRemote(history_begins_at=500)
    starting = await remote.current_snapshot()
    events_before = remote.committed_events

    result = await remote.apply(
        batch_id="batch-1",
        expected_snapshot=starting,
        envelopes=(_envelope(1), _envelope(2)),
    )

    assert result.status is ApplyStatus.REJECTED
    assert remote.committed_events == events_before
    assert await remote.current_snapshot() == starting


async def test_first_batch_above_floor_is_rejected_without_mutation() -> None:
    remote = InMemoryFakeRemote(history_begins_at=500)
    starting = await remote.current_snapshot()
    events_before = remote.committed_events

    result = await remote.apply(
        batch_id="batch-1",
        expected_snapshot=starting,
        envelopes=(_envelope(502), _envelope(503)),
    )

    assert result.status is ApplyStatus.REJECTED
    assert remote.committed_events == events_before
    assert await remote.current_snapshot() == starting


async def test_gap_after_accepted_history_is_rejected_without_mutation() -> None:
    remote = InMemoryFakeRemote(history_begins_at=500)
    await _apply_accepted(
        remote, batch_id="batch-1", envelopes=(_envelope(500), _envelope(501))
    )
    snapshot_before = await remote.current_snapshot()
    events_before = remote.committed_events

    result = await remote.apply(
        batch_id="batch-2",
        expected_snapshot=snapshot_before,
        envelopes=(_envelope(503), _envelope(504)),
    )

    assert result.status is ApplyStatus.REJECTED
    assert remote.committed_events == events_before
    assert await remote.current_snapshot() == snapshot_before


async def test_overlap_under_a_new_batch_id_is_rejected_not_idempotent_or_conflict() -> (
    None
):
    remote = InMemoryFakeRemote(history_begins_at=500)
    await _apply_accepted(
        remote, batch_id="batch-1", envelopes=(_envelope(500), _envelope(501))
    )
    snapshot_before = await remote.current_snapshot()
    events_before = remote.committed_events

    result = await remote.apply(
        batch_id="batch-2",
        expected_snapshot=snapshot_before,
        envelopes=(_envelope(501), _envelope(502)),
    )

    assert result.status is ApplyStatus.REJECTED
    assert result.status is not ApplyStatus.IDEMPOTENT
    assert result.status is not ApplyStatus.CONFLICT
    assert remote.committed_events == events_before
    assert await remote.current_snapshot() == snapshot_before
    assert sum(1 for e in remote.committed_events if e.sequence == 501) == 1


async def test_valid_continuation_after_partial_history_is_accepted() -> None:
    remote = InMemoryFakeRemote(history_begins_at=500)
    await _apply_accepted(
        remote, batch_id="batch-1", envelopes=(_envelope(500), _envelope(501))
    )
    mid_snapshot = await remote.current_snapshot()

    result = await remote.apply(
        batch_id="batch-2",
        expected_snapshot=mid_snapshot,
        envelopes=(_envelope(502), _envelope(503)),
    )

    assert result.status is ApplyStatus.ACCEPTED
    assert tuple(e.sequence for e in remote.committed_events) == (500, 501, 502, 503)
    assert result.snapshot != mid_snapshot


async def test_rebuilt_fake_preserves_floor_and_continuity_rules() -> None:
    seed = InMemoryFakeRemote(history_begins_at=500)
    await _apply_accepted(
        seed, batch_id="batch-1", envelopes=(_envelope(500), _envelope(501))
    )
    rebuilt = InMemoryFakeRemote(
        history_begins_at=500,
        initial_events=seed.committed_events,
        initial_snapshot=await seed.current_snapshot(),
    )

    snapshot_before = await rebuilt.current_snapshot()
    events_before = rebuilt.committed_events

    gap = await rebuilt.apply(
        batch_id="batch-gap",
        expected_snapshot=snapshot_before,
        envelopes=(_envelope(503), _envelope(504)),
    )
    overlap = await rebuilt.apply(
        batch_id="batch-overlap",
        expected_snapshot=snapshot_before,
        envelopes=(_envelope(501), _envelope(502)),
    )

    assert gap.status is ApplyStatus.REJECTED
    assert overlap.status is ApplyStatus.REJECTED
    assert rebuilt.committed_events == events_before
    assert await rebuilt.current_snapshot() == snapshot_before


# --- P2d §1.6 continuity coverage (default history_begins_at=1) ---


async def test_accepted_first_batch_on_a_fresh_remote() -> None:
    remote = InMemoryFakeRemote()
    starting = await remote.current_snapshot()

    result = await remote.apply(
        batch_id="batch-1",
        expected_snapshot=starting,
        envelopes=(_envelope(1), _envelope(2)),
    )

    assert result.status is ApplyStatus.ACCEPTED
    assert remote.committed_events == (_envelope(1), _envelope(2))
    assert result.snapshot != starting


async def test_skip_ahead_batch_is_rejected_with_no_mutation() -> None:
    remote = InMemoryFakeRemote()
    await _apply_accepted(remote, batch_id="batch-1", envelopes=(_envelope(1), _envelope(2)))
    snapshot_before = await remote.current_snapshot()
    events_before = remote.committed_events

    result = await remote.apply(
        batch_id="batch-2",
        expected_snapshot=snapshot_before,
        envelopes=(_envelope(4), _envelope(5)),
    )

    assert result.status is ApplyStatus.REJECTED
    assert "export history" in result.reason.lower()
    assert remote.committed_events == events_before
    assert await remote.current_snapshot() == snapshot_before


async def test_overlap_batch_under_new_batch_id_is_rejected_with_no_mutation() -> None:
    remote = InMemoryFakeRemote()
    await _apply_accepted(remote, batch_id="batch-1", envelopes=(_envelope(1), _envelope(2)))
    snapshot_before = await remote.current_snapshot()
    events_before = remote.committed_events

    result = await remote.apply(
        batch_id="batch-2",
        expected_snapshot=snapshot_before,
        envelopes=(_envelope(2), _envelope(3)),
    )

    assert result.status is ApplyStatus.REJECTED
    assert result.status is not ApplyStatus.IDEMPOTENT
    assert result.status is not ApplyStatus.CONFLICT
    assert remote.committed_events == events_before
    assert len(remote.committed_events) == 2
    assert sum(1 for e in remote.committed_events if e.sequence == 2) == 1


async def test_valid_next_contiguous_batch_is_accepted() -> None:
    remote = InMemoryFakeRemote()
    await _apply_accepted(remote, batch_id="batch-1", envelopes=(_envelope(1), _envelope(2)))
    mid_snapshot = await remote.current_snapshot()

    result = await remote.apply(
        batch_id="batch-2",
        expected_snapshot=mid_snapshot,
        envelopes=(_envelope(3), _envelope(4)),
    )

    assert result.status is ApplyStatus.ACCEPTED
    assert tuple(e.sequence for e in remote.committed_events) == (1, 2, 3, 4)
    assert result.snapshot != mid_snapshot


async def test_rebuilt_state_enforces_continuity_without_in_process_history() -> None:
    seed = InMemoryFakeRemote()
    await _apply_accepted(seed, batch_id="batch-1", envelopes=(_envelope(1), _envelope(2)))
    rebuilt = InMemoryFakeRemote(
        initial_events=seed.committed_events,
        initial_snapshot=await seed.current_snapshot(),
    )

    snapshot_before = await rebuilt.current_snapshot()

    skip = await rebuilt.apply(
        batch_id="batch-skip",
        expected_snapshot=snapshot_before,
        envelopes=(_envelope(4), _envelope(5)),
    )
    overlap = await rebuilt.apply(
        batch_id="batch-overlap",
        expected_snapshot=snapshot_before,
        envelopes=(_envelope(2), _envelope(3)),
    )
    valid = await rebuilt.apply(
        batch_id="batch-valid",
        expected_snapshot=snapshot_before,
        envelopes=(_envelope(3), _envelope(4)),
    )

    assert skip.status is ApplyStatus.REJECTED
    assert overlap.status is ApplyStatus.REJECTED
    assert valid.status is ApplyStatus.ACCEPTED
    assert tuple(e.sequence for e in rebuilt.committed_events) == (1, 2, 3, 4)
