"""Re-applying the same batch identity must never duplicate a remote event.

A caller retries for two different reasons the fake remote must treat alike:
it saw an explicit failure and is trying again, or it never learned whether
the first attempt committed (an ambiguous timeout-after-commit) and is
retrying blind, quite possibly still believing the pre-apply snapshot is
current. Both retries carry the same idempotency batch identity and the same
envelope content as the original, and both must come back as an idempotent
acknowledgement rather than a second commit. The same batch identity reused
with *different* content is a caller contract violation, not a retry, and is
rejected deterministically instead of silently overwriting history.
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


async def test_reapplying_the_same_accepted_batch_id_and_content_is_idempotent() -> (
    None
):
    remote = InMemoryFakeRemote()
    starting = await remote.current_snapshot()
    envelopes = (_envelope(1), _envelope(2))

    first = await remote.apply(
        batch_id="batch-1", expected_snapshot=starting, envelopes=envelopes
    )
    second = await remote.apply(
        batch_id="batch-1", expected_snapshot=first.snapshot, envelopes=envelopes
    )

    assert second.status is ApplyStatus.IDEMPOTENT
    assert second.snapshot == first.snapshot
    assert len(remote.committed_events) == 2


async def test_a_blind_retry_against_a_now_stale_snapshot_still_gets_an_idempotent_ack() -> (
    None
):
    """Models an ambiguous timeout-after-commit: the caller never learned the
    first attempt succeeded, so it retries with the *pre-apply* snapshot it
    already had - not the new one - yet must still be acknowledged, not
    told the batch conflicts."""
    remote = InMemoryFakeRemote()
    starting = await remote.current_snapshot()
    envelopes = (_envelope(1),)

    first = await remote.apply(
        batch_id="batch-1", expected_snapshot=starting, envelopes=envelopes
    )
    retry = await remote.apply(
        batch_id="batch-1", expected_snapshot=starting, envelopes=envelopes
    )

    assert retry.status is ApplyStatus.IDEMPOTENT
    assert retry.snapshot == first.snapshot
    assert len(remote.committed_events) == 1


async def test_the_same_batch_id_with_different_content_is_rejected_as_a_conflict() -> (
    None
):
    remote = InMemoryFakeRemote()
    starting = await remote.current_snapshot()

    first = await remote.apply(
        batch_id="batch-1", expected_snapshot=starting, envelopes=(_envelope(1),)
    )
    mismatched = await remote.apply(
        batch_id="batch-1",
        expected_snapshot=first.snapshot,
        envelopes=(_envelope(1, event_id="different-op"),),
    )

    assert mismatched.status is ApplyStatus.CONFLICT
    assert mismatched.snapshot == first.snapshot
    assert len(remote.committed_events) == 1
