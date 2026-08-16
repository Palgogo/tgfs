import dataclasses

import pytest

from tgfs.core.commands import (
    COMMAND_VERSION,
    ROOT_NODE_ID,
    AppliedCommand,
    NodeKind,
    NodeSnapshot,
    applied_command_from_dict,
    new_node_id,
    new_operation_id,
)
from tgfs.core.model import TGFSDirectory
from tgfs.errors import InvalidCommandPayload, InvalidNodeId, RootNodeNotRemovable


def a_dir_snapshot(**overrides) -> NodeSnapshot:
    return NodeSnapshot(
        **{
            "node_id": new_node_id(),
            "parent_id": ROOT_NODE_ID,
            "name": "documents",
            "kind": NodeKind.DIRECTORY,
            **overrides,
        }
    )


def a_file_ref_snapshot(**overrides) -> NodeSnapshot:
    return NodeSnapshot(
        **{
            "node_id": new_node_id(),
            "parent_id": ROOT_NODE_ID,
            "name": "report.pdf",
            "kind": NodeKind.FILE_REF,
            "message_id": 1234,
            **overrides,
        }
    )


class TestNodeSnapshot:
    def test_describes_a_directory_as_a_frozen_value(self):
        node_id = new_node_id()

        snapshot = NodeSnapshot(
            node_id=node_id,
            parent_id=ROOT_NODE_ID,
            name="documents",
            kind=NodeKind.DIRECTORY,
        )

        assert snapshot.node_id == node_id
        assert snapshot.parent_id == ROOT_NODE_ID
        assert snapshot.name == "documents"
        assert snapshot.kind is NodeKind.DIRECTORY
        assert snapshot.message_id is None

        with pytest.raises(dataclasses.FrozenInstanceError):
            snapshot.name = "other"  # type: ignore[misc]

    def test_describes_a_file_ref_by_the_message_it_points_at(self):
        snapshot = a_file_ref_snapshot(message_id=5678)

        assert snapshot.kind is NodeKind.FILE_REF
        assert snapshot.message_id == 5678

    def test_refuses_a_file_ref_without_a_message(self):
        with pytest.raises(InvalidCommandPayload):
            a_file_ref_snapshot(message_id=None)

    def test_refuses_a_directory_carrying_a_message(self):
        with pytest.raises(InvalidCommandPayload):
            a_dir_snapshot(message_id=1234)

    def test_refuses_an_id_that_is_not_a_durable_node_id(self):
        with pytest.raises(InvalidNodeId):
            a_dir_snapshot(node_id="/documents")

        with pytest.raises(InvalidNodeId):
            a_dir_snapshot(parent_id="root")

    def test_refuses_the_root_as_a_snapshotted_node(self):
        # The root is created by no command and so is described by none: a
        # snapshot naming it would let a change set replace the one node every
        # store is required to keep.
        with pytest.raises(RootNodeNotRemovable):
            a_dir_snapshot(node_id=ROOT_NODE_ID)


class TestAppliedCommand:
    def test_is_a_frozen_versioned_record_of_what_the_store_accepted(self):
        operation_id = new_operation_id()
        created = a_dir_snapshot()

        applied = AppliedCommand(
            revision=7,
            operation_id=operation_id,
            created=(created,),
        )

        assert applied.revision == 7
        assert applied.operation_id == operation_id
        assert applied.created == (created,)
        assert applied.updated == ()
        assert applied.deleted == ()
        assert applied.version == COMMAND_VERSION

        with pytest.raises(dataclasses.FrozenInstanceError):
            applied.revision = 8  # type: ignore[misc]

    def test_refuses_change_sets_the_caller_could_still_mutate(self):
        # A list handed in would stay aliased to the caller: the record of what
        # was durably accepted has to stop changing the moment it is made.
        with pytest.raises(InvalidCommandPayload):
            AppliedCommand(
                revision=1, operation_id=new_operation_id(), created=[a_dir_snapshot()]
            )

        with pytest.raises(InvalidCommandPayload):
            AppliedCommand(
                revision=1, operation_id=new_operation_id(), deleted=[new_node_id()]
            )

    def test_refuses_to_carry_a_live_model_object(self):
        # TGFSDirectory and TGFSFileRef are mutable and parent-linked; holding
        # one would make this record change under whoever reads it later.
        with pytest.raises(InvalidCommandPayload):
            AppliedCommand(
                revision=1,
                operation_id=new_operation_id(),
                created=(TGFSDirectory.root_dir(),),
            )

    @pytest.mark.parametrize("revision", [0, -1, 1.5, "3", True, None])
    def test_refuses_a_revision_that_cannot_order_two_writes(self, revision):
        with pytest.raises(InvalidCommandPayload):
            AppliedCommand(revision=revision, operation_id=new_operation_id())

    def test_refuses_a_deletion_that_is_not_a_durable_node_id(self):
        with pytest.raises(InvalidNodeId):
            AppliedCommand(
                revision=1, operation_id=new_operation_id(), deleted=("/documents",)
            )

    def test_refuses_deleting_the_root(self):
        with pytest.raises(RootNodeNotRemovable):
            AppliedCommand(
                revision=1, operation_id=new_operation_id(), deleted=(ROOT_NODE_ID,)
            )

    def test_refuses_naming_the_same_node_twice(self):
        node_id = new_node_id()

        with pytest.raises(InvalidCommandPayload):
            AppliedCommand(
                revision=1,
                operation_id=new_operation_id(),
                created=(a_dir_snapshot(node_id=node_id),),
                deleted=(node_id,),
            )

        with pytest.raises(InvalidCommandPayload):
            AppliedCommand(
                revision=1,
                operation_id=new_operation_id(),
                created=(a_dir_snapshot(node_id=node_id),),
                updated=(a_dir_snapshot(node_id=node_id),),
            )

    def test_refuses_a_change_set_that_changes_nothing(self):
        # Every revision has to mean a difference, or a reader cannot tell a
        # replayed retry from a write that was genuinely accepted.
        with pytest.raises(InvalidCommandPayload):
            AppliedCommand(revision=1, operation_id=new_operation_id())


class TestSerialization:
    def test_round_trips_every_kind_of_change(self):
        applied = AppliedCommand(
            revision=42,
            operation_id=new_operation_id(),
            created=(a_dir_snapshot(), a_file_ref_snapshot()),
            updated=(a_file_ref_snapshot(message_id=999),),
            deleted=(new_node_id(),),
        )

        assert applied_command_from_dict(applied.to_dict()) == applied

    def test_serializes_to_plain_json_able_values(self):
        node_id = new_node_id()
        operation_id = new_operation_id()

        applied = AppliedCommand(
            revision=3,
            operation_id=operation_id,
            created=(
                NodeSnapshot(
                    node_id=node_id,
                    parent_id=ROOT_NODE_ID,
                    name="report.pdf",
                    kind=NodeKind.FILE_REF,
                    message_id=17,
                ),
            ),
        )

        assert applied.to_dict() == {
            "version": COMMAND_VERSION,
            "revision": 3,
            "operationId": operation_id,
            "created": [
                {
                    "type": "FR",
                    "nodeId": node_id,
                    "parentId": ROOT_NODE_ID,
                    "name": "report.pdf",
                    "messageId": 17,
                }
            ],
            "updated": [],
            "deleted": [],
        }

    def test_refuses_a_record_written_by_another_version(self):
        applied = AppliedCommand(
            revision=1, operation_id=new_operation_id(), created=(a_dir_snapshot(),)
        )
        data = applied.to_dict() | {"version": COMMAND_VERSION + 1}

        with pytest.raises(InvalidCommandPayload):
            applied_command_from_dict(data)

    @pytest.mark.parametrize(
        "drop", ["version", "revision", "operationId", "created", "updated", "deleted"]
    )
    def test_refuses_a_record_missing_a_field(self, drop):
        applied = AppliedCommand(
            revision=1, operation_id=new_operation_id(), created=(a_dir_snapshot(),)
        )
        data = {k: v for k, v in applied.to_dict().items() if k != drop}

        with pytest.raises(InvalidCommandPayload):
            applied_command_from_dict(data)

    def test_refuses_a_record_carrying_anything_it_does_not_understand(self):
        applied = AppliedCommand(
            revision=1, operation_id=new_operation_id(), created=(a_dir_snapshot(),)
        )

        with pytest.raises(InvalidCommandPayload):
            applied_command_from_dict(applied.to_dict() | {"appliedAt": "now"})

        with pytest.raises(InvalidCommandPayload):
            applied_command_from_dict(
                applied.to_dict() | {"created": [{"type": "?", "nodeId": new_node_id()}]}
            )

    def test_refuses_anything_that_is_not_a_record(self):
        for value in [None, [], "{}", 42]:
            with pytest.raises(InvalidCommandPayload):
                applied_command_from_dict(value)
