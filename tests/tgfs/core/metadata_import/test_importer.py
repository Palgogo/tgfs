"""Loading a source tree into a fresh SQLite target.

Every test here builds a plain in-memory `TGFSDirectory` as the source - the
same shape `GithubRepoMetadataRepository.get()` would hand back - and a path to
an as-yet-nonexistent SQLite file as the target. No network, no live store, no
production wiring: just `import_metadata` against P1c/P1b as they already are.
"""

from pathlib import Path

import pytest

from tgfs.core.metadata_import.importer import (
    ImportFailed,
    TargetNotEmpty,
    import_metadata,
)
from tgfs.core.metadata_import.reconcile import reconcile
from tgfs.core.model import TGFSDirectory, TGFSFileRef
from tgfs.core.repository.impl.metadata.sqlite_metadata import SqliteMetadataRepository


def _tree() -> TGFSDirectory:
    root = TGFSDirectory.root_dir()
    documents = root.create_dir("documents", None)
    documents.create_file_ref("notes.txt", 11)
    invoices = documents.create_dir("invoices", None)
    invoices.create_file_ref("2026.pdf", 77)
    photos = root.create_dir("photos", None)
    photos.create_file_ref("beach.jpg", 42)
    return root


async def _reopen(target_path: Path) -> TGFSDirectory:
    """The target as a second, independent repository sees it after a restart."""
    repository = SqliteMetadataRepository(target_path)
    await repository.init()
    try:
        return repository.root()
    finally:
        await repository.close()


class _SpySource(TGFSDirectory):
    """Raises if the importer ever asks this tree to mutate itself."""

    def create_dir(self, name, dir_to_copy=None):
        raise AssertionError("import must not create directories on the source")

    def create_file_ref(self, name, fd_message_id):
        raise AssertionError("import must not create file refs on the source")

    def delete(self):
        raise AssertionError("import must not delete from the source")

    def delete_file_ref(self, fr):
        raise AssertionError("import must not delete from the source")


def _spy_tree() -> _SpySource:
    """The same shape as `_tree`, built by appending directly to the lists so
    that no mutating method of `_SpySource` is exercised while setting it up -
    only the importer's own calls are under test."""
    root = _SpySource(name="root", parent=None)

    documents = _SpySource(name="documents", parent=root)
    root.children.append(documents)
    documents.files.append(TGFSFileRef(message_id=11, name="notes.txt", location=documents))

    invoices = _SpySource(name="invoices", parent=documents)
    documents.children.append(invoices)
    invoices.files.append(TGFSFileRef(message_id=77, name="2026.pdf", location=invoices))

    return root


class TestImporting:
    async def test_a_nested_tree_is_translated_into_the_same_paths(
        self, target_path: Path
    ):
        source = _tree()

        result = await import_metadata(source, target_path)

        assert result.source_dir_count == 3
        assert result.source_file_count == 3
        assert result.target_dir_count == 3
        assert result.target_file_count == 3
        assert result.reconciliation.equal is True

        target = await _reopen(target_path)
        assert reconcile(source, target).equal is True

    async def test_an_empty_source_leaves_only_the_root(self, target_path: Path):
        source = TGFSDirectory.root_dir()

        result = await import_metadata(source, target_path)

        assert result.source_dir_count == 0
        assert result.source_file_count == 0
        assert result.target_dir_count == 0
        assert result.target_file_count == 0
        assert result.durable_revision == 0

    async def test_the_result_reports_bounded_elapsed_time_and_a_durable_revision(
        self, target_path: Path
    ):
        result = await import_metadata(_tree(), target_path)

        assert result.elapsed_seconds >= 0
        assert result.reopen_elapsed_seconds >= 0
        assert result.durable_revision > 0


class TestReadOnlySourceBoundary:
    async def test_the_source_tree_is_never_mutated(self, target_path: Path):
        source = _spy_tree()

        result = await import_metadata(source, target_path)

        assert result.reconciliation.equal is True


class TestRejectingANonEmptyTarget:
    async def test_a_target_that_already_holds_data_is_refused(
        self, target_path: Path
    ):
        first_source = _tree()
        await import_metadata(first_source, target_path)

        with pytest.raises(TargetNotEmpty):
            await import_metadata(_tree(), target_path)

        # Refused before any mutation: the original import is untouched.
        target = await _reopen(target_path)
        assert reconcile(first_source, target).equal is True


class TestCommandFailureIsReportedClearly:
    async def test_a_source_with_a_name_collision_fails_the_import_instead_of_merging(
        self, target_path: Path
    ):
        # Built by hand rather than through `create_dir`, which would itself
        # refuse the collision - this stands in for a source that is not
        # internally consistent, which the import must still fail loudly on.
        root = TGFSDirectory.root_dir()
        root.children.append(TGFSDirectory(name="dup", parent=root))
        root.children.append(TGFSDirectory(name="dup", parent=root))

        with pytest.raises(ImportFailed):
            await import_metadata(root, target_path)
