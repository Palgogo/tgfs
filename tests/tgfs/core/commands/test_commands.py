import dataclasses

import pytest

from tgfs.core.commands import (
    COMMAND_VERSION,
    ROOT_NODE_ID,
    ClearRoot,
    CopySubtree,
    CreateDir,
    CreateFileRef,
    DeleteNode,
    MoveNode,
    MutationCommand,
    RelinkFileRef,
    new_node_id,
    new_operation_id,
)
from tgfs.errors import (
    InvalidCommandPayload,
    InvalidName,
    InvalidNodeId,
    InvalidOperationId,
    RootNodeNotRemovable,
)


class TestCreateDir:
    def test_is_a_frozen_versioned_value(self):
        node_id = new_node_id()
        operation_id = new_operation_id()

        command = CreateDir(
            operation_id=operation_id,
            parent_id=ROOT_NODE_ID,
            name="documents",
            node_id=node_id,
        )

        assert command.operation_id == operation_id
        assert command.parent_id == ROOT_NODE_ID
        assert command.name == "documents"
        assert command.node_id == node_id
        assert command.version == COMMAND_VERSION

        with pytest.raises(dataclasses.FrozenInstanceError):
            command.name = "other"  # type: ignore[misc]


def every_command() -> list[MutationCommand]:
    return [
        CreateDir(
            operation_id=new_operation_id(),
            parent_id=ROOT_NODE_ID,
            name="documents",
            node_id=new_node_id(),
        ),
        CreateFileRef(
            operation_id=new_operation_id(),
            parent_id=ROOT_NODE_ID,
            name="report.pdf",
            message_id=1234,
            node_id=new_node_id(),
        ),
        RelinkFileRef(
            operation_id=new_operation_id(),
            node_id=new_node_id(),
            message_id=5678,
        ),
        DeleteNode(operation_id=new_operation_id(), node_id=new_node_id()),
        ClearRoot(operation_id=new_operation_id()),
        CopySubtree(
            operation_id=new_operation_id(),
            source_id=new_node_id(),
            parent_id=ROOT_NODE_ID,
            name="copy",
            node_id=new_node_id(),
        ),
        MoveNode(
            operation_id=new_operation_id(),
            node_id=new_node_id(),
            new_parent_id=ROOT_NODE_ID,
            new_name="renamed",
        ),
    ]


class TestEveryCommand:
    @pytest.mark.parametrize(
        "command", every_command(), ids=lambda c: type(c).__name__
    )
    def test_is_a_frozen_versioned_mutation_command(self, command):
        assert isinstance(command, MutationCommand)
        assert command.version == COMMAND_VERSION
        assert command.operation_id

        with pytest.raises(dataclasses.FrozenInstanceError):
            command.operation_id = new_operation_id()

class TestCommandIdValidation:
    def test_rejects_a_malformed_node_id(self):
        with pytest.raises(InvalidNodeId):
            CreateDir(
                operation_id=new_operation_id(),
                parent_id=ROOT_NODE_ID,
                name="documents",
                node_id="documents",  # type: ignore[arg-type]
            )

    def test_rejects_a_malformed_parent_id(self):
        with pytest.raises(InvalidNodeId):
            CreateDir(
                operation_id=new_operation_id(),
                parent_id="/documents",  # type: ignore[arg-type]
                name="documents",
                node_id=new_node_id(),
            )

    def test_rejects_a_path_shaped_node_id_in_every_command(self):
        # A path is exactly what a durable id must not be: it changes under a
        # rename, so a command holding one names a different node after a move.
        with pytest.raises(InvalidNodeId):
            DeleteNode(operation_id=new_operation_id(), node_id="/a/b")  # type: ignore[arg-type]

        with pytest.raises(InvalidNodeId):
            MoveNode(
                operation_id=new_operation_id(),
                node_id=new_node_id(),
                new_parent_id="/a",  # type: ignore[arg-type]
                new_name="b",
            )

        with pytest.raises(InvalidNodeId):
            CopySubtree(
                operation_id=new_operation_id(),
                source_id="7",  # type: ignore[arg-type]
                parent_id=ROOT_NODE_ID,
                name="copy",
                node_id=new_node_id(),
            )

    def test_rejects_a_malformed_operation_id(self):
        with pytest.raises(InvalidOperationId):
            ClearRoot(operation_id="op-1")  # type: ignore[arg-type]


class TestCommandPayloadValidation:
    @pytest.mark.parametrize("name", ["", "a/b", "-hidden", None, 5])
    def test_rejects_an_unusable_name(self, name):
        with pytest.raises(InvalidName):
            CreateDir(
                operation_id=new_operation_id(),
                parent_id=ROOT_NODE_ID,
                name=name,
                node_id=new_node_id(),
            )

    def test_rejects_an_unusable_new_name(self):
        with pytest.raises(InvalidName):
            MoveNode(
                operation_id=new_operation_id(),
                node_id=new_node_id(),
                new_parent_id=ROOT_NODE_ID,
                new_name="a/b",
            )

    @pytest.mark.parametrize("message_id", [0, -1, "12", None, True, 1.0])
    def test_rejects_an_unusable_message_id(self, message_id):
        with pytest.raises(InvalidCommandPayload):
            CreateFileRef(
                operation_id=new_operation_id(),
                parent_id=ROOT_NODE_ID,
                name="report.pdf",
                message_id=message_id,
                node_id=new_node_id(),
            )

        with pytest.raises(InvalidCommandPayload):
            RelinkFileRef(
                operation_id=new_operation_id(),
                node_id=new_node_id(),
                message_id=message_id,
            )


class TestRootSemantics:
    def test_delete_node_refuses_the_root(self):
        # Removing the root is not a delete of one more node: the store has to
        # keep having a root. Callers that mean "empty everything" say ClearRoot.
        with pytest.raises(RootNodeNotRemovable):
            DeleteNode(operation_id=new_operation_id(), node_id=ROOT_NODE_ID)

    def test_move_node_refuses_to_move_the_root(self):
        with pytest.raises(RootNodeNotRemovable):
            MoveNode(
                operation_id=new_operation_id(),
                node_id=ROOT_NODE_ID,
                new_parent_id=new_node_id(),
                new_name="anywhere",
            )

    def test_clear_root_is_the_way_to_empty_the_root(self):
        command = ClearRoot(operation_id=new_operation_id())

        assert isinstance(command, MutationCommand)

    @pytest.mark.parametrize(
        "build",
        [
            lambda: CreateDir(
                operation_id=new_operation_id(),
                parent_id=ROOT_NODE_ID,
                name="documents",
                node_id=ROOT_NODE_ID,
            ),
            lambda: CreateFileRef(
                operation_id=new_operation_id(),
                parent_id=ROOT_NODE_ID,
                name="report.pdf",
                message_id=1,
                node_id=ROOT_NODE_ID,
            ),
            lambda: CopySubtree(
                operation_id=new_operation_id(),
                source_id=new_node_id(),
                parent_id=ROOT_NODE_ID,
                name="copy",
                node_id=ROOT_NODE_ID,
            ),
            lambda: RelinkFileRef(
                operation_id=new_operation_id(),
                node_id=ROOT_NODE_ID,
                message_id=1,
            ),
        ],
        ids=["create_dir", "create_file_ref", "copy_subtree", "relink_file_ref"],
    )
    def test_the_root_id_never_names_a_new_or_relinked_node(self, build):
        with pytest.raises(InvalidCommandPayload):
            build()


class TestEveryCommandSet:
    def test_covers_the_declared_command_set(self):
        assert {type(command).__name__ for command in every_command()} == {
            "CreateDir",
            "CreateFileRef",
            "RelinkFileRef",
            "DeleteNode",
            "ClearRoot",
            "CopySubtree",
            "MoveNode",
        }
