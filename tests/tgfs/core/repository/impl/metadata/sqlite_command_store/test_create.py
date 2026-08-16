"""Creating nodes, and refusing to.

A creation is the smallest complete unit of this store's job: check the parent,
write the node, record the operation, queue the outbox row, all in one
transaction. The refusals matter as much as the happy path - a rejected command
has to leave a file that looks exactly as it did before it arrived.
"""

import pytest

from tgfs.core.commands import (
    ROOT_NODE_ID,
    CreateDir,
    CreateFileRef,
    NodeKind,
    NodeSnapshot,
    new_node_id,
    new_operation_id,
)
from tgfs.errors import (
    FileOrDirectoryAlreadyExists,
    FileOrDirectoryDoesNotExist,
    NotADirectory,
)

from . import durable


class TestCreatingADirectory:
    async def test_reports_the_directory_it_created(self, store) -> None:
        node_id = new_node_id()

        applied = await store.accept(
            CreateDir(
                operation_id=new_operation_id(),
                parent_id=ROOT_NODE_ID,
                name="docs",
                node_id=node_id,
            )
        )

        assert applied.revision == 1
        assert applied.created == (
            NodeSnapshot(
                node_id=node_id,
                parent_id=ROOT_NODE_ID,
                name="docs",
                kind=NodeKind.DIRECTORY,
            ),
        )
        assert applied.updated == ()
        assert applied.deleted == ()

    async def test_writes_the_directory_to_the_file(self, store, db_path) -> None:
        node_id = new_node_id()

        await store.accept(
            CreateDir(
                operation_id=new_operation_id(),
                parent_id=ROOT_NODE_ID,
                name="docs",
                node_id=node_id,
            )
        )

        row = durable.node_row(db_path, node_id)
        assert row is not None
        assert row["parent_id"] == ROOT_NODE_ID
        assert row["name"] == "docs"
        assert row["kind"] == "D"
        assert row["message_id"] is None

    async def test_the_directory_is_still_there_after_a_restart(
        self, store, open_store
    ) -> None:
        node_id = new_node_id()
        await store.accept(
            CreateDir(
                operation_id=new_operation_id(),
                parent_id=ROOT_NODE_ID,
                name="docs",
                node_id=node_id,
            )
        )
        await store.close()

        projection = await (await open_store()).load()

        assert projection.revision == 1
        assert projection.node(node_id).name == "docs"

    async def test_each_accepted_command_moves_the_revision_on_by_one(
        self, store
    ) -> None:
        revisions = []
        for name in ("a", "b", "c"):
            applied = await store.accept(
                CreateDir(
                    operation_id=new_operation_id(),
                    parent_id=ROOT_NODE_ID,
                    name=name,
                    node_id=new_node_id(),
                )
            )
            revisions.append(applied.revision)

        assert revisions == [1, 2, 3]

    async def test_creates_below_another_directory(self, store, db_path) -> None:
        parent_id = new_node_id()
        child_id = new_node_id()
        await store.accept(
            CreateDir(
                operation_id=new_operation_id(),
                parent_id=ROOT_NODE_ID,
                name="docs",
                node_id=parent_id,
            )
        )

        await store.accept(
            CreateDir(
                operation_id=new_operation_id(),
                parent_id=parent_id,
                name="notes",
                node_id=child_id,
            )
        )

        assert durable.require_node_row(db_path, child_id)["parent_id"] == parent_id


class TestRefusingToCreateADirectory:
    async def test_refuses_a_name_the_parent_already_uses(self, store) -> None:
        # The same parent and the same name, twice, under different node ids.
        parent_id, name = ROOT_NODE_ID, "docs"
        await store.accept(
            CreateDir(
                operation_id=new_operation_id(),
                parent_id=parent_id,
                name=name,
                node_id=new_node_id(),
            )
        )

        with pytest.raises(FileOrDirectoryAlreadyExists):
            await store.accept(
                CreateDir(
                    operation_id=new_operation_id(),
                    parent_id=parent_id,
                    name=name,
                    node_id=new_node_id(),
                )
            )

    async def test_a_refused_duplicate_changes_nothing_at_all(
        self, store, db_path
    ) -> None:
        await store.accept(
            CreateDir(
                operation_id=new_operation_id(),
                parent_id=ROOT_NODE_ID,
                name="docs",
                node_id=new_node_id(),
            )
        )
        before = durable.snapshot(db_path)

        with pytest.raises(FileOrDirectoryAlreadyExists):
            await store.accept(
                CreateDir(
                    operation_id=new_operation_id(),
                    parent_id=ROOT_NODE_ID,
                    name="docs",
                    node_id=new_node_id(),
                )
            )

        # Not just the node: no operation row and no outbox row either, or a
        # publisher would announce a mutation that was never accepted.
        assert durable.snapshot(db_path) == before
        assert len(durable.outbox_rows(db_path)) == 1

    async def test_refuses_a_parent_that_is_not_there(self, store, db_path) -> None:
        before = durable.snapshot(db_path)

        with pytest.raises(FileOrDirectoryDoesNotExist):
            await store.accept(
                CreateDir(
                    operation_id=new_operation_id(),
                    parent_id=new_node_id(),
                    name="docs",
                    node_id=new_node_id(),
                )
            )

        assert durable.snapshot(db_path) == before
        assert durable.outbox_rows(db_path) == []

    async def test_refuses_to_create_a_node_that_already_exists(self, store) -> None:
        node_id = new_node_id()
        await store.accept(
            CreateDir(
                operation_id=new_operation_id(),
                parent_id=ROOT_NODE_ID,
                name="docs",
                node_id=node_id,
            )
        )

        with pytest.raises(FileOrDirectoryAlreadyExists):
            await store.accept(
                CreateDir(
                    operation_id=new_operation_id(),
                    parent_id=ROOT_NODE_ID,
                    name="other",
                    node_id=node_id,
                )
            )


class TestCreatingAFileRef:
    async def test_reports_the_file_ref_it_created(self, store) -> None:
        node_id = new_node_id()

        applied = await store.accept(
            CreateFileRef(
                operation_id=new_operation_id(),
                parent_id=ROOT_NODE_ID,
                name="report.pdf",
                message_id=4242,
                node_id=node_id,
            )
        )

        assert applied.created == (
            NodeSnapshot(
                node_id=node_id,
                parent_id=ROOT_NODE_ID,
                name="report.pdf",
                kind=NodeKind.FILE_REF,
                message_id=4242,
            ),
        )

    async def test_keeps_the_file_descriptor_message(self, store, db_path) -> None:
        node_id = new_node_id()

        await store.accept(
            CreateFileRef(
                operation_id=new_operation_id(),
                parent_id=ROOT_NODE_ID,
                name="report.pdf",
                message_id=4242,
                node_id=node_id,
            )
        )

        row = durable.require_node_row(db_path, node_id)
        assert row["kind"] == "FR"
        assert row["message_id"] == 4242

    async def test_the_file_ref_is_still_there_after_a_restart(
        self, store, open_store
    ) -> None:
        node_id = new_node_id()
        await store.accept(
            CreateFileRef(
                operation_id=new_operation_id(),
                parent_id=ROOT_NODE_ID,
                name="report.pdf",
                message_id=4242,
                node_id=node_id,
            )
        )
        await store.close()

        loaded = (await (await open_store()).load()).node(node_id)

        assert loaded.kind is NodeKind.FILE_REF
        assert loaded.message_id == 4242

    async def test_refuses_to_hang_a_node_below_a_file_ref(
        self, store, db_path
    ) -> None:
        file_id = new_node_id()
        await store.accept(
            CreateFileRef(
                operation_id=new_operation_id(),
                parent_id=ROOT_NODE_ID,
                name="report.pdf",
                message_id=4242,
                node_id=file_id,
            )
        )
        before = durable.snapshot(db_path)

        with pytest.raises(NotADirectory):
            await store.accept(
                CreateDir(
                    operation_id=new_operation_id(),
                    parent_id=file_id,
                    name="nested",
                    node_id=new_node_id(),
                )
            )

        assert durable.snapshot(db_path) == before
