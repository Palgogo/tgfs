"""Retryable remote failures and orchestrator mapping.

`RemoteUnavailableError` carries only closed vocabulary tokens. The
orchestrator maps it to `RETRYABLE_ERROR` without acknowledging events.
"""

import pytest

from tgfs.core.export.envelope import ExportEnvelope, SourceEvent, build_envelope
from tgfs.core.export.fake_remote import InMemoryFakeRemote
from tgfs.core.export.orchestrator import ExportOrchestrator, ExportStatus
from tgfs.core.export.protocol import (
    ApplyStatus,
    RemoteApplyError,
    RemoteUnavailableError,
)
from tgfs.core.outbox.types import Cursor

from tests.tgfs.core.outbox.fakes import FakeOutboxSource, record

from .fakes import (
    BrokenRemote,
    FailThenDelegateRemote,
    PropagatingRemote,
    UnavailableRemote,
)

EVENT_TYPE = "metadata.command"


def _envelope(sequence: int, event_id: str = "") -> ExportEnvelope:
    return build_envelope(
        SourceEvent(
            event_id=event_id or f"op-{sequence}",
            sequence=sequence,
            event_type=EVENT_TYPE,
            payload="{}",
        )
    )


@pytest.mark.parametrize(
    "reason",
    (
        "remote_unavailable",
        "remote_rate_limited",
        "remote_timeout_unresolved",
    ),
)
def test_remote_unavailable_error_accepts_closed_vocabulary_tokens(
    reason: str,
) -> None:
    error = RemoteUnavailableError(reason)
    assert error.reason == reason


@pytest.mark.parametrize(
    "invalid_reason",
    (
        "503 Service Unavailable",
        "remote_unknown",
        "",
    ),
)
def test_remote_unavailable_error_rejects_non_vocabulary_reasons(
    invalid_reason: str,
) -> None:
    with pytest.raises(ValueError):
        RemoteUnavailableError(invalid_reason)


async def test_retryable_before_commit_returns_retryable_error_without_ack() -> (
    None
):
    source = FakeOutboxSource([record(1, "op-1")])
    remote = UnavailableRemote(reason="remote_unavailable")
    orchestrator = ExportOrchestrator(
        source=source, remote=remote, event_type=EVENT_TYPE
    )

    outcome = await orchestrator.export_batch(
        after=Cursor(0),
        limit=10,
        batch_id="batch-1",
        expected_snapshot=await remote.current_snapshot(),
    )

    assert outcome.status is ExportStatus.RETRYABLE_ERROR
    assert outcome.reason == "remote_unavailable"
    assert outcome.acknowledged_event_ids == ()
    assert outcome.next_cursor == Cursor(0)
    assert outcome.next_snapshot is None
    assert source.acknowledge_calls == []


async def test_timeout_after_commit_is_retryable_then_idempotent_on_retry() -> (
    None
):
    inner = InMemoryFakeRemote()
    genesis = await inner.current_snapshot()
    remote = FailThenDelegateRemote(
        inner,
        fail_after_commit=True,
        reason="remote_timeout_unresolved",
    )
    records = [record(1, "op-1")]
    source = FakeOutboxSource(list(records))
    orchestrator = ExportOrchestrator(
        source=source, remote=remote, event_type=EVENT_TYPE
    )

    first = await orchestrator.export_batch(
        after=Cursor(0),
        limit=10,
        batch_id="batch-1",
        expected_snapshot=genesis,
    )

    retry_source = FakeOutboxSource(list(records))
    retry_orchestrator = ExportOrchestrator(
        source=retry_source, remote=remote, event_type=EVENT_TYPE
    )
    second = await retry_orchestrator.export_batch(
        after=Cursor(0),
        limit=10,
        batch_id="batch-1",
        expected_snapshot=genesis,
    )

    assert first.status is ExportStatus.RETRYABLE_ERROR
    assert first.reason == "remote_timeout_unresolved"
    assert first.acknowledged_event_ids == ()
    assert len(inner.committed_events) == 1
    assert second.status is ExportStatus.IDEMPOTENT
    assert second.acknowledged_event_ids == ("op-1",)
    assert retry_source.acknowledge_calls == [Cursor(1)]


async def test_retry_after_rate_limit_then_accepts_on_second_attempt() -> None:
    inner = InMemoryFakeRemote()
    genesis = await inner.current_snapshot()
    remote = FailThenDelegateRemote(
        inner,
        fail_count=1,
        reason="remote_rate_limited",
    )
    source = FakeOutboxSource([record(1, "op-1")])
    orchestrator = ExportOrchestrator(
        source=source, remote=remote, event_type=EVENT_TYPE
    )

    first = await orchestrator.export_batch(
        after=Cursor(0),
        limit=10,
        batch_id="batch-1",
        expected_snapshot=genesis,
    )
    second = await orchestrator.export_batch(
        after=Cursor(0),
        limit=10,
        batch_id="batch-1",
        expected_snapshot=genesis,
    )

    assert first.status is ExportStatus.RETRYABLE_ERROR
    assert first.reason == "remote_rate_limited"
    assert first.acknowledged_event_ids == ()
    assert second.status is ExportStatus.ACCEPTED
    assert second.acknowledged_event_ids == ("op-1",)
    assert len(inner.committed_events) == 1


async def test_terminal_remote_apply_error_remains_terminal_error() -> None:
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
    assert outcome.status is not ExportStatus.RETRYABLE_ERROR
    assert outcome.acknowledged_event_ids == ()
    assert source.acknowledge_calls == []


async def test_stale_snapshot_conflict_is_not_retryable() -> None:
    inner = InMemoryFakeRemote()
    genesis = await inner.current_snapshot()
    await inner.apply(
        batch_id="other",
        expected_snapshot=genesis,
        envelopes=(_envelope(1, "other-op"),),
    )
    source = FakeOutboxSource([record(1, "op-1")])
    orchestrator = ExportOrchestrator(
        source=source, remote=inner, event_type=EVENT_TYPE
    )

    outcome = await orchestrator.export_batch(
        after=Cursor(0),
        limit=10,
        batch_id="batch-1",
        expected_snapshot=genesis,
    )

    assert outcome.status is ExportStatus.CONFLICT
    assert outcome.status is not ExportStatus.RETRYABLE_ERROR
    assert source.acknowledge_calls == []


async def test_unknown_exception_propagates_and_is_not_reclassified() -> None:
    source = FakeOutboxSource([record(1, "op-1")])
    remote = PropagatingRemote(RuntimeError("unexpected"))
    orchestrator = ExportOrchestrator(
        source=source, remote=remote, event_type=EVENT_TYPE
    )

    with pytest.raises(RuntimeError, match="unexpected"):
        await orchestrator.export_batch(
            after=Cursor(0),
            limit=10,
            batch_id="batch-1",
            expected_snapshot=await remote.current_snapshot(),
        )

    assert source.acknowledge_calls == []
