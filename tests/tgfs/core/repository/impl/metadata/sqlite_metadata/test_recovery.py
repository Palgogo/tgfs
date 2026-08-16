"""What the file records, and what is left when it refuses.

Two repositories are opened on the same file here on purpose. It is the only
honest way to make the store say no to something the tree in memory thought was
fine, and what happens next is the whole question: the caller has to be told,
and the change it was refused must not go on being visible as though it had
happened.
"""

from pathlib import Path

import pytest

from tgfs.core.commands import Readiness
from tgfs.core.repository.impl.metadata.sqlite_metadata import SqliteMetadataRepository
from tgfs.errors import (
    FileOrDirectoryAlreadyExists,
    FileOrDirectoryDoesNotExist,
    ProjectionStaleError,
)

from ..sqlite_command_store import durable
from .view import view


class TestTheOutbox:
    async def test_every_accepted_change_leaves_exactly_one_row(
        self, repository: SqliteMetadataRepository, db_path: Path
    ):
        documents = repository.root().create_dir("documents")
        documents.create_file_ref("notes.txt", 11)
        await repository.push()
        documents.find_file("notes.txt").message_id = 22
        documents.delete()
        await repository.push()

        outbox = durable.outbox_rows(db_path)
        assert len(outbox) == 4
        assert [row["revision"] for row in outbox] == [1, 2, 3, 4]

    async def test_the_outbox_and_the_operations_agree(
        self, repository: SqliteMetadataRepository, db_path: Path
    ):
        repository.root().create_dir("documents")
        repository.root().create_file_ref("readme.md", 5)
        await repository.push()

        outbox = durable.outbox_rows(db_path)
        operations = durable.operation_rows(db_path)
        assert [row["operation_id"] for row in outbox] == [
            row["operation_id"] for row in operations
        ]


async def _refused_create(open_repository) -> SqliteMetadataRepository:
    """A repository whose next push will be refused, and the tree it still shows.

    Both are opened before anything is durable, so the second one has never seen
    the directory the first is about to create: its own check cannot catch the
    clash, and only the store can.
    """
    first = await open_repository()
    second = await open_repository()

    first.root().create_dir("documents")
    await first.push()

    second.root().create_dir("documents")
    return second


class TestARefusedPush:
    async def test_the_refusal_reaches_the_caller(self, open_repository):
        second = await _refused_create(open_repository)

        with pytest.raises(FileOrDirectoryAlreadyExists):
            await second.push()

    async def test_what_the_store_refused_stops_being_visible(self, open_repository):
        second = await _refused_create(open_repository)

        with pytest.raises(FileOrDirectoryAlreadyExists):
            await second.push()

        assert view(second.root()) == []

    async def test_a_second_writer_the_store_outran_stops_answering(
        self, open_repository
    ):
        """The limit this adapter is built inside: one writer per file.

        A repository that another one has written past cannot tell from memory
        what it is missing, so the P1b contract declares it stale rather than
        let it serve a view with someone else's writes silently absent.
        """
        first = await open_repository()
        second = await open_repository()

        first.root().create_dir("documents")
        await first.push()

        second.root().create_dir("fresh")
        with pytest.raises(ProjectionStaleError):
            await second.push()

        assert second.readiness is Readiness.STALE


class TestAPushRefusedHalfWay:
    """A push whose commands are accepted up to the one that is not.

    Ordered on purpose: the directory is deleted and a file is then created
    under it, so the store accepts the first command and refuses the second for
    a reason only it can see - by then the parent is gone.
    """

    async def _delete_then_fill(
        self, repository: SqliteMetadataRepository
    ) -> SqliteMetadataRepository:
        documents = repository.root().create_dir("documents")
        await repository.push()

        documents.delete()
        documents.create_file_ref("orphan.txt", 5)
        return repository

    async def test_the_refusal_reaches_the_caller(
        self, repository: SqliteMetadataRepository
    ):
        await self._delete_then_fill(repository)

        with pytest.raises(FileOrDirectoryDoesNotExist):
            await repository.push()

    async def test_what_was_accepted_before_the_refusal_stands(
        self, repository: SqliteMetadataRepository, db_path: Path
    ):
        await self._delete_then_fill(repository)

        with pytest.raises(FileOrDirectoryDoesNotExist):
            await repository.push()

        # The deletion happened; the file under it never did.
        assert view(repository.root()) == []
        assert durable.node_count(db_path) == 1

    async def test_a_refused_command_is_not_carried_into_the_next_push(
        self, repository: SqliteMetadataRepository, db_path: Path
    ):
        await self._delete_then_fill(repository)
        with pytest.raises(FileOrDirectoryDoesNotExist):
            await repository.push()

        repository.root().create_dir("elsewhere")
        await repository.push()

        assert view(repository.root()) == [("/elsewhere", "D", 0)]
        # Creating the directory, deleting it, and creating the next one. The
        # refused command is not among them and is never tried again.
        assert len(durable.operation_rows(db_path)) == 3
