"""Copying a subtree.

A copy is the one command whose result the caller cannot predict: it names only
the root of the copy, and the store mints an id for every descendant because it
is the only thing that knows what the subtree held at the instant the copy was
accepted. Those ids come back in the change set, and they have to be new - a
copy that shared ids with its source would be an alias, and deleting one would
take the other with it.
"""

import pytest

from tgfs.core.commands import (
    ROOT_NODE_ID,
    CopySubtree,
    DeleteNode,
    NodeKind,
    RelinkFileRef,
    new_node_id,
    new_operation_id,
    patch_projection,
)
from tgfs.errors import (
    FileOrDirectoryAlreadyExists,
    FileOrDirectoryDoesNotExist,
    InvalidCommandPayload,
)

from . import durable


async def _tree(store, make_dir, make_file) -> dict:
    """docs/{2024/{06/report.pdf}, readme.md}"""
    docs = await make_dir("docs")
    year = await make_dir("2024", parent_id=docs)
    month = await make_dir("06", parent_id=year)
    report = await make_file("report.pdf", parent_id=month, message_id=1000)
    readme = await make_file("readme.md", parent_id=docs, message_id=1001)
    return dict(docs=docs, year=year, month=month, report=report, readme=readme)


class TestCopyingOneNode:
    async def test_reports_the_copy_under_the_id_it_was_given(
        self, store, make_dir
    ) -> None:
        source_id = await make_dir("docs")
        copy_id = new_node_id()

        applied = await store.accept(
            CopySubtree(
                operation_id=new_operation_id(),
                source_id=source_id,
                parent_id=ROOT_NODE_ID,
                name="docs-copy",
                node_id=copy_id,
            )
        )

        assert [s.node_id for s in applied.created] == [copy_id]
        assert applied.created[0].name == "docs-copy"
        assert applied.created[0].parent_id == ROOT_NODE_ID

    async def test_a_copied_file_ref_points_at_the_same_message(
        self, store, make_file, db_path
    ) -> None:
        source_id = await make_file("report.pdf", message_id=1000)
        copy_id = new_node_id()

        await store.accept(
            CopySubtree(
                operation_id=new_operation_id(),
                source_id=source_id,
                parent_id=ROOT_NODE_ID,
                name="report-copy.pdf",
                node_id=copy_id,
            )
        )

        row = durable.require_node_row(db_path, copy_id)
        assert row["kind"] == "FR"
        assert row["message_id"] == 1000


class TestCopyingASubtree:
    async def test_copies_every_descendant(
        self, store, make_dir, make_file, db_path
    ) -> None:
        source = await _tree(store, make_dir, make_file)
        copy_id = new_node_id()

        applied = await store.accept(
            CopySubtree(
                operation_id=new_operation_id(),
                source_id=source["docs"],
                parent_id=ROOT_NODE_ID,
                name="docs-copy",
                node_id=copy_id,
            )
        )

        # Five nodes in, five nodes out.
        assert len(applied.created) == 5
        assert durable.node_count(db_path) == 11  # root + 5 + 5

    async def test_every_copied_node_gets_a_new_id(
        self, store, make_dir, make_file
    ) -> None:
        source = await _tree(store, make_dir, make_file)

        applied = await store.accept(
            CopySubtree(
                operation_id=new_operation_id(),
                source_id=source["docs"],
                parent_id=ROOT_NODE_ID,
                name="docs-copy",
                node_id=new_node_id(),
            )
        )

        copied = {s.node_id for s in applied.created}
        assert copied.isdisjoint(set(source.values()))
        assert len(copied) == len(applied.created)

    async def test_the_copy_has_the_same_shape_and_names(
        self, store, make_dir, make_file
    ) -> None:
        source = await _tree(store, make_dir, make_file)
        copy_id = new_node_id()

        applied = await store.accept(
            CopySubtree(
                operation_id=new_operation_id(),
                source_id=source["docs"],
                parent_id=ROOT_NODE_ID,
                name="docs-copy",
                node_id=copy_id,
            )
        )

        by_id = {s.node_id: s for s in applied.created}

        def path(snapshot) -> str:
            parts = [snapshot.name]
            while snapshot.parent_id in by_id:
                snapshot = by_id[snapshot.parent_id]
                parts.append(snapshot.name)
            return "/".join(reversed(parts))

        assert sorted(path(s) for s in applied.created) == [
            "docs-copy",
            "docs-copy/2024",
            "docs-copy/2024/06",
            "docs-copy/2024/06/report.pdf",
            "docs-copy/readme.md",
        ]

    async def test_names_parents_before_their_children(
        self, store, make_dir, make_file
    ) -> None:
        # The reader patches its view in the order it is given, so a child that
        # arrives before its parent cannot be placed.
        source = await _tree(store, make_dir, make_file)
        applied = await store.accept(
            CopySubtree(
                operation_id=new_operation_id(),
                source_id=source["docs"],
                parent_id=ROOT_NODE_ID,
                name="docs-copy",
                node_id=new_node_id(),
            )
        )

        seen = set()
        for snapshot in applied.created:
            assert snapshot.parent_id == ROOT_NODE_ID or snapshot.parent_id in seen
            seen.add(snapshot.node_id)

    async def test_the_change_set_can_be_patched_into_a_projection(
        self, store, make_dir, make_file, open_store
    ) -> None:
        source = await _tree(store, make_dir, make_file)
        projection = await store.load()
        applied = await store.accept(
            CopySubtree(
                operation_id=new_operation_id(),
                source_id=source["docs"],
                parent_id=ROOT_NODE_ID,
                name="docs-copy",
                node_id=new_node_id(),
            )
        )

        patched = patch_projection(projection, applied)

        assert dict(patched.nodes) == dict((await store.load()).nodes)

    async def test_the_copy_survives_a_restart(
        self, store, make_dir, make_file, open_store
    ) -> None:
        source = await _tree(store, make_dir, make_file)
        applied = await store.accept(
            CopySubtree(
                operation_id=new_operation_id(),
                source_id=source["docs"],
                parent_id=ROOT_NODE_ID,
                name="docs-copy",
                node_id=new_node_id(),
            )
        )
        await store.close()

        projection = await (await open_store()).load()

        for snapshot in applied.created:
            assert projection.node(snapshot.node_id) == snapshot


class TestTheCopyIsIndependent:
    async def test_deleting_the_source_leaves_the_copy_alone(
        self, store, make_dir, make_file, db_path
    ) -> None:
        source = await _tree(store, make_dir, make_file)
        applied = await store.accept(
            CopySubtree(
                operation_id=new_operation_id(),
                source_id=source["docs"],
                parent_id=ROOT_NODE_ID,
                name="docs-copy",
                node_id=new_node_id(),
            )
        )

        await store.accept(
            DeleteNode(operation_id=new_operation_id(), node_id=source["docs"])
        )

        for snapshot in applied.created:
            assert durable.node_row(db_path, snapshot.node_id) is not None

    async def test_relinking_a_copied_file_leaves_the_source_alone(
        self, store, make_dir, make_file, db_path
    ) -> None:
        source = await _tree(store, make_dir, make_file)
        applied = await store.accept(
            CopySubtree(
                operation_id=new_operation_id(),
                source_id=source["docs"],
                parent_id=ROOT_NODE_ID,
                name="docs-copy",
                node_id=new_node_id(),
            )
        )
        copied_report = next(
            s
            for s in applied.created
            if s.kind is NodeKind.FILE_REF and s.name == "report.pdf"
        )

        await store.accept(
            RelinkFileRef(
                operation_id=new_operation_id(),
                node_id=copied_report.node_id,
                message_id=9999,
            )
        )

        assert durable.require_node_row(db_path, source["report"])["message_id"] == 1000
        assert durable.require_node_row(db_path, copied_report.node_id)["message_id"] == 9999


class TestRefusingToCopy:
    async def test_refuses_a_source_that_is_not_there(self, store, db_path) -> None:
        before = durable.snapshot(db_path)

        with pytest.raises(FileOrDirectoryDoesNotExist):
            await store.accept(
                CopySubtree(
                    operation_id=new_operation_id(),
                    source_id=new_node_id(),
                    parent_id=ROOT_NODE_ID,
                    name="docs-copy",
                    node_id=new_node_id(),
                )
            )

        assert durable.snapshot(db_path) == before

    async def test_refuses_a_name_the_target_parent_already_uses(
        self, store, make_dir, make_file, db_path
    ) -> None:
        source = await _tree(store, make_dir, make_file)
        before = durable.snapshot(db_path)

        with pytest.raises(FileOrDirectoryAlreadyExists):
            await store.accept(
                CopySubtree(
                    operation_id=new_operation_id(),
                    source_id=source["docs"],
                    parent_id=ROOT_NODE_ID,
                    name="docs",
                    node_id=new_node_id(),
                )
            )

        assert durable.snapshot(db_path) == before

    async def test_refuses_a_target_parent_that_is_not_there(
        self, store, make_dir, db_path
    ) -> None:
        source_id = await make_dir("docs")
        before = durable.snapshot(db_path)

        with pytest.raises(FileOrDirectoryDoesNotExist):
            await store.accept(
                CopySubtree(
                    operation_id=new_operation_id(),
                    source_id=source_id,
                    parent_id=new_node_id(),
                    name="docs-copy",
                    node_id=new_node_id(),
                )
            )

        assert durable.snapshot(db_path) == before

    async def test_refuses_to_copy_the_root(self, store, make_dir, db_path) -> None:
        # The root is the fixed point the tree hangs from: it has no name and no
        # parent, so there is nothing for a copy to reproduce. The store has to
        # say that, rather than fail somewhere inside the row decoder and blame
        # the caller for a node id it never sent.
        docs = await make_dir("docs")
        before = durable.snapshot(db_path)

        with pytest.raises(InvalidCommandPayload):
            await store.accept(
                CopySubtree(
                    operation_id=new_operation_id(),
                    source_id=ROOT_NODE_ID,
                    parent_id=docs,
                    name="everything",
                    node_id=new_node_id(),
                )
            )

        assert durable.snapshot(db_path) == before

    async def test_copying_into_its_own_subtree_copies_what_was_there(
        self, store, make_dir, make_file
    ) -> None:
        # Not a refusal: a copy is of the subtree as it stood when the command
        # was accepted, so the result is finite and the recursion is not.
        source = await _tree(store, make_dir, make_file)

        applied = await store.accept(
            CopySubtree(
                operation_id=new_operation_id(),
                source_id=source["docs"],
                parent_id=source["month"],
                name="docs-copy",
                node_id=new_node_id(),
            )
        )

        assert len(applied.created) == 5
        assert applied.created[0].parent_id == source["month"]
