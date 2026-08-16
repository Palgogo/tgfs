"""What survives a failure at the worst possible moment.

Every accepted command writes three things - the namespace, the operation
record, the outbox row - and the whole design rests on them landing together.
The moment that would break it is the instant after all three have been written
and before the commit makes them durable: a store that let anything through
there would publish changes nobody applied, or apply changes nobody is told
about.

The store takes a `before_commit` seam for exactly this. A real backend cannot
be asked to die on the third statement, and a test that waited for a real crash
would not be a test.
"""

import pytest

from tgfs.core.commands import (
    ROOT_NODE_ID,
    CopySubtree,
    CreateDir,
    DeleteNode,
    new_node_id,
    new_operation_id,
)

from . import durable


class Crash(Exception):
    """Whatever kills a process between the last write and the commit."""


def crashing():
    def _crash() -> None:
        raise Crash("the lights went out")

    return _crash


def crashing_after(successes: int):
    """Fails once, on the accept after `successes` of them have gone through."""
    state = dict(seen=0)

    def _crash() -> None:
        state["seen"] += 1
        if state["seen"] > successes:
            raise Crash("the lights went out")

    return _crash


class TestFailingBeforeTheCommit:
    async def test_the_caller_hears_about_it(self, open_store) -> None:
        store = await open_store(before_commit=crashing())

        with pytest.raises(Crash):
            await store.accept(
                CreateDir(
                    operation_id=new_operation_id(),
                    parent_id=ROOT_NODE_ID,
                    name="docs",
                    node_id=new_node_id(),
                )
            )

    async def test_the_node_never_appears(self, open_store, db_path) -> None:
        store = await open_store(before_commit=crashing())
        node_id = new_node_id()

        with pytest.raises(Crash):
            await store.accept(
                CreateDir(
                    operation_id=new_operation_id(),
                    parent_id=ROOT_NODE_ID,
                    name="docs",
                    node_id=node_id,
                )
            )

        assert durable.node_row(db_path, node_id) is None
        assert durable.node_count(db_path) == 1

    async def test_no_operation_and_no_outbox_row_are_left_behind(
        self, open_store, db_path
    ) -> None:
        store = await open_store(before_commit=crashing())
        before = durable.snapshot(db_path)

        with pytest.raises(Crash):
            await store.accept(
                CreateDir(
                    operation_id=new_operation_id(),
                    parent_id=ROOT_NODE_ID,
                    name="docs",
                    node_id=new_node_id(),
                )
            )

        assert durable.snapshot(db_path) == before
        assert durable.operation_rows(db_path) == []
        assert durable.outbox_rows(db_path) == []

    async def test_the_revision_does_not_move(self, open_store) -> None:
        store = await open_store(before_commit=crashing())

        with pytest.raises(Crash):
            await store.accept(
                CreateDir(
                    operation_id=new_operation_id(),
                    parent_id=ROOT_NODE_ID,
                    name="docs",
                    node_id=new_node_id(),
                )
            )

        assert (await store.load()).revision == 0

    async def test_a_multi_node_copy_leaves_nothing_half_written(
        self, store, open_store, make_dir, make_file, db_path
    ) -> None:
        # The copy writes five rows. Four of them surviving would be the worst
        # possible outcome, and is the one this is looking for.
        docs = await make_dir("docs")
        year = await make_dir("2024", parent_id=docs)
        await make_file("report.pdf", parent_id=year, message_id=1000)
        await store.close()
        crashing_store = await open_store(before_commit=crashing())
        before = durable.snapshot(db_path)

        with pytest.raises(Crash):
            await crashing_store.accept(
                CopySubtree(
                    operation_id=new_operation_id(),
                    source_id=docs,
                    parent_id=ROOT_NODE_ID,
                    name="docs-copy",
                    node_id=new_node_id(),
                )
            )

        assert durable.snapshot(db_path) == before

    async def test_a_delete_that_fails_leaves_the_whole_subtree(
        self, store, open_store, make_dir, make_file, db_path
    ) -> None:
        docs = await make_dir("docs")
        year = await make_dir("2024", parent_id=docs)
        await make_file("report.pdf", parent_id=year, message_id=1000)
        await store.close()
        crashing_store = await open_store(before_commit=crashing())
        before = durable.snapshot(db_path)

        with pytest.raises(Crash):
            await crashing_store.accept(
                DeleteNode(operation_id=new_operation_id(), node_id=docs)
            )

        assert durable.snapshot(db_path) == before


class TestAfterTheFailure:
    async def test_what_was_already_accepted_stays_accepted(
        self, open_store, db_path
    ) -> None:
        store = await open_store(before_commit=crashing_after(1))
        kept = new_node_id()
        await store.accept(
            CreateDir(
                operation_id=new_operation_id(),
                parent_id=ROOT_NODE_ID,
                name="kept",
                node_id=kept,
            )
        )

        with pytest.raises(Crash):
            await store.accept(
                CreateDir(
                    operation_id=new_operation_id(),
                    parent_id=ROOT_NODE_ID,
                    name="lost",
                    node_id=new_node_id(),
                )
            )

        assert durable.node_row(db_path, kept) is not None
        assert durable.node_count(db_path) == 2
        assert len(durable.outbox_rows(db_path)) == 1

    async def test_the_store_still_works(self, open_store, db_path) -> None:
        store = await open_store(before_commit=crashing_after(0))
        with pytest.raises(Crash):
            await store.accept(
                CreateDir(
                    operation_id=new_operation_id(),
                    parent_id=ROOT_NODE_ID,
                    name="lost",
                    node_id=new_node_id(),
                )
            )
        await store.close()

        healthy = await open_store()
        applied = await healthy.accept(
            CreateDir(
                operation_id=new_operation_id(),
                parent_id=ROOT_NODE_ID,
                name="docs",
                node_id=new_node_id(),
            )
        )

        # The revision the lost command would have taken is still free.
        assert applied.revision == 1

    async def test_a_restart_sees_nothing_of_the_lost_command(
        self, open_store
    ) -> None:
        store = await open_store(before_commit=crashing())
        with pytest.raises(Crash):
            await store.accept(
                CreateDir(
                    operation_id=new_operation_id(),
                    parent_id=ROOT_NODE_ID,
                    name="lost",
                    node_id=new_node_id(),
                )
            )
        await store.close()

        projection = await (await open_store()).load()

        assert dict(projection.nodes) == {}
        assert projection.revision == 0

    async def test_the_lost_command_can_be_sent_again_under_the_same_id(
        self, open_store
    ) -> None:
        # Nothing was recorded, so this is not a retry of an accepted
        # operation - it is the command finally happening.
        store = await open_store(before_commit=crashing_after(0))
        command = CreateDir(
            operation_id=new_operation_id(),
            parent_id=ROOT_NODE_ID,
            name="docs",
            node_id=new_node_id(),
        )
        with pytest.raises(Crash):
            await store.accept(command)
        await store.close()

        applied = await (await open_store()).accept(command)

        assert applied.revision == 1
        assert applied.created[0].name == "docs"


class TestTheSeamAndReplays:
    async def test_a_replay_does_not_reach_the_commit_at_all(
        self, store, open_store
    ) -> None:
        # A retry of an accepted operation writes nothing, so it must not be
        # able to fail on the way to a commit it never makes.
        command = CreateDir(
            operation_id=new_operation_id(),
            parent_id=ROOT_NODE_ID,
            name="docs",
            node_id=new_node_id(),
        )
        first = await store.accept(command)
        await store.close()

        replaying = await open_store(before_commit=crashing())
        assert await replaying.accept(command) == first
