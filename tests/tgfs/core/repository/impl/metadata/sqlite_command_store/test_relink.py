"""Pointing a file ref at a different file descriptor message.

A relink is the one mutation that changes a node without changing where it sits,
and it is what an upload of a new version of a file comes down to. What matters
is that the node keeps its identity - same id, same place - and that the new
message is what a reader sees after a restart, not the old one.
"""

import pytest

from tgfs.core.commands import (
    ROOT_NODE_ID,
    NodeKind,
    NodeSnapshot,
    RelinkFileRef,
    new_node_id,
    new_operation_id,
)
from tgfs.errors import FileOrDirectoryDoesNotExist, IsADirectory, NothingToApply

from . import durable


class TestRelinking:
    async def test_reports_the_file_ref_as_updated(self, store, make_file) -> None:
        node_id = await make_file("report.pdf", message_id=1000)

        applied = await store.accept(
            RelinkFileRef(
                operation_id=new_operation_id(), node_id=node_id, message_id=2000
            )
        )

        assert applied.updated == (
            NodeSnapshot(
                node_id=node_id,
                parent_id=ROOT_NODE_ID,
                name="report.pdf",
                kind=NodeKind.FILE_REF,
                message_id=2000,
            ),
        )
        assert applied.created == ()
        assert applied.deleted == ()

    async def test_writes_the_new_message_over_the_old_one(
        self, store, make_file, db_path
    ) -> None:
        node_id = await make_file("report.pdf", message_id=1000)

        await store.accept(
            RelinkFileRef(
                operation_id=new_operation_id(), node_id=node_id, message_id=2000
            )
        )

        assert durable.require_node_row(db_path, node_id)["message_id"] == 2000

    async def test_a_restart_sees_the_new_message(
        self, store, make_file, open_store
    ) -> None:
        node_id = await make_file("report.pdf", message_id=1000)
        await store.accept(
            RelinkFileRef(
                operation_id=new_operation_id(), node_id=node_id, message_id=2000
            )
        )
        await store.close()

        projection = await (await open_store()).load()

        assert projection.node(node_id).message_id == 2000
        assert projection.revision == 2

    async def test_keeps_the_node_where_it_was(self, store, make_dir, make_file) -> None:
        parent_id = await make_dir("docs")
        node_id = await make_file("report.pdf", parent_id=parent_id, message_id=1000)

        applied = await store.accept(
            RelinkFileRef(
                operation_id=new_operation_id(), node_id=node_id, message_id=2000
            )
        )

        assert applied.updated[0].parent_id == parent_id
        assert applied.updated[0].name == "report.pdf"


class TestRefusingToRelink:
    async def test_refuses_a_node_that_is_not_there(self, store, db_path) -> None:
        before = durable.snapshot(db_path)

        with pytest.raises(FileOrDirectoryDoesNotExist):
            await store.accept(
                RelinkFileRef(
                    operation_id=new_operation_id(),
                    node_id=new_node_id(),
                    message_id=2000,
                )
            )

        assert durable.snapshot(db_path) == before

    async def test_refuses_a_relink_to_the_message_it_already_points_at(
        self, store, make_file, db_path
    ) -> None:
        # The same answer as moving a node to where it already is, and for the
        # same reason: nothing became true, so there is no revision to hand back
        # and no outbox row a publisher should be made to carry onward.
        node_id = await make_file("report.pdf", message_id=1000)
        before = durable.snapshot(db_path)

        with pytest.raises(NothingToApply):
            await store.accept(
                RelinkFileRef(
                    operation_id=new_operation_id(), node_id=node_id, message_id=1000
                )
            )

        assert durable.snapshot(db_path) == before
        assert len(durable.outbox_rows(db_path)) == 1

    async def test_refuses_a_directory(self, store, make_dir, db_path) -> None:
        node_id = await make_dir("docs")
        before = durable.snapshot(db_path)

        with pytest.raises(IsADirectory):
            await store.accept(
                RelinkFileRef(
                    operation_id=new_operation_id(), node_id=node_id, message_id=2000
                )
            )

        assert durable.snapshot(db_path) == before
