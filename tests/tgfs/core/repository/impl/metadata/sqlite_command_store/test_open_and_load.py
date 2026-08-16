"""What a store is before any command reaches it.

An empty store is not nothing: it is a file with durability turned on, a schema
it can recognise after an upgrade, and a root that every later command hangs
from. Everything else in this package assumes these hold.
"""

import sqlite3
from pathlib import Path

import pytest

from tgfs.core.commands import ROOT_NODE_ID, Projection
from tgfs.core.repository.impl.metadata.sqlite_command_store import (
    SCHEMA_VERSION,
    SqliteCommandStore,
)
from tgfs.errors import DurableStoreError, MetadataNotInitialized

from . import durable


def _write_schema_version(path: Path, version: int) -> None:
    """Stamp a version onto the file, the way another build of this code would."""
    connection = sqlite3.connect(path)
    try:
        connection.execute("UPDATE schema_version SET version = ?", (version,))
        connection.commit()
    finally:
        connection.close()


class TestOpening:
    async def test_creates_the_database_file(self, store, db_path: Path) -> None:
        assert db_path.exists()

    async def test_records_the_schema_version(self, store, db_path: Path) -> None:
        assert durable.schema_version(db_path) == SCHEMA_VERSION

    async def test_keeps_the_write_ahead_log(self, store, db_path: Path) -> None:
        # A file property, so a later reader sees it too.
        assert durable.file_pragma(db_path, "journal_mode") == "wal"

    async def test_waits_for_the_disk_on_every_commit(self, store) -> None:
        # 2 is FULL, and it is per connection, so only this store can be asked.
        # Anything less and a commit is acknowledged before the write is on the
        # disk, which is the one thing this store sells.
        assert store.pragma("synchronous") == 2

    async def test_enforces_foreign_keys(self, store) -> None:
        assert store.pragma("foreign_keys") == 1

    async def test_reopening_keeps_one_schema_version_row(
        self, open_store, db_path: Path
    ) -> None:
        first = await open_store()
        await first.close()
        await open_store()

        assert durable.one(db_path, "SELECT COUNT(*) FROM schema_version") == 1
        assert durable.schema_version(db_path) == SCHEMA_VERSION


class TestTheRoot:
    async def test_persists_the_root_under_the_agreed_id(
        self, store, db_path: Path
    ) -> None:
        root = durable.node_row(db_path, ROOT_NODE_ID)

        assert root is not None
        assert root["kind"] == "D"

    async def test_the_root_survives_a_restart_without_being_duplicated(
        self, store, open_store, db_path: Path
    ) -> None:
        await store.close()
        await open_store()

        assert durable.node_row(db_path, ROOT_NODE_ID) is not None
        assert durable.node_count(db_path) == 1


class TestAFileThisCodeCannotUnderstand:
    """A store written by a later version is refused, not opened hopefully.

    What a future schema means by these tables is not something this code can
    work out, and opening it anyway would have it write rows in a shape it
    invented for itself into a file somebody else's build still reads.
    """

    async def test_refuses_a_schema_from_a_later_version(
        self, store, make_dir, db_path: Path
    ) -> None:
        await make_dir("docs")
        await store.close()
        _write_schema_version(db_path, SCHEMA_VERSION + 1)

        with pytest.raises(DurableStoreError):
            await SqliteCommandStore(db_path).open()

    async def test_the_refusal_names_both_versions(
        self, store, db_path: Path
    ) -> None:
        # The operator has to be told which build to reach for, and a message
        # that only says "cannot open" does not tell them.
        await store.close()
        _write_schema_version(db_path, SCHEMA_VERSION + 1)

        with pytest.raises(DurableStoreError) as raised:
            await SqliteCommandStore(db_path).open()

        assert str(SCHEMA_VERSION + 1) in str(raised.value)
        assert str(SCHEMA_VERSION) in str(raised.value)

    async def test_it_writes_nothing_to_the_file_it_refused(
        self, store, make_dir, db_path: Path
    ) -> None:
        await make_dir("docs")
        await store.close()
        _write_schema_version(db_path, SCHEMA_VERSION + 1)
        before = durable.snapshot(db_path)

        with pytest.raises(DurableStoreError):
            await SqliteCommandStore(db_path).open()

        assert durable.snapshot(db_path) == before
        assert durable.schema_version(db_path) == SCHEMA_VERSION + 1


class TestLoading:
    async def test_an_untouched_store_loads_the_empty_projection(self, store) -> None:
        projection = await store.load()

        assert projection.revision == 0
        assert dict(projection.nodes) == {}
        assert projection.contains(ROOT_NODE_ID)

    async def test_a_store_that_was_never_opened_refuses_to_load(
        self, db_path: Path
    ) -> None:
        with pytest.raises(MetadataNotInitialized):
            await SqliteCommandStore(db_path).load()
