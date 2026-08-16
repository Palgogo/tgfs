import dataclasses

import pytest

from tgfs.core.commands import (
    ROOT_NODE_ID,
    AppliedCommand,
    DeleteNode,
    NodeKind,
    NodeSnapshot,
    Projection,
    new_node_id,
    new_operation_id,
    patch_projection,
)
from tgfs.errors import ProjectionPatchError


def a_dir(node_id, parent_id=ROOT_NODE_ID, name="documents") -> NodeSnapshot:
    return NodeSnapshot(
        node_id=node_id, parent_id=parent_id, name=name, kind=NodeKind.DIRECTORY
    )


def a_file_ref(node_id, parent_id=ROOT_NODE_ID, name="report.pdf", message_id=1234):
    return NodeSnapshot(
        node_id=node_id,
        parent_id=parent_id,
        name=name,
        kind=NodeKind.FILE_REF,
        message_id=message_id,
    )


def applied(revision, **changes) -> AppliedCommand:
    return AppliedCommand(
        revision=revision, operation_id=new_operation_id(), **changes
    )


def with_a_dir(projection: Projection, node_id, **kwargs) -> Projection:
    return patch_projection(
        projection, applied(projection.revision + 1, created=(a_dir(node_id, **kwargs),))
    )


class TestEmptyProjection:
    def test_holds_only_the_root_at_revision_zero(self):
        projection = Projection.empty()

        assert projection.revision == 0
        assert projection.contains(ROOT_NODE_ID)
        assert projection.children(ROOT_NODE_ID) == ()

    def test_is_a_frozen_value(self):
        projection = Projection.empty()

        with pytest.raises(dataclasses.FrozenInstanceError):
            projection.revision = 1  # type: ignore[misc]


class TestCreate:
    def test_adds_the_node_and_advances_the_revision(self):
        node_id = new_node_id()

        patched = patch_projection(
            Projection.empty(), applied(1, created=(a_dir(node_id),))
        )

        assert patched.revision == 1
        assert patched.node(node_id).name == "documents"
        assert patched.children(ROOT_NODE_ID) == (patched.node(node_id),)

    def test_leaves_the_projection_it_was_given_untouched(self):
        before = Projection.empty()

        patch_projection(before, applied(1, created=(a_dir(new_node_id()),)))

        assert before.revision == 0
        assert before.children(ROOT_NODE_ID) == ()

    def test_refuses_a_node_whose_parent_it_has_never_seen(self):
        with pytest.raises(ProjectionPatchError):
            patch_projection(
                Projection.empty(),
                applied(1, created=(a_dir(new_node_id(), parent_id=new_node_id()),)),
            )

    def test_refuses_a_node_it_already_holds(self):
        node_id = new_node_id()
        projection = with_a_dir(Projection.empty(), node_id)

        with pytest.raises(ProjectionPatchError):
            patch_projection(projection, applied(2, created=(a_dir(node_id),)))


class TestCopy:
    def test_adds_a_whole_subtree_named_parent_before_child(self):
        # A copy is reported as the snapshots that ended up existing, so the
        # patcher never has to know what was copied from where.
        copy_root, copied_child = new_node_id(), new_node_id()

        patched = patch_projection(
            Projection.empty(),
            applied(
                1,
                created=(
                    a_dir(copy_root, name="copy"),
                    a_file_ref(copied_child, parent_id=copy_root),
                ),
            ),
        )

        assert patched.children(copy_root) == (patched.node(copied_child),)

    def test_refuses_a_subtree_whose_parents_come_after_their_children(self):
        copy_root, copied_child = new_node_id(), new_node_id()

        with pytest.raises(ProjectionPatchError):
            patch_projection(
                Projection.empty(),
                applied(
                    1,
                    created=(
                        a_file_ref(copied_child, parent_id=copy_root),
                        a_dir(copy_root, name="copy"),
                    ),
                ),
            )


class TestRelinkAndMove:
    def test_relink_points_a_file_ref_at_another_message(self):
        node_id = new_node_id()
        projection = patch_projection(
            Projection.empty(), applied(1, created=(a_file_ref(node_id),))
        )

        patched = patch_projection(
            projection, applied(2, updated=(a_file_ref(node_id, message_id=5678),))
        )

        assert patched.node(node_id).message_id == 5678

    def test_move_reparents_and_renames(self):
        target, node_id = new_node_id(), new_node_id()
        projection = with_a_dir(Projection.empty(), target, name="target")
        projection = with_a_dir(projection, node_id)

        patched = patch_projection(
            projection,
            applied(3, updated=(a_dir(node_id, parent_id=target, name="renamed"),)),
        )

        assert patched.node(node_id).parent_id == target
        assert patched.node(node_id).name == "renamed"
        assert patched.children(ROOT_NODE_ID) == (patched.node(target),)
        assert patched.children(target) == (patched.node(node_id),)

    def test_refuses_updating_a_node_it_has_never_seen(self):
        with pytest.raises(ProjectionPatchError):
            patch_projection(
                Projection.empty(), applied(1, updated=(a_dir(new_node_id()),))
            )

    def test_refuses_a_move_under_the_node_being_moved(self):
        parent, child = new_node_id(), new_node_id()
        projection = with_a_dir(Projection.empty(), parent)
        projection = with_a_dir(projection, child, parent_id=parent, name="child")

        with pytest.raises(ProjectionPatchError):
            patch_projection(
                projection, applied(3, updated=(a_dir(parent, parent_id=child),))
            )

    def test_refuses_changing_what_a_node_is(self):
        node_id = new_node_id()
        projection = with_a_dir(Projection.empty(), node_id)

        with pytest.raises(ProjectionPatchError):
            patch_projection(
                projection, applied(2, updated=(a_file_ref(node_id, name="documents"),))
            )


class TestDelete:
    def test_removes_the_node_and_everything_below_it(self):
        parent, child, grandchild = new_node_id(), new_node_id(), new_node_id()
        projection = with_a_dir(Projection.empty(), parent)
        projection = with_a_dir(projection, child, parent_id=parent, name="child")
        projection = patch_projection(
            projection,
            applied(3, created=(a_file_ref(grandchild, parent_id=child),)),
        )

        patched = patch_projection(projection, applied(4, deleted=(parent,)))

        assert not patched.contains(parent)
        assert not patched.contains(child)
        assert not patched.contains(grandchild)
        assert patched.children(ROOT_NODE_ID) == ()

    def test_refuses_deleting_a_node_it_has_never_seen(self):
        with pytest.raises(ProjectionPatchError):
            patch_projection(
                Projection.empty(), applied(1, deleted=(new_node_id(),))
            )

    def test_clearing_the_root_is_the_deletion_of_its_children(self):
        first, second = new_node_id(), new_node_id()
        projection = with_a_dir(Projection.empty(), first)
        projection = with_a_dir(projection, second, name="second")

        patched = patch_projection(projection, applied(3, deleted=(first, second)))

        assert patched.children(ROOT_NODE_ID) == ()
        assert patched.contains(ROOT_NODE_ID)


class TestOrdering:
    def test_applies_deletions_before_creations(self):
        # A store is free to accept "replace what was there" as one revision;
        # the projection has to end up with the new node, not refuse the reuse
        # of a name or lose the creation to the deletion.
        old, new = new_node_id(), new_node_id()
        projection = with_a_dir(Projection.empty(), old)

        patched = patch_projection(
            projection, applied(2, created=(a_dir(new),), deleted=(old,))
        )

        assert patched.children(ROOT_NODE_ID) == (patched.node(new),)

    def test_refuses_a_revision_that_does_not_follow_the_one_it_holds(self):
        projection = Projection.empty()

        with pytest.raises(ProjectionPatchError):
            patch_projection(projection, applied(2, created=(a_dir(new_node_id()),)))

        advanced = with_a_dir(projection, new_node_id())

        with pytest.raises(ProjectionPatchError):
            patch_projection(advanced, applied(1, created=(a_dir(new_node_id()),)))

    def test_a_refused_patch_changes_nothing(self):
        node_id = new_node_id()
        projection = with_a_dir(Projection.empty(), node_id)

        with pytest.raises(ProjectionPatchError):
            patch_projection(
                projection,
                applied(
                    2,
                    created=(a_dir(new_node_id()),),
                    deleted=(new_node_id(),),
                ),
            )

        assert projection.revision == 1
        assert projection.children(ROOT_NODE_ID) == (projection.node(node_id),)


class TestWhatThePatcherRefusesToBe:
    def test_refuses_a_command_that_was_never_durably_accepted(self):
        # The patcher is downstream of durability by construction: handing it a
        # command would let a caller move the projection ahead of the store.
        command = DeleteNode(operation_id=new_operation_id(), node_id=new_node_id())

        with pytest.raises(ProjectionPatchError):
            patch_projection(Projection.empty(), command)  # type: ignore[arg-type]

    def test_refuses_anything_that_is_not_a_projection(self):
        with pytest.raises(ProjectionPatchError):
            patch_projection({}, applied(1, created=(a_dir(new_node_id()),)))  # type: ignore[arg-type]
