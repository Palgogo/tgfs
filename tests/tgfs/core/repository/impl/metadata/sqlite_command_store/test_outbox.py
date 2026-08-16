"""The outbox: one row per accepted mutation, and nothing else.

The outbox is the promise that whatever publishes changes onward will be told
about every accepted mutation exactly once. That promise is kept by writing the
row in the same transaction as the mutation, so these tests are mostly about
counting: one row when a command is accepted, none when it is refused, none
extra when it is retried.

The other half is what the row says. It is read back by a process that has only
the row - not the command object, not this store - so it has to parse back into
the very command and change set that were accepted, and it must not carry
anything that was never part of the command.
"""

import json

from tgfs.core.commands import (
    ROOT_NODE_ID,
    ClearRoot,
    CopySubtree,
    CreateDir,
    CreateFileRef,
    DeleteNode,
    MoveNode,
    RelinkFileRef,
    applied_command_from_dict,
    new_node_id,
    new_operation_id,
)
from tgfs.core.repository.impl.metadata.sqlite_command_store.codec import (
    command_from_dict,
)

from . import durable


class TestOneRowPerAcceptedMutation:
    async def test_an_accepted_command_leaves_exactly_one_row(
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

        assert len(durable.outbox_rows(db_path)) == 1

    async def test_every_accepted_command_adds_one_row(
        self, store, make_dir, make_file, db_path
    ) -> None:
        docs = await make_dir("docs")
        report = await make_file("report.pdf", parent_id=docs, message_id=1000)
        await store.accept(
            RelinkFileRef(
                operation_id=new_operation_id(), node_id=report, message_id=2000
            )
        )
        await store.accept(
            MoveNode(
                operation_id=new_operation_id(),
                node_id=report,
                new_parent_id=ROOT_NODE_ID,
                new_name="report.pdf",
            )
        )
        await store.accept(DeleteNode(operation_id=new_operation_id(), node_id=docs))

        assert len(durable.outbox_rows(db_path)) == 5

    async def test_the_rows_are_in_the_order_the_revisions_were_accepted(
        self, store, make_dir, db_path
    ) -> None:
        for name in ("a", "b", "c"):
            await make_dir(name)

        rows = durable.outbox_rows(db_path)

        assert [row["revision"] for row in rows] == [1, 2, 3]
        assert [row["sequence"] for row in rows] == sorted(
            row["sequence"] for row in rows
        )

    async def test_a_row_names_the_operation_that_produced_it(
        self, store, db_path
    ) -> None:
        operation_id = new_operation_id()

        await store.accept(
            CreateDir(
                operation_id=operation_id,
                parent_id=ROOT_NODE_ID,
                name="docs",
                node_id=new_node_id(),
            )
        )

        assert durable.outbox_rows(db_path)[0]["operation_id"] == operation_id


class TestNoRowWithoutAnAcceptance:
    async def test_a_refused_command_leaves_no_row(self, store, db_path) -> None:
        try:
            await store.accept(
                CreateDir(
                    operation_id=new_operation_id(),
                    parent_id=new_node_id(),
                    name="docs",
                    node_id=new_node_id(),
                )
            )
        except Exception:
            pass

        assert durable.outbox_rows(db_path) == []

    async def test_a_replayed_command_adds_no_second_row(
        self, store, db_path
    ) -> None:
        command = CreateDir(
            operation_id=new_operation_id(),
            parent_id=ROOT_NODE_ID,
            name="docs",
            node_id=new_node_id(),
        )
        await store.accept(command)
        await store.accept(command)
        await store.accept(command)

        assert len(durable.outbox_rows(db_path)) == 1

    async def test_a_replay_does_not_rewrite_the_row_it_already_wrote(
        self, store, db_path
    ) -> None:
        command = CreateDir(
            operation_id=new_operation_id(),
            parent_id=ROOT_NODE_ID,
            name="docs",
            node_id=new_node_id(),
        )
        await store.accept(command)
        before = durable.outbox_rows(db_path)[0]

        await store.accept(command)

        assert dict(durable.outbox_rows(db_path)[0]) == dict(before)


class TestWhatTheRowSays:
    async def test_the_command_parses_back_into_the_command_that_was_sent(
        self, store, db_path
    ) -> None:
        command = CreateFileRef(
            operation_id=new_operation_id(),
            parent_id=ROOT_NODE_ID,
            name="report.pdf",
            message_id=4242,
            node_id=new_node_id(),
        )
        await store.accept(command)

        row = durable.outbox_rows(db_path)[0]

        assert command_from_dict(json.loads(row["command_json"])) == command

    async def test_every_kind_of_command_parses_back(
        self, store, make_dir, make_file, db_path
    ) -> None:
        docs = await make_dir("docs")
        report = await make_file("report.pdf", parent_id=docs, message_id=1000)
        sent = [
            RelinkFileRef(
                operation_id=new_operation_id(), node_id=report, message_id=2000
            ),
            CopySubtree(
                operation_id=new_operation_id(),
                source_id=docs,
                parent_id=ROOT_NODE_ID,
                name="docs-copy",
                node_id=new_node_id(),
            ),
            MoveNode(
                operation_id=new_operation_id(),
                node_id=report,
                new_parent_id=ROOT_NODE_ID,
                new_name="report.pdf",
            ),
            DeleteNode(operation_id=new_operation_id(), node_id=docs),
            ClearRoot(operation_id=new_operation_id()),
        ]
        for command in sent:
            await store.accept(command)

        parsed = [
            command_from_dict(json.loads(row["command_json"]))
            for row in durable.outbox_rows(db_path)
        ]

        assert parsed[2:] == sent

    async def test_the_change_set_parses_back_into_what_was_returned(
        self, store, make_dir, make_file, db_path
    ) -> None:
        # A copy, because its change set is the one a reader cannot rebuild for
        # itself: the ids in it were minted inside the transaction.
        docs = await make_dir("docs")
        await make_file("readme.md", parent_id=docs, message_id=1000)
        applied = await store.accept(
            CopySubtree(
                operation_id=new_operation_id(),
                source_id=docs,
                parent_id=ROOT_NODE_ID,
                name="docs-copy",
                node_id=new_node_id(),
            )
        )

        row = durable.outbox_rows(db_path)[-1]

        assert applied_command_from_dict(json.loads(row["applied_json"])) == applied

    async def test_the_row_carries_nothing_but_the_command(
        self, store, db_path
    ) -> None:
        # Whatever publishes this may put it somewhere far less private than
        # the database, so the row is checked to hold only what the command
        # itself declared - no session, no channel, no credential.
        await store.accept(
            CreateFileRef(
                operation_id=new_operation_id(),
                parent_id=ROOT_NODE_ID,
                name="report.pdf",
                message_id=4242,
                node_id=new_node_id(),
            )
        )

        payload = json.loads(durable.outbox_rows(db_path)[0]["command_json"])

        assert set(payload) == {
            "type",
            "operationId",
            "parentId",
            "name",
            "messageId",
            "nodeId",
            "version",
        }

    async def test_no_row_holds_anything_that_looks_like_a_credential(
        self, store, make_dir, make_file, db_path
    ) -> None:
        docs = await make_dir("docs")
        await make_file("report.pdf", parent_id=docs, message_id=1000)

        text = " ".join(
            f"{row['command_json']} {row['applied_json']}"
            for row in durable.outbox_rows(db_path)
        ).lower()

        for secret in ("token", "session", "password", "api_hash", "api_id", "bot"):
            assert secret not in text
