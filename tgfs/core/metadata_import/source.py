"""The one read this module is allowed to make of a GitHub-backed source.

A GitHub-backed `IMetaDataRepository` (real or fake) is handed here only as
its bound `get` method, never as the object itself - so nothing here can
reach `push`, or any other method a real client might expose, even by
accident. The loader is awaited exactly once, and what it returns is
unwrapped to the `TGFSDirectory` an import walks.
"""

from dataclasses import dataclass
from typing import Awaitable, Callable

from tgfs.core.model import TGFSDirectory, TGFSMetadata

from .provenance import SourceDescriptor

Loader = Callable[[], Awaitable[TGFSMetadata]]


@dataclass(frozen=True)
class GithubSourceBoundary:
    """Pairs an immutable source descriptor with the one read it is allowed."""

    descriptor: SourceDescriptor
    get: Loader

    async def load_tree(self) -> TGFSDirectory:
        """Await the loader exactly once and hand back the tree it read."""
        metadata = await self.get()
        return metadata.dir


__all__ = ["GithubSourceBoundary", "Loader"]
