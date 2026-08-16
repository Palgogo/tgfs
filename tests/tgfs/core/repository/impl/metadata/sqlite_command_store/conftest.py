from pathlib import Path
from typing import AsyncIterator, Callable, Optional

import pytest
import pytest_asyncio

from tgfs.core.commands import (
    ROOT_NODE_ID,
    CreateDir,
    CreateFileRef,
    NodeId,
    new_node_id,
    new_operation_id,
)
from tgfs.core.repository.impl.metadata.sqlite_command_store import SqliteCommandStore


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "metadata.sqlite3"


@pytest_asyncio.fixture
async def open_store(db_path: Path) -> AsyncIterator[Callable[..., object]]:
    """Opens stores against one path and closes them all at the end.

    Reopening the same file is how a restart is described in these tests, so
    the fixture hands out a factory rather than a single store.
    """
    stores: list[SqliteCommandStore] = []

    async def _open(before_commit: Optional[Callable[[], None]] = None):
        store = SqliteCommandStore(db_path, before_commit=before_commit)
        await store.open()
        stores.append(store)
        return store

    yield _open

    for store in stores:
        await store.close()


@pytest_asyncio.fixture
async def store(open_store) -> SqliteCommandStore:
    return await open_store()


@pytest.fixture
def make_dir(store) -> Callable:
    """Builds a directory through the store, and hands back its id.

    Tests about deleting, copying or moving need a tree to work on; going
    through accept() to build it keeps them from depending on a shape the store
    would never have produced itself.
    """

    async def _make_dir(name: str, parent_id: NodeId = ROOT_NODE_ID) -> NodeId:
        node_id = new_node_id()
        await store.accept(
            CreateDir(
                operation_id=new_operation_id(),
                parent_id=parent_id,
                name=name,
                node_id=node_id,
            )
        )
        return node_id

    return _make_dir


@pytest.fixture
def make_file(store) -> Callable:
    async def _make_file(
        name: str, parent_id: NodeId = ROOT_NODE_ID, message_id: int = 1000
    ) -> NodeId:
        node_id = new_node_id()
        await store.accept(
            CreateFileRef(
                operation_id=new_operation_id(),
                parent_id=parent_id,
                name=name,
                message_id=message_id,
                node_id=node_id,
            )
        )
        return node_id

    return _make_file
