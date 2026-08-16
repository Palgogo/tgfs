"""The narrow boundary between a GitHub-backed metadata source and an import.

`GithubSourceBoundary` is handed only a bound `get` method, never a live
`GithubRepoMetadataRepository` - this proves that shape with a fake standing
in for one, whose every other method raises if the boundary (or anything it
hands a tree to) ever calls it. No network, no PyGithub, no credentials.
"""

from pathlib import Path

from tgfs.core.metadata_import.importer import import_metadata
from tgfs.core.metadata_import.provenance import SourceDescriptor
from tgfs.core.metadata_import.source import GithubSourceBoundary
from tgfs.core.model import TGFSDirectory, TGFSFileRef, TGFSMetadata

_SOURCE_DESCRIPTOR = SourceDescriptor(repository="octo/demo", ref="deadbeef")


class _MutationForbiddenDirectory(TGFSDirectory):
    """Raises if asked to mutate - the shape a `GithubDirectory` node has."""

    def create_dir(self, name, dir_to_copy=None):
        raise AssertionError("the boundary must never create directories")

    def create_file_ref(self, name, fd_message_id):
        raise AssertionError("the boundary must never create file refs")

    def delete(self):
        raise AssertionError("the boundary must never delete")

    def delete_file_ref(self, fr):
        raise AssertionError("the boundary must never delete file refs")


def _mutation_forbidden_tree() -> _MutationForbiddenDirectory:
    root = _MutationForbiddenDirectory(name="root", parent=None)
    docs = _MutationForbiddenDirectory(name="docs", parent=root)
    root.children.append(docs)
    docs.files.append(TGFSFileRef(message_id=11, name="notes.txt", location=docs))
    return root


class _FakeGithubMetadataRepository:
    """Stands in for `GithubRepoMetadataRepository`: `get()` is the only read
    a real client offers, and `push()` is where a real mutation would go."""

    def __init__(self, tree: TGFSDirectory):
        self._tree = tree
        self.get_calls = 0

    async def get(self) -> TGFSMetadata:
        self.get_calls += 1
        return TGFSMetadata(dir=self._tree)

    async def push(self) -> None:
        raise AssertionError("the boundary must never push to the source")


class TestLoadTree:
    async def test_the_loader_is_awaited_exactly_once(self):
        tree = _mutation_forbidden_tree()
        fake_repo = _FakeGithubMetadataRepository(tree)
        boundary = GithubSourceBoundary(
            descriptor=_SOURCE_DESCRIPTOR, get=fake_repo.get
        )

        loaded = await boundary.load_tree()

        assert loaded is tree
        assert fake_repo.get_calls == 1


class TestImportThroughTheBoundary:
    async def test_import_never_mutates_a_tree_loaded_through_the_boundary(
        self, target_path: Path
    ):
        tree = _mutation_forbidden_tree()
        fake_repo = _FakeGithubMetadataRepository(tree)
        boundary = GithubSourceBoundary(
            descriptor=_SOURCE_DESCRIPTOR, get=fake_repo.get
        )

        source = await boundary.load_tree()
        result = await import_metadata(
            source, target_path, source_descriptor=_SOURCE_DESCRIPTOR
        )

        assert fake_repo.get_calls == 1
        assert result.reconciliation.equal is True
