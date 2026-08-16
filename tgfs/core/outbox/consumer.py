"""Deterministic, idempotent replay of one bounded batch.

`OutboxConsumer.consume_batch` reads a bounded batch in durable sequence
order and publishes each record once, in order, stopping at the first
failure so a later event can never overtake one still unresolved. It never
sleeps, spawns a thread or task, or recurses to retry - a caller decides
whether and when to call it again, using the returned cursor and retry hint.
"""

from dataclasses import dataclass
from typing import Optional

from .transport import TerminalTransportError, TransientTransportError, Transport
from .types import Cursor, OutboxRecord, OutboxSource


@dataclass(frozen=True)
class RetryableFailure:
    event_id: str
    sequence: Cursor
    reason: str


@dataclass(frozen=True)
class TerminalFailure:
    event_id: str
    sequence: Cursor
    reason: str


@dataclass(frozen=True)
class RetryHint:
    """Data a caller can use to decide how to retry - not a scheduled retry."""

    resume_after: Cursor
    event_id: str
    reason: str


@dataclass(frozen=True)
class BatchOutcome:
    attempted: tuple[str, ...]
    acknowledged: tuple[str, ...]
    retryable_failures: tuple[RetryableFailure, ...]
    terminal_failures: tuple[TerminalFailure, ...]
    next_cursor: Cursor
    retry_hint: Optional[RetryHint]


class OutboxConsumer:
    def __init__(self, *, source: OutboxSource, transport: Transport):
        self._source = source
        self._transport = transport

    async def consume_batch(self, *, after: Cursor, limit: int) -> BatchOutcome:
        if limit <= 0:
            raise ValueError(f"limit must be a positive, finite bound, got {limit!r}")

        batch = await self._source.read_batch(after, limit)

        attempted: list[str] = []
        acknowledged: list[str] = []
        cursor = after

        for record in batch.records:
            if record.acknowledged:
                # Durable state already says this was delivered: replaying it
                # must not create a second externally observable publish
                # attempt, so the transport is never called for it.
                acknowledged.append(record.event_id)
                cursor = record.sequence
                continue

            attempted.append(record.event_id)
            try:
                await self._transport.publish(record.event_id, record.payload)
            except TransientTransportError as e:
                return BatchOutcome(
                    attempted=tuple(attempted),
                    acknowledged=tuple(acknowledged),
                    retryable_failures=(
                        RetryableFailure(record.event_id, record.sequence, str(e)),
                    ),
                    terminal_failures=(),
                    next_cursor=cursor,
                    retry_hint=RetryHint(
                        resume_after=cursor,
                        event_id=record.event_id,
                        reason=str(e),
                    ),
                )
            except TerminalTransportError as e:
                return _terminal(attempted, acknowledged, record, cursor, str(e))
            except Exception as e:
                # Unclassified failures have no bounded-retry policy behind
                # them here, so the safe default is to stop and report -
                # never to loop on an unknown failure forever.
                return _terminal(
                    attempted, acknowledged, record, cursor, f"unclassified: {e}"
                )
            await self._source.acknowledge(record.sequence)
            acknowledged.append(record.event_id)
            cursor = record.sequence

        return BatchOutcome(
            attempted=tuple(attempted),
            acknowledged=tuple(acknowledged),
            retryable_failures=(),
            terminal_failures=(),
            next_cursor=cursor,
            retry_hint=None,
        )


def _terminal(
    attempted: list[str],
    acknowledged: list[str],
    record: OutboxRecord,
    cursor: Cursor,
    reason: str,
) -> BatchOutcome:
    return BatchOutcome(
        attempted=tuple(attempted),
        acknowledged=tuple(acknowledged),
        retryable_failures=(),
        terminal_failures=(TerminalFailure(record.event_id, record.sequence, reason),),
        next_cursor=cursor,
        retry_hint=None,
    )
