"""What the adapter shows, and when the file learns about it.

The existing metadata contract is 'change the tree, then push'. These tests hold
the adapter to both halves of it: the change is visible to the caller straight
away, and the file is only told at push - through a command the durable store
accepted, never by writing a node some other way.
"""

from pathlib import Path

import pytest

from tgfs.core.commands import ROOT_NODE_ID
from tgfs.core.repository.impl.metadata.sqlite_metadata import SqliteMetadataRepository
from tgfs.errors import FileOrDirectoryAlreadyExists, FileOrDirectoryDoesNotExist

from ..sqlite_command_store import durable
from .view import view


class TestAnEmptyStore:
    async def test_a_new_file_shows_an_empty_root(
        self, repository: SqliteMetadataRepository
    ):
        root = repository.root()

        assert root.name == "root"
        assert root.parent is None
        assert view(root) == []

    async def test_the_root_is_the_node_id_every_store_agrees_on(
        self, repository: SqliteMetadataRepository
    ):
        assert repository.root().node_id == ROOT_NODE_ID


class TestCreating:
    async def test_a_directory_is_visible_before_it_is_pushed(
        self, repository: SqliteMetadataRepository
    ):
        repository.root().create_dir("documents")

        assert view(repository.root()) == [("/documents", "D", 0)]

    async def test_the_file_is_not_told_until_push(
        self, repository: SqliteMetadataRepository, db_path: Path
    ):
        repository.root().create_dir("documents")

        assert durable.children_of(db_path, ROOT_NODE_ID) == []
        assert durable.outbox_rows(db_path) == []

    async def test_push_writes_the_directory_through_a_command(
        self, repository: SqliteMetadataRepository, db_path: Path
    ):
        created = repository.root().create_dir("documents")
        await repository.push()

        row = durable.require_node_row(db_path, created.node_id)
        assert row["name"] == "documents"
        assert row["parent_id"] == ROOT_NODE_ID
        assert row["kind"] == "D"
        # The node exists because a command was accepted, not because the
        # adapter wrote a row of its own.
        assert len(durable.operation_rows(db_path)) == 1

    async def test_a_file_ref_carries_the_message_it_points_at(
        self, repository: SqliteMetadataRepository, db_path: Path
    ):
        file_ref = repository.root().create_file_ref("notes.txt", 4242)
        await repository.push()

        row = durable.require_node_row(db_path, file_ref.node_id)
        assert row["kind"] == "FR"
        assert row["message_id"] == 4242

    async def test_a_nested_tree_hangs_from_the_right_parents(
        self, repository: SqliteMetadataRepository, db_path: Path
    ):
        documents = repository.root().create_dir("documents")
        invoices = documents.create_dir("invoices")
        invoices.create_file_ref("2026.pdf", 77)
        await repository.push()

        assert view(repository.root()) == [
            ("/documents", "D", 0),
            ("/documents/invoices", "D", 0),
            ("/documents/invoices/2026.pdf", "FR", 77),
        ]
        # The three of them, plus the root every store is seeded with.
        assert durable.node_count(db_path) == 4

    async def test_a_second_directory_of_the_same_name_is_refused(
        self, repository: SqliteMetadataRepository
    ):
        repository.root().create_dir("documents")

        with pytest.raises(FileOrDirectoryAlreadyExists):
            repository.root().create_dir("documents")

    async def test_a_second_file_of_the_same_name_is_refused(
        self, repository: SqliteMetadataRepository
    ):
        repository.root().create_file_ref("notes.txt", 1)

        with pytest.raises(FileOrDirectoryAlreadyExists):
            repository.root().create_file_ref("notes.txt", 2)

    async def test_the_directory_api_still_finds_what_it_created(
        self, repository: SqliteMetadataRepository
    ):
        documents = repository.root().create_dir("documents")
        await repository.push()

        assert repository.root().find_dir("documents") is documents
        with pytest.raises(FileOrDirectoryDoesNotExist):
            repository.root().find_dir("elsewhere")

    async def test_pushing_nothing_writes_nothing(
        self, repository: SqliteMetadataRepository, db_path: Path
    ):
        await repository.push()

        assert durable.operation_rows(db_path) == []


class TestRestart:
    async def test_the_namespace_comes_back_as_it_was_left(
        self, open_repository, db_path: Path
    ):
        repository = await open_repository()
        documents = repository.root().create_dir("documents")
        documents.create_dir("invoices")
        documents.create_file_ref("notes.txt", 909)
        repository.root().create_file_ref("readme.md", 11)
        await repository.push()
        before = view(repository.root())
        await repository.close()

        reopened = await open_repository()

        assert view(reopened.root()) == before
        assert before != []

    async def test_what_was_never_pushed_is_not_there_after_a_restart(
        self, open_repository
    ):
        repository = await open_repository()
        repository.root().create_dir("kept")
        await repository.push()
        repository.root().create_dir("never-pushed")
        await repository.close()

        reopened = await open_repository()

        assert view(reopened.root()) == [("/kept", "D", 0)]
