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
    MalformedSource,
    TargetPathExists,
    import_metadata,
)
from tgfs.core.metadata_import.provenance import SourceDescriptor, TargetDescriptor
from tgfs.core.metadata_import.reconcile import reconcile
from tgfs.core.model import TGFSDirectory, TGFSFileRef
from tgfs.core.repository.impl.metadata.sqlite_command_store import SqliteCommandStore
from tgfs.core.repository.impl.metadata.sqlite_metadata import SqliteMetadataRepository

_SOURCE_DESCRIPTOR = SourceDescriptor(repository="octo/demo", ref="deadbeef")


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

        result = await import_metadata(
            source, target_path, source_descriptor=_SOURCE_DESCRIPTOR
        )

        assert result.source_dir_count == 3
        assert result.source_file_count == 3
        assert result.target_dir_count == 3
        assert result.target_file_count == 3
        assert result.reconciliation.equal is True

        target = await _reopen(target_path)
        assert reconcile(source, target).equal is True

    async def test_an_empty_source_leaves_only_the_root(self, target_path: Path):
        source = TGFSDirectory.root_dir()

        result = await import_metadata(
            source, target_path, source_descriptor=_SOURCE_DESCRIPTOR
        )

        assert result.source_dir_count == 0
        assert result.source_file_count == 0
        assert result.target_dir_count == 0
        assert result.target_file_count == 0
        assert result.durable_revision == 0

    async def test_the_result_reports_bounded_elapsed_time_and_a_durable_revision(
        self, target_path: Path
    ):
        result = await import_metadata(
            _tree(), target_path, source_descriptor=_SOURCE_DESCRIPTOR
        )

        assert result.elapsed_seconds >= 0
        assert result.reopen_elapsed_seconds >= 0
        assert result.durable_revision > 0


class TestProvenanceIsRecorded:
    async def test_the_result_carries_the_caller_supplied_source_descriptor(
        self, target_path: Path
    ):
        result = await import_metadata(
            _tree(), target_path, source_descriptor=_SOURCE_DESCRIPTOR
        )

        assert result.source_descriptor == _SOURCE_DESCRIPTOR
        assert result.target_descriptor == TargetDescriptor.of(target_path)

    async def test_the_result_is_stamped_with_a_started_and_completed_utc_timestamp(
        self, target_path: Path
    ):
        result = await import_metadata(
            _tree(), target_path, source_descriptor=_SOURCE_DESCRIPTOR
        )

        assert result.started_at <= result.completed_at
        assert result.started_at.endswith("+00:00")
        assert result.completed_at.endswith("+00:00")

    async def test_the_reconciliation_report_carries_the_same_descriptors(
        self, target_path: Path
    ):
        result = await import_metadata(
            _tree(), target_path, source_descriptor=_SOURCE_DESCRIPTOR
        )

        assert result.reconciliation.source_descriptor == _SOURCE_DESCRIPTOR
        assert result.reconciliation.target_descriptor == TargetDescriptor.of(
            target_path
        )
        assert result.reconciliation.completed_at is not None


class TestReadOnlySourceBoundary:
    async def test_the_source_tree_is_never_mutated(self, target_path: Path):
        source = _spy_tree()

        result = await import_metadata(
            source, target_path, source_descriptor=_SOURCE_DESCRIPTOR
        )

        assert result.reconciliation.equal is True


class TestTargetPathIsRefusedIfItAlreadyExists:
    async def test_a_target_that_already_holds_an_import_is_refused(
        self, target_path: Path
    ):
        first_source = _tree()
        await import_metadata(
            first_source, target_path, source_descriptor=_SOURCE_DESCRIPTOR
        )

        with pytest.raises(TargetPathExists):
            await import_metadata(
                _tree(), target_path, source_descriptor=_SOURCE_DESCRIPTOR
            )

        # Refused before any mutation: the original import is untouched.
        target = await _reopen(target_path)
        assert reconcile(first_source, target).equal is True

    async def test_an_empty_pre_existing_file_is_refused_without_ever_being_opened(
        self, target_path: Path
    ):
        target_path.write_bytes(b"")

        with pytest.raises(TargetPathExists):
            await import_metadata(
                _tree(), target_path, source_descriptor=_SOURCE_DESCRIPTOR
            )

        # Never opened: an empty file is not turned into an empty SQLite
        # database and accepted as a fresh target.
        assert target_path.read_bytes() == b""


class TestPreflightRejectsAmbiguousSource:
    async def test_a_source_with_a_name_collision_is_refused_before_any_target_exists(
        self, target_path: Path
    ):
        # Built by hand rather than through `create_dir`, which would itself
        # refuse the collision - this stands in for a source that is not
        # internally consistent, which the import must refuse before ever
        # touching the target.
        root = TGFSDirectory.root_dir()
        root.children.append(TGFSDirectory(name="dup", parent=root))
        root.children.append(TGFSDirectory(name="dup", parent=root))

        with pytest.raises(MalformedSource):
            await import_metadata(
                root, target_path, source_descriptor=_SOURCE_DESCRIPTOR
            )

        assert not target_path.exists()


class TestDurableFailureIsReportedClearly:
    async def test_a_store_level_failure_mid_import_does_not_claim_success(
        self, target_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        original_accept = SqliteCommandStore.accept
        calls = {"n": 0}

        async def _flaky_accept(self, command):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("simulated infrastructure failure")
            return await original_accept(self, command)

        monkeypatch.setattr(SqliteCommandStore, "accept", _flaky_accept)

        with pytest.raises(ImportFailed) as exc_info:
            await import_metadata(
                _tree(), target_path, source_descriptor=_SOURCE_DESCRIPTOR
            )

        assert str(target_path) in str(exc_info.value)

        # The failed target is preserved, not deleted - and reopening it
        # never shows a complete, falsely-reconciled projection of the
        # source: there is no ImportResult, and the partial data on disk
        # genuinely does not match.
        assert target_path.exists()
        target = await _reopen(target_path)
        assert reconcile(_tree(), target).equal is False


class TestDeterministicReimport:
    async def test_the_same_source_imported_into_two_fresh_targets_agrees(
        self, tmp_path: Path
    ):
        target_a = tmp_path / "a.sqlite3"
        target_b = tmp_path / "b.sqlite3"

        result_a = await import_metadata(
            _tree(), target_a, source_descriptor=_SOURCE_DESCRIPTOR
        )
        result_b = await import_metadata(
            _tree(), target_b, source_descriptor=_SOURCE_DESCRIPTOR
        )

        assert result_a.source_dir_count == result_b.source_dir_count
        assert result_a.source_file_count == result_b.source_file_count
        assert result_a.target_dir_count == result_b.target_dir_count
        assert result_a.target_file_count == result_b.target_file_count
        assert result_a.reconciliation.equal is True
        assert result_b.reconciliation.equal is True

        tree_a = await _reopen(target_a)
        tree_b = await _reopen(target_b)
        assert reconcile(tree_a, tree_b).equal is True


class TestIntegrityAfterImportAndReopen:
    async def test_integrity_and_durability_settings_hold(self, target_path: Path):
        await import_metadata(
            _tree(), target_path, source_descriptor=_SOURCE_DESCRIPTOR
        )

        store = SqliteCommandStore(target_path)
        await store.open()
        try:
            assert store.pragma("integrity_check") == "ok"
            assert store.pragma("foreign_key_check") is None
            assert store.pragma("journal_mode") == "wal"
            assert store.pragma("synchronous") == 2
            assert store.pragma("foreign_keys") == 1
        finally:
            await store.close()
