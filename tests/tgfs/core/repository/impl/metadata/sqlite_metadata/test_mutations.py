"""Removing, copying and relinking, as commands rather than as edits.

These are the changes that are easiest to make look right in memory and never
write down: a list the caller cannot see any more, a message id assigned over.
Every test here asks the file what happened, and most of them ask again after a
restart, because that is the only reading of 'it happened' that survives.
"""

from pathlib import Path

import pytest

from tgfs.core.commands import ROOT_NODE_ID
from tgfs.core.model import TGFSDirectory
from tgfs.core.repository.impl.metadata.sqlite_metadata import SqliteMetadataRepository
from tgfs.errors import InvalidCommandPayload

from ..sqlite_command_store import durable
from .view import sqlite_dir, view


async def _tree(repository: SqliteMetadataRepository) -> None:
    """A small tree, pushed, for the tests that need something to work on."""
    documents = repository.root().create_dir("documents")
    invoices = documents.create_dir("invoices")
    invoices.create_file_ref("2026.pdf", 77)
    documents.create_file_ref("notes.txt", 11)
    await repository.push()


class TestRemoving:
    async def test_a_deleted_file_ref_leaves_the_file(
        self, repository: SqliteMetadataRepository, db_path: Path
    ):
        await _tree(repository)
        documents = sqlite_dir(repository.root(), "documents")
        notes = documents.find_file("notes.txt")

        notes.delete()
        await repository.push()

        assert view(repository.root()) == [
            ("/documents", "D", 0),
            ("/documents/invoices", "D", 0),
            ("/documents/invoices/2026.pdf", "FR", 77),
        ]
        assert durable.child_named(db_path, documents.node_id, "notes.txt") is None

    async def test_a_deleted_directory_takes_its_subtree_with_it(
        self, repository: SqliteMetadataRepository, db_path: Path
    ):
        await _tree(repository)
        documents = repository.root().find_dir("documents")

        documents.delete()
        await repository.push()

        assert view(repository.root()) == []
        # The root, and nothing else.
        assert durable.node_count(db_path) == 1

    async def test_a_deletion_survives_a_restart(self, open_repository):
        repository = await open_repository()
        await _tree(repository)
        repository.root().find_dir("documents").delete()
        await repository.push()
        await repository.close()

        reopened = await open_repository()

        assert view(reopened.root()) == []

    async def test_clearing_the_root_empties_it(
        self, repository: SqliteMetadataRepository, db_path: Path
    ):
        await _tree(repository)

        repository.root().delete()
        await repository.push()

        assert view(repository.root()) == []
        assert durable.children_of(db_path, ROOT_NODE_ID) == []

    async def test_clearing_an_already_empty_root_asks_the_store_for_nothing(
        self, repository: SqliteMetadataRepository, db_path: Path
    ):
        repository.root().delete()
        await repository.push()

        assert durable.operation_rows(db_path) == []


class TestCopying:
    async def test_a_copy_brings_the_whole_subtree(
        self, repository: SqliteMetadataRepository
    ):
        await _tree(repository)
        source = repository.root().find_dir("documents")

        repository.root().create_dir("archive", source)
        await repository.push()

        assert view(repository.root()) == [
            ("/archive", "D", 0),
            ("/archive/invoices", "D", 0),
            ("/archive/invoices/2026.pdf", "FR", 77),
            ("/archive/notes.txt", "FR", 11),
            ("/documents", "D", 0),
            ("/documents/invoices", "D", 0),
            ("/documents/invoices/2026.pdf", "FR", 77),
            ("/documents/notes.txt", "FR", 11),
        ]

    async def test_the_copy_is_made_of_nodes_of_its_own(
        self, repository: SqliteMetadataRepository
    ):
        await _tree(repository)
        source = sqlite_dir(repository.root(), "documents")

        copy = repository.root().create_dir("archive", source)
        await repository.push()

        assert (
            sqlite_dir(copy, "invoices").node_id
            != sqlite_dir(source, "invoices").node_id
        )

    async def test_a_copy_survives_a_restart(self, open_repository):
        repository = await open_repository()
        await _tree(repository)
        repository.root().create_dir("archive", repository.root().find_dir("documents"))
        await repository.push()
        before = view(repository.root())
        await repository.close()

        reopened = await open_repository()

        assert view(reopened.root()) == before

    async def test_a_directory_from_another_tree_cannot_be_copied_in(
        self, repository: SqliteMetadataRepository
    ):
        with pytest.raises(InvalidCommandPayload):
            repository.root().create_dir("archive", TGFSDirectory.root_dir())


class TestRelinking:
    async def test_a_ref_points_at_the_message_it_was_given(
        self, repository: SqliteMetadataRepository, db_path: Path
    ):
        notes = repository.root().create_file_ref("notes.txt", 11)
        await repository.push()

        notes.message_id = 222
        await repository.push()

        assert notes.message_id == 222
        assert durable.require_node_row(db_path, notes.node_id)["message_id"] == 222

    async def test_the_new_message_survives_a_restart(self, open_repository):
        repository = await open_repository()
        notes = repository.root().create_file_ref("notes.txt", 11)
        await repository.push()
        notes.message_id = 222
        await repository.push()
        await repository.close()

        reopened = await open_repository()

        assert view(reopened.root()) == [("/notes.txt", "FR", 222)]

    async def test_the_message_it_already_points_at_asks_the_store_for_nothing(
        self, repository: SqliteMetadataRepository, db_path: Path
    ):
        notes = repository.root().create_file_ref("notes.txt", 11)
        await repository.push()

        notes.message_id = 11
        await repository.push()

        assert len(durable.operation_rows(db_path)) == 1
