"""The metadata repository backed by one local SQLite file.

It is an adapter in the strict sense: on one side the `IMetaDataRepository`
contract the existing APIs already use - change the tree, then push - and on the
other the P1b durable store, which only accepts typed commands and is the sole
authority on whether a change happened.

The two are joined by the push. A mutating call on the tree records the command
it means and shows the caller the result immediately; the push hands those
commands to the store one at a time, and the visible tree is then rebuilt from
what the store accepted. So the tree is never the record of anything - it is a
view of the file, and a change the store refused cannot outlive the push that
was refused.

Deliberately local: it imports no Telegram and no GitHub, exports nothing to
either, and reads no configuration of its own. It is handed a path.
"""

from pathlib import Path
from typing import Union

from tgfs.core.commands import CommandRepository, MutationCommand, Readiness
from tgfs.core.model import TGFSMetadata
from tgfs.core.repository.impl.metadata.sqlite_command_store import SqliteCommandStore
from tgfs.core.repository.interface import IMetaDataRepository
from tgfs.errors import MetadataNotInitialized

from .namespace import SqliteDirectory, empty_root, rebuild


class SqliteMetadataRepository(IMetaDataRepository):
    def __init__(self, path: Union[str, Path]):
        super().__init__()

        self._store = SqliteCommandStore(path)
        self._commands = CommandRepository(self._store)
        self._pending: list[MutationCommand] = []
        self._root: SqliteDirectory | None = None

    def enqueue(self, command: MutationCommand) -> None:
        """Take note of what a change to the tree means, to be pushed later.

        The operation id was minted when the command was built, so a command
        that reaches the store twice is answered twice with the same revision
        instead of being applied again.
        """
        self._pending.append(command)

    async def get(self) -> TGFSMetadata:
        await self._store.open()
        await self._commands.init()

        self._pending.clear()
        self._root = empty_root(self)
        rebuild(self._root, self._commands.projection())

        return TGFSMetadata(dir=self._root)

    async def push(self) -> None:
        root = self._require_root()

        # Taken before the first apply: whatever happens below, these commands
        # have had their one chance, and the rebuild is what decides what the
        # caller can still see of them.
        pending, self._pending = self._pending, []

        try:
            for command in pending:
                await self._commands.apply(command)
        finally:
            # Rebuilt whether or not every command was accepted. A refusal
            # leaves the store exactly as it was, and the tree has to say so:
            # what the caller was refused stops being visible, and what was
            # accepted before the refusal stays. A projection the store has
            # already outrun is the one thing not worth rebuilding from, so a
            # stale repository is left alone and its error carries.
            if self._commands.readiness is Readiness.READY:
                rebuild(root, self._commands.projection())

    def root(self) -> SqliteDirectory:
        return self._require_root()

    async def close(self) -> None:
        await self._store.close()

    @property
    def readiness(self) -> Readiness:
        return self._commands.readiness

    def _require_root(self) -> SqliteDirectory:
        if self._root is None:
            raise MetadataNotInitialized
        return self._root
