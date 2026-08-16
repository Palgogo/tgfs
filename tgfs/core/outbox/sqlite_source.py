"""The `OutboxSource` P1b's durable store is read through.

This is the one place P2a is allowed to know `SqliteCommandStore` exists. It
translates the store's own `read_outbox`/`acknowledge_outbox` into the
consumer's narrow port so the consumer logic itself never imports sqlite3 or
reaches past the store's own read/write API. It is not wired into any
runtime, configuration, or client - only into this package's own P1b
integration test.
"""

from tgfs.core.repository.impl.metadata.sqlite_command_store import SqliteCommandStore

from .types import BatchResult, Cursor, OutboxRecord


class SqliteOutboxSource:
    def __init__(self, store: SqliteCommandStore):
        self._store = store

    async def read_batch(self, after: Cursor, limit: int) -> BatchResult:
        entries = await self._store.read_outbox(after, limit)
        records = tuple(
            OutboxRecord(
                sequence=Cursor(entry.sequence),
                event_id=str(entry.operation_id),
                payload=entry.command_json,
                acknowledged=entry.acknowledged,
            )
            for entry in entries
        )
        next_cursor = records[-1].sequence if records else after
        return BatchResult(records=records, next_cursor=next_cursor)

    async def acknowledge(self, sequence: Cursor) -> None:
        await self._store.acknowledge_outbox(sequence)
