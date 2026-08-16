"""Which metadata backend a client is built with, and nothing else.

Selection is the only thing under test here, so Telegram is a stand-in that can
resolve a channel id and would fail loudly if anything asked it for more. The
local store is reached by naming it in the configuration and by no other route:
a configuration that does not say 'sqlite' gets exactly the backend it got
before this adapter existed.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from tgfs.config import MetadataConfig, MetadataType, SqliteMetadataConfig
from tgfs.core.client import Client
from tgfs.core.model import TGFSDirectory, TGFSMetadata
from tgfs.core.repository.impl.metadata.pinned_message import TGMsgMetadataRepository
from tgfs.core.repository.impl.metadata.sqlite_metadata import SqliteDirectory
from tgfs.core.repository.interface import IMetaDataRepository

CHANNEL_ID = "114514"


@pytest.fixture
def tdlib_api() -> MagicMock:
    """A Telegram API that can resolve a channel and is asked for nothing else."""
    tdlib = MagicMock()
    tdlib.next_bot.resolve_channel_id = AsyncMock(return_value=1919810)
    tdlib.account = None
    return tdlib


class _FakeRemoteRepository(IMetaDataRepository):
    """Stands in for a backend whose constructor would otherwise go online."""

    instances: list["_FakeRemoteRepository"] = []

    def __init__(self, config):
        super().__init__()
        self.config = config
        _FakeRemoteRepository.instances.append(self)

    async def push(self) -> None:
        pass

    async def get(self) -> TGFSMetadata:
        return TGFSMetadata(dir=TGFSDirectory.root_dir())


def _sqlite_config(path: Path) -> MetadataConfig:
    return MetadataConfig(
        name="local",
        type=MetadataType.SQLITE,
        github_repo=None,
        sqlite=SqliteMetadataConfig(path=str(path)),
    )


class TestSelectingTheLocalStore:
    async def test_a_sqlite_configuration_serves_the_local_namespace(
        self, tdlib_api: MagicMock, tmp_path: Path
    ):
        db_path = tmp_path / "metadata.sqlite3"

        client = await Client.create(CHANNEL_ID, _sqlite_config(db_path), tdlib_api)

        assert isinstance(client.dir_api.root, SqliteDirectory)
        assert client.name == "local"
        assert db_path.exists()

    async def test_the_local_store_needs_no_telegram_metadata_call(
        self, tdlib_api: MagicMock, tmp_path: Path
    ):
        await Client.create(
            CHANNEL_ID, _sqlite_config(tmp_path / "metadata.sqlite3"), tdlib_api
        )

        tdlib_api.next_bot.get_pinned_messages.assert_not_called()
        tdlib_api.next_bot.send_text.assert_not_called()

    async def test_a_sqlite_configuration_without_a_path_is_refused(
        self, tdlib_api: MagicMock
    ):
        # Reachable only by building the configuration by hand: reading one
        # from a file refuses this well before a client is ever created.
        without_path = MetadataConfig(
            name="local", type=MetadataType.SQLITE, github_repo=None, sqlite=None
        )

        with pytest.raises(ValueError, match="metadata -> sqlite"):
            await Client.create(CHANNEL_ID, without_path, tdlib_api)


class TestTheOtherBackendsAreUntouched:
    async def test_pinned_message_is_still_what_a_default_configuration_gets(
        self, tdlib_api: MagicMock, monkeypatch
    ):
        monkeypatch.setattr(
            TGMsgMetadataRepository,
            "get",
            AsyncMock(return_value=TGFSMetadata(dir=TGFSDirectory.root_dir())),
        )
        config = MetadataConfig(
            name="notes", type=MetadataType.PINNED_MESSAGE, github_repo=None
        )

        client = await Client.create(CHANNEL_ID, config, tdlib_api)

        assert not isinstance(client.dir_api.root, SqliteDirectory)

    async def test_github_repo_is_still_built_from_its_own_configuration(
        self, tdlib_api: MagicMock, monkeypatch
    ):
        monkeypatch.setattr(
            "tgfs.core.repository.impl.metadata.github_repo.GithubRepoMetadataRepository",
            _FakeRemoteRepository,
        )
        _FakeRemoteRepository.instances.clear()
        github_repo = MagicMock(repo="owner/repo")
        config = MetadataConfig(
            name="notes", type=MetadataType.GITHUB_REPO, github_repo=github_repo
        )

        client = await Client.create(CHANNEL_ID, config, tdlib_api)

        assert _FakeRemoteRepository.instances[-1].config is github_repo
        assert not isinstance(client.dir_api.root, SqliteDirectory)
