"""Consuming P1b's durable outbox end to end, through a real SQLite file.

This is the proof that the consumer's narrow reader abstraction really can be
backed by P1b's `SqliteCommandStore` rather than only by an in-memory fake:
commands are accepted through the store, consumed through the fake transport,
and after the store is closed and reopened the acknowledged events do not
publish again.
"""

from pathlib import Path

from tgfs.core.commands import ROOT_NODE_ID, CreateDir, new_node_id, new_operation_id
from tgfs.core.outbox.consumer import OutboxConsumer
from tgfs.core.outbox.sqlite_source import SqliteOutboxSource
from tgfs.core.outbox.types import Cursor
from tgfs.core.repository.impl.metadata.sqlite_command_store import SqliteCommandStore

from .fakes import FakeTransport


async def test_acknowledged_p1b_events_do_not_republish_after_a_reopen(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "metadata.sqlite3"
    store = await SqliteCommandStore(db_path).open()
    await store.accept(
        CreateDir(
            operation_id=new_operation_id(),
            parent_id=ROOT_NODE_ID,
            name="docs",
            node_id=new_node_id(),
        )
    )
    await store.accept(
        CreateDir(
            operation_id=new_operation_id(),
            parent_id=ROOT_NODE_ID,
            name="pics",
            node_id=new_node_id(),
        )
    )

    transport = FakeTransport()
    consumer = OutboxConsumer(source=SqliteOutboxSource(store), transport=transport)
    first = await consumer.consume_batch(after=Cursor(0), limit=10)
    await store.close()

    reopened = await SqliteCommandStore(db_path).open()
    second_transport = FakeTransport()
    second_consumer = OutboxConsumer(
        source=SqliteOutboxSource(reopened), transport=second_transport
    )
    second = await second_consumer.consume_batch(after=Cursor(0), limit=10)
    await reopened.close()

    assert len(first.acknowledged) == 2
    assert second_transport.calls == []
    assert second.acknowledged == first.acknowledged
