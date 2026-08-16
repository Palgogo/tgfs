"""Asking twice.

A caller that does not hear back cannot tell a write that failed from an answer
that was lost, so it retries. The operation id is what lets the store tell those
apart: the same id carrying the same command is the same intention arriving
again and is answered with what it was answered the first time, down to the
revision and the ids that were minted. The same id carrying a different command
is not a retry at all, and saying so is better than either applying it or
silently pretending it was already done.
"""

import dataclasses

import pytest

from tgfs.core.commands import (
    ROOT_NODE_ID,
    CopySubtree,
    CreateDir,
    DeleteNode,
    new_node_id,
    new_operation_id,
)
from tgfs.errors import OperationIdConflict

from . import durable


class TestReplayingAnOperation:
    async def test_answers_a_repeat_with_the_original_result(self, store) -> None:
        command = CreateDir(
            operation_id=new_operation_id(),
            parent_id=ROOT_NODE_ID,
            name="docs",
            node_id=new_node_id(),
        )
        first = await store.accept(command)

        second = await store.accept(command)

        assert second == first

    async def test_a_repeat_writes_nothing(self, store, db_path) -> None:
        command = CreateDir(
            operation_id=new_operation_id(),
            parent_id=ROOT_NODE_ID,
            name="docs",
            node_id=new_node_id(),
        )
        await store.accept(command)
        before = durable.snapshot(db_path)

        await store.accept(command)

        assert durable.snapshot(db_path) == before

    async def test_a_repeat_does_not_move_the_revision_on(self, store) -> None:
        command = CreateDir(
            operation_id=new_operation_id(),
            parent_id=ROOT_NODE_ID,
            name="docs",
            node_id=new_node_id(),
        )
        await store.accept(command)

        await store.accept(command)

        assert (await store.load()).revision == 1

    async def test_a_repeat_leaves_exactly_one_outbox_row(self, store, db_path) -> None:
        command = CreateDir(
            operation_id=new_operation_id(),
            parent_id=ROOT_NODE_ID,
            name="docs",
            node_id=new_node_id(),
        )
        await store.accept(command)

        await store.accept(command)

        assert len(durable.outbox_rows(db_path)) == 1

    async def test_a_repeat_after_a_restart_still_gets_the_original_result(
        self, store, open_store
    ) -> None:
        command = CreateDir(
            operation_id=new_operation_id(),
            parent_id=ROOT_NODE_ID,
            name="docs",
            node_id=new_node_id(),
        )
        first = await store.accept(command)
        await store.close()

        second = await (await open_store()).accept(command)

        assert second == first

    async def test_a_repeated_copy_reports_the_ids_it_minted_the_first_time(
        self, store, make_dir, make_file
    ) -> None:
        # The caller cannot reconstruct these: they were invented inside the
        # first transaction, and the retry has to be told the same ones.
        docs = await make_dir("docs")
        await make_dir("2024", parent_id=docs)
        await make_file("readme.md", parent_id=docs, message_id=1000)
        command = CopySubtree(
            operation_id=new_operation_id(),
            source_id=docs,
            parent_id=ROOT_NODE_ID,
            name="docs-copy",
            node_id=new_node_id(),
        )
        first = await store.accept(command)

        second = await store.accept(command)

        assert second.created == first.created

    async def test_a_repeated_delete_reports_what_it_deleted(
        self, store, make_dir
    ) -> None:
        docs = await make_dir("docs")
        command = DeleteNode(operation_id=new_operation_id(), node_id=docs)
        first = await store.accept(command)

        second = await store.accept(command)

        assert second == first


class TestTheSameIdForSomethingElse:
    async def test_refuses_a_different_command_under_a_used_id(self, store) -> None:
        command = CreateDir(
            operation_id=new_operation_id(),
            parent_id=ROOT_NODE_ID,
            name="docs",
            node_id=new_node_id(),
        )
        await store.accept(command)

        with pytest.raises(OperationIdConflict):
            await store.accept(dataclasses.replace(command, name="something-else"))

    async def test_a_conflicting_id_changes_nothing(self, store, db_path) -> None:
        command = CreateDir(
            operation_id=new_operation_id(),
            parent_id=ROOT_NODE_ID,
            name="docs",
            node_id=new_node_id(),
        )
        await store.accept(command)
        before = durable.snapshot(db_path)

        with pytest.raises(OperationIdConflict):
            await store.accept(dataclasses.replace(command, node_id=new_node_id()))

        assert durable.snapshot(db_path) == before

    async def test_a_conflict_is_reported_even_for_a_different_command_type(
        self, store, make_dir
    ) -> None:
        operation_id = new_operation_id()
        docs = await make_dir("docs")
        await store.accept(
            CreateDir(
                operation_id=operation_id,
                parent_id=docs,
                name="2024",
                node_id=new_node_id(),
            )
        )

        with pytest.raises(OperationIdConflict):
            await store.accept(
                DeleteNode(operation_id=operation_id, node_id=docs)
            )

    async def test_the_conflict_names_the_operation(self, store) -> None:
        command = CreateDir(
            operation_id=new_operation_id(),
            parent_id=ROOT_NODE_ID,
            name="docs",
            node_id=new_node_id(),
        )
        await store.accept(command)

        with pytest.raises(OperationIdConflict) as raised:
            await store.accept(dataclasses.replace(command, name="other"))

        assert command.operation_id in str(raised.value)
