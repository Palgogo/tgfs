"""Removing nodes, and what has to be gone with them.

Deleting is where a store leaks if it is going to: a child whose parent is gone
is not visible to anybody, so nothing complains, and it stays in the file
forever. Every test here checks the descendants as well as the node that was
named.

The change set is the other half. A delete reports only the node it was asked
about, never its descendants: the reader on the other side knows a subtree goes
with its root, and naming the children as well would have it try to delete nodes
its view has already dropped.
"""

import pytest

from tgfs.core.commands import (
    ROOT_NODE_ID,
    ClearRoot,
    DeleteNode,
    new_node_id,
    new_operation_id,
)
from tgfs.errors import FileOrDirectoryDoesNotExist, NothingToApply

from . import durable


class TestDeletingANode:
    async def test_reports_the_node_it_deleted(self, store, make_dir) -> None:
        node_id = await make_dir("docs")

        applied = await store.accept(
            DeleteNode(operation_id=new_operation_id(), node_id=node_id)
        )

        assert applied.deleted == (node_id,)
        assert applied.created == ()
        assert applied.updated == ()

    async def test_takes_the_node_out_of_the_file(
        self, store, make_dir, db_path
    ) -> None:
        node_id = await make_dir("docs")

        await store.accept(DeleteNode(operation_id=new_operation_id(), node_id=node_id))

        assert durable.node_row(db_path, node_id) is None

    async def test_takes_every_descendant_with_it(
        self, store, make_dir, make_file, db_path
    ) -> None:
        docs = await make_dir("docs")
        year = await make_dir("2024", parent_id=docs)
        month = await make_dir("06", parent_id=year)
        report = await make_file("report.pdf", parent_id=month)

        await store.accept(DeleteNode(operation_id=new_operation_id(), node_id=docs))

        for node_id in (docs, year, month, report):
            assert durable.node_row(db_path, node_id) is None
        # Only the root is left, so nothing was orphaned rather than deleted.
        assert durable.node_count(db_path) == 1

    async def test_names_only_the_root_of_what_it_deleted(
        self, store, make_dir
    ) -> None:
        docs = await make_dir("docs")
        await make_dir("2024", parent_id=docs)

        applied = await store.accept(
            DeleteNode(operation_id=new_operation_id(), node_id=docs)
        )

        assert applied.deleted == (docs,)

    async def test_the_subtree_is_gone_after_a_restart(
        self, store, make_dir, open_store
    ) -> None:
        docs = await make_dir("docs")
        year = await make_dir("2024", parent_id=docs)
        await store.accept(DeleteNode(operation_id=new_operation_id(), node_id=docs))
        await store.close()

        projection = await (await open_store()).load()

        assert not projection.contains(docs)
        assert not projection.contains(year)

    async def test_frees_the_name_it_was_using(self, store, make_dir) -> None:
        node_id = await make_dir("docs")
        await store.accept(DeleteNode(operation_id=new_operation_id(), node_id=node_id))

        # No exception: the name is free again.
        assert await make_dir("docs") != node_id

    async def test_refuses_a_node_that_is_not_there(self, store, db_path) -> None:
        before = durable.snapshot(db_path)

        with pytest.raises(FileOrDirectoryDoesNotExist):
            await store.accept(
                DeleteNode(operation_id=new_operation_id(), node_id=new_node_id())
            )

        assert durable.snapshot(db_path) == before


class TestClearingTheRoot:
    async def test_reports_every_top_level_node_it_removed(
        self, store, make_dir, make_file
    ) -> None:
        docs = await make_dir("docs")
        readme = await make_file("readme.md")
        await make_dir("2024", parent_id=docs)

        applied = await store.accept(ClearRoot(operation_id=new_operation_id()))

        assert set(applied.deleted) == {docs, readme}
        assert applied.created == ()
        assert applied.updated == ()

    async def test_leaves_nothing_but_the_root(
        self, store, make_dir, make_file, db_path
    ) -> None:
        docs = await make_dir("docs")
        year = await make_dir("2024", parent_id=docs)
        await make_file("report.pdf", parent_id=year)

        await store.accept(ClearRoot(operation_id=new_operation_id()))

        assert durable.node_count(db_path) == 1
        assert durable.node_row(db_path, ROOT_NODE_ID) is not None

    async def test_the_root_is_still_empty_after_a_restart(
        self, store, make_dir, open_store
    ) -> None:
        await make_dir("docs")
        await store.accept(ClearRoot(operation_id=new_operation_id()))
        await store.close()

        projection = await (await open_store()).load()

        assert dict(projection.nodes) == {}
        assert projection.contains(ROOT_NODE_ID)

    async def test_refuses_to_clear_a_root_that_is_already_empty(
        self, store, db_path
    ) -> None:
        before = durable.snapshot(db_path)

        # A revision has to say what became true, and nothing did. Accepting it
        # would publish an empty change set to every reader.
        with pytest.raises(NothingToApply):
            await store.accept(ClearRoot(operation_id=new_operation_id()))

        assert durable.snapshot(db_path) == before
        assert durable.outbox_rows(db_path) == []
