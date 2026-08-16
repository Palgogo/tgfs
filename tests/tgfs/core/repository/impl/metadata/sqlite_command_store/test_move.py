"""Moving a node, and taking its subtree with it.

A move is deliberately not a copy followed by a delete: the node keeps its id,
and so does everything below it, because the ids are what open file handles,
outbox records and any other reader already hold. One row changes - the moved
node's parent and name - and the subtree comes along by still pointing at it.
"""

import pytest

from tgfs.core.commands import (
    ROOT_NODE_ID,
    MoveNode,
    new_node_id,
    new_operation_id,
)
from tgfs.errors import (
    FileOrDirectoryAlreadyExists,
    FileOrDirectoryDoesNotExist,
    InvalidCommandPayload,
    NotADirectory,
    NothingToApply,
)

from . import durable


class TestMovingANode:
    async def test_reports_the_node_at_its_new_place(self, store, make_dir) -> None:
        docs = await make_dir("docs")
        archive = await make_dir("archive")

        applied = await store.accept(
            MoveNode(
                operation_id=new_operation_id(),
                node_id=docs,
                new_parent_id=archive,
                new_name="docs-2024",
            )
        )

        assert len(applied.updated) == 1
        moved = applied.updated[0]
        assert moved.node_id == docs
        assert moved.parent_id == archive
        assert moved.name == "docs-2024"
        assert applied.created == ()
        assert applied.deleted == ()

    async def test_writes_the_new_place_to_the_file(self, store, make_dir) -> None:
        docs = await make_dir("docs")
        archive = await make_dir("archive")

        await store.accept(
            MoveNode(
                operation_id=new_operation_id(),
                node_id=docs,
                new_parent_id=archive,
                new_name="docs-2024",
            )
        )

        row = durable.require_node_row(store.path, docs)
        assert row["parent_id"] == archive
        assert row["name"] == "docs-2024"

    async def test_renames_a_node_without_moving_it(self, store, make_dir) -> None:
        docs = await make_dir("docs")

        applied = await store.accept(
            MoveNode(
                operation_id=new_operation_id(),
                node_id=docs,
                new_parent_id=ROOT_NODE_ID,
                new_name="documents",
            )
        )

        assert applied.updated[0].parent_id == ROOT_NODE_ID
        assert applied.updated[0].name == "documents"

    async def test_every_node_below_keeps_its_id_and_its_parent(
        self, store, make_dir, make_file, db_path
    ) -> None:
        docs = await make_dir("docs")
        year = await make_dir("2024", parent_id=docs)
        report = await make_file("report.pdf", parent_id=year, message_id=1000)
        archive = await make_dir("archive")
        below = {
            node_id: dict(durable.require_node_row(db_path, node_id))
            for node_id in (year, report)
        }

        await store.accept(
            MoveNode(
                operation_id=new_operation_id(),
                node_id=docs,
                new_parent_id=archive,
                new_name="docs",
            )
        )

        # Not one row below the moved node was rewritten: they hang from an id
        # that did not change.
        for node_id, before in below.items():
            assert dict(durable.require_node_row(db_path, node_id)) == before

    async def test_only_the_moved_node_changes(
        self, store, make_dir, make_file, db_path
    ) -> None:
        docs = await make_dir("docs")
        await make_dir("2024", parent_id=docs)
        archive = await make_dir("archive")
        before = {row["node_id"]: dict(row) for row in durable.node_rows(db_path)}

        await store.accept(
            MoveNode(
                operation_id=new_operation_id(),
                node_id=docs,
                new_parent_id=archive,
                new_name="docs",
            )
        )

        after = {row["node_id"]: dict(row) for row in durable.node_rows(db_path)}
        assert set(after) == set(before)
        assert [k for k in after if after[k] != before[k]] == [docs]

    async def test_the_move_survives_a_restart(
        self, store, make_dir, make_file, open_store
    ) -> None:
        docs = await make_dir("docs")
        year = await make_dir("2024", parent_id=docs)
        archive = await make_dir("archive")
        await store.accept(
            MoveNode(
                operation_id=new_operation_id(),
                node_id=docs,
                new_parent_id=archive,
                new_name="docs",
            )
        )
        await store.close()

        projection = await (await open_store()).load()

        assert projection.node(docs).parent_id == archive
        assert projection.node(year).parent_id == docs

    async def test_frees_the_name_it_used_to_have(self, store, make_dir) -> None:
        docs = await make_dir("docs")
        archive = await make_dir("archive")
        await store.accept(
            MoveNode(
                operation_id=new_operation_id(),
                node_id=docs,
                new_parent_id=archive,
                new_name="docs",
            )
        )

        assert await make_dir("docs") != docs


class TestRefusingToMove:
    async def test_refuses_a_node_that_is_not_there(self, store, db_path) -> None:
        before = durable.snapshot(db_path)

        with pytest.raises(FileOrDirectoryDoesNotExist):
            await store.accept(
                MoveNode(
                    operation_id=new_operation_id(),
                    node_id=new_node_id(),
                    new_parent_id=ROOT_NODE_ID,
                    new_name="docs",
                )
            )

        assert durable.snapshot(db_path) == before

    async def test_refuses_a_new_parent_that_is_not_there(
        self, store, make_dir, db_path
    ) -> None:
        docs = await make_dir("docs")
        before = durable.snapshot(db_path)

        with pytest.raises(FileOrDirectoryDoesNotExist):
            await store.accept(
                MoveNode(
                    operation_id=new_operation_id(),
                    node_id=docs,
                    new_parent_id=new_node_id(),
                    new_name="docs",
                )
            )

        assert durable.snapshot(db_path) == before

    async def test_refuses_to_move_below_a_file_ref(
        self, store, make_dir, make_file, db_path
    ) -> None:
        docs = await make_dir("docs")
        report = await make_file("report.pdf")
        before = durable.snapshot(db_path)

        with pytest.raises(NotADirectory):
            await store.accept(
                MoveNode(
                    operation_id=new_operation_id(),
                    node_id=docs,
                    new_parent_id=report,
                    new_name="docs",
                )
            )

        assert durable.snapshot(db_path) == before

    async def test_refuses_a_name_the_new_parent_already_uses(
        self, store, make_dir, db_path
    ) -> None:
        docs = await make_dir("docs")
        archive = await make_dir("archive")
        await make_dir("docs", parent_id=archive)
        before = durable.snapshot(db_path)

        with pytest.raises(FileOrDirectoryAlreadyExists):
            await store.accept(
                MoveNode(
                    operation_id=new_operation_id(),
                    node_id=docs,
                    new_parent_id=archive,
                    new_name="docs",
                )
            )

        assert durable.snapshot(db_path) == before

    async def test_refuses_to_move_a_node_below_itself(
        self, store, make_dir, db_path
    ) -> None:
        # It would cut the subtree loose from the root: still in the file,
        # reachable from nothing.
        docs = await make_dir("docs")
        year = await make_dir("2024", parent_id=docs)
        before = durable.snapshot(db_path)

        with pytest.raises(InvalidCommandPayload):
            await store.accept(
                MoveNode(
                    operation_id=new_operation_id(),
                    node_id=docs,
                    new_parent_id=year,
                    new_name="docs",
                )
            )

        assert durable.snapshot(db_path) == before

    async def test_refuses_to_move_a_node_into_itself(
        self, store, make_dir, db_path
    ) -> None:
        docs = await make_dir("docs")
        before = durable.snapshot(db_path)

        with pytest.raises(InvalidCommandPayload):
            await store.accept(
                MoveNode(
                    operation_id=new_operation_id(),
                    node_id=docs,
                    new_parent_id=docs,
                    new_name="docs",
                )
            )

        assert durable.snapshot(db_path) == before

    async def test_moving_a_node_to_where_it_already_is_applies_nothing(
        self, store, make_dir, db_path
    ) -> None:
        # Same answer as clearing an empty root: there is no revision to hand
        # back for a command that leaves the tree exactly as it found it.
        docs = await make_dir("docs")
        before = durable.snapshot(db_path)

        with pytest.raises(NothingToApply):
            await store.accept(
                MoveNode(
                    operation_id=new_operation_id(),
                    node_id=docs,
                    new_parent_id=ROOT_NODE_ID,
                    new_name="docs",
                )
            )

        assert durable.snapshot(db_path) == before
