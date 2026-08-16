"""Pure, read-only comparison of a source tree against a target tree.

No store, no import here - just two in-memory `TGFSDirectory` trees and the
report `reconcile` produces about how they differ. Each test builds both sides
by hand so the classification under test is the only thing that varies.
"""

from tgfs.core.metadata_import.reconcile import NodeMismatch, reconcile
from tgfs.core.model import TGFSDirectory


def _tree() -> TGFSDirectory:
    root = TGFSDirectory.root_dir()
    documents = root.create_dir("documents", None)
    documents.create_file_ref("notes.txt", 11)
    invoices = documents.create_dir("invoices", None)
    invoices.create_file_ref("2026.pdf", 77)
    return root


class TestIdentical:
    def test_two_separately_built_but_equal_trees_reconcile_as_equal(self):
        report = reconcile(_tree(), _tree())

        assert report.equal is True
        assert report.mismatches == ()
        assert report.missing_in_target == ()
        assert report.extra_in_target == ()
        assert report.source_dir_count == 2
        assert report.source_file_count == 2
        assert report.target_dir_count == 2
        assert report.target_file_count == 2

    def test_two_empty_roots_reconcile_as_equal(self):
        report = reconcile(TGFSDirectory.root_dir(), TGFSDirectory.root_dir())

        assert report.equal is True
        assert report.source_dir_count == 0
        assert report.source_file_count == 0


class TestMissingInTarget:
    def test_a_node_present_only_in_source_is_reported_missing(self):
        source = _tree()
        target = _tree()
        target.find_dir("documents").find_dir("invoices").delete()

        report = reconcile(source, target)

        assert report.equal is False
        assert report.missing_in_target == (
            "/documents/invoices",
            "/documents/invoices/2026.pdf",
        )
        assert report.extra_in_target == ()
        assert report.mismatches == ()


class TestExtraInTarget:
    def test_a_node_present_only_in_target_is_reported_extra(self):
        source = _tree()
        target = _tree()
        target.find_dir("documents").create_file_ref("extra.txt", 99)

        report = reconcile(source, target)

        assert report.equal is False
        assert report.missing_in_target == ()
        assert report.extra_in_target == ("/documents/extra.txt",)
        assert report.mismatches == ()


class TestKindMismatch:
    def test_a_path_that_is_a_directory_on_one_side_and_a_file_on_the_other_is_a_mismatch(
        self,
    ):
        source = _tree()
        target = _tree()
        # Same path, different kind: turn "invoices" from a directory into a
        # file ref by removing the directory and adding a file of that name.
        target.find_dir("documents").find_dir("invoices").delete()
        target.find_dir("documents").create_file_ref("invoices", 5)

        report = reconcile(source, target)

        assert report.equal is False
        assert report.mismatches == (
            NodeMismatch(
                path="/documents/invoices",
                field="kind",
                source_value="D",
                target_value="FR",
            ),
        )


class TestMessageIdMismatch:
    def test_a_file_ref_pointing_at_a_different_message_is_a_mismatch(self):
        source = _tree()
        target = _tree()
        target.find_dir("documents").find_file("notes.txt").message_id = 12

        report = reconcile(source, target)

        assert report.equal is False
        assert report.mismatches == (
            NodeMismatch(
                path="/documents/notes.txt",
                field="message_id",
                source_value=11,
                target_value=12,
            ),
        )


class TestNameAndParentMismatch:
    def test_a_renamed_node_shows_up_as_missing_and_extra_by_path(self):
        source = _tree()
        target = _tree()
        target.find_dir("documents").find_file("notes.txt").name = "renamed.txt"

        report = reconcile(source, target)

        assert report.equal is False
        assert report.missing_in_target == ("/documents/notes.txt",)
        assert report.extra_in_target == ("/documents/renamed.txt",)

    def test_a_node_moved_to_a_different_parent_shows_up_as_missing_and_extra_by_path(
        self,
    ):
        source = _tree()
        target = _tree()
        documents = target.find_dir("documents")
        invoices = documents.find_dir("invoices")
        notes = documents.find_file("notes.txt")
        documents.delete_file_ref(notes)
        invoices.files.append(notes)
        notes.location = invoices

        report = reconcile(source, target)

        assert report.equal is False
        assert report.missing_in_target == ("/documents/notes.txt",)
        assert report.extra_in_target == ("/documents/invoices/notes.txt",)
