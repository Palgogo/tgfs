from pathlib import Path
from typing import AsyncIterator, Callable

import pytest
import pytest_asyncio

from tgfs.core.repository.impl.metadata.sqlite_metadata import SqliteMetadataRepository


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "metadata.sqlite3"


@pytest_asyncio.fixture
async def open_repository(db_path: Path) -> AsyncIterator[Callable[..., object]]:
    """Opens repositories against one file and closes them all at the end.

    A restart is described here by opening the same file again, so the fixture
    hands out a factory rather than a single repository.
    """
    repositories: list[SqliteMetadataRepository] = []

    async def _open() -> SqliteMetadataRepository:
        repository = SqliteMetadataRepository(db_path)
        await repository.init()
        repositories.append(repository)
        return repository

    yield _open

    for repository in repositories:
        await repository.close()


@pytest_asyncio.fixture
async def repository(open_repository) -> SqliteMetadataRepository:
    return await open_repository()
