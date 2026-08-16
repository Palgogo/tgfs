"""Applying a command in the one order that survives a crash.

Durable first, projection second. Every apply runs the same four steps:

    ready? -> the store accepts the command -> an AppliedCommand ->
    the projection is patched -> the revision is published

The order is the whole design. If the store refuses, nothing happened and the
repository carries on. If the store accepted and the projection then could not
follow, the write is on disk and only the in-memory view is wrong - so the view
is declared stale and this repository stops answering, rather than serving
reads that silently omit an accepted write. Nothing is ever rolled back out of
the store to make memory look right.

`IMetaDataRepository` is deliberately untouched. Its implementations push a
whole serialized tree and know nothing about revisions; handing them an `apply`
would give them the shape of this contract without any of its guarantees. This
protocol is where a command-aware backend will be plugged in, and adapting the
existing backends to it is the next piece of work.
"""

from enum import Enum
from typing import Callable, Optional, Protocol, runtime_checkable

from tgfs.errors import (
    MetadataNotInitialized,
    ProjectionPatchError,
    ProjectionStaleError,
)

from .applied import AppliedCommand
from .commands import MutationCommand
from .projection import Projection, patch_projection


class Readiness(Enum):
    NOT_READY = "NOT_READY"
    READY = "READY"
    # Terminal. A stale repository cannot recover on its own: only something
    # that can re-read the store knows what it should have been holding.
    STALE = "STALE"


@runtime_checkable
class IDurableCommandStore(Protocol):
    """The durable side: the only thing that decides a write happened."""

    async def load(self) -> Projection:
        """The projection as the store currently has it."""
        ...

    async def accept(self, command: MutationCommand) -> AppliedCommand:
        """Write the command down and report what became true.

        Returning is the acceptance. A store that raises has written nothing.
        """
        ...


@runtime_checkable
class ICommandMetaDataRepository(Protocol):
    """What a caller sees: a readiness, a read view and `apply`."""

    @property
    def readiness(self) -> Readiness: ...

    def projection(self) -> Projection: ...

    async def apply(self, command: MutationCommand) -> AppliedCommand: ...


Patcher = Callable[[Projection, AppliedCommand], Projection]


class CommandRepository:
    """Sequences one durable store and one projection.

    Holds no tree of its own - the projection is a value it replaces - so there
    is no half-applied state to unwind when a step fails.
    """

    def __init__(
        self,
        store: IDurableCommandStore,
        patcher: Patcher = patch_projection,
        on_revision: Optional[Callable[[int], None]] = None,
    ):
        self._store = store
        # A seam, so a test can watch or fail the patch step at exactly the
        # point being described. The default is the pure patcher.
        self._patch = patcher
        self._on_revision = on_revision
        self._readiness = Readiness.NOT_READY
        self._projection = Projection.empty()

    @property
    def readiness(self) -> Readiness:
        return self._readiness

    async def init(self) -> None:
        self._require_not_stale()
        self._projection = await self._store.load()
        self._readiness = Readiness.READY

    def projection(self) -> Projection:
        self._require_ready()
        return self._projection

    async def apply(self, command: MutationCommand) -> AppliedCommand:
        self._require_ready()

        # Durable first: until this returns there is nothing to project, and
        # anything that goes wrong here leaves the world as it was.
        applied = await self._store.accept(command)

        try:
            patched = self._patch(self._projection, applied)
        except ProjectionPatchError as e:
            # The store has already accepted `applied`. It stays accepted.
            self._readiness = Readiness.STALE
            raise ProjectionStaleError(applied.revision, cause=str(e)) from e

        self._projection = patched
        if self._on_revision:
            self._on_revision(applied.revision)
        return applied

    def _require_ready(self) -> None:
        self._require_not_stale()
        if self._readiness is not Readiness.READY:
            raise MetadataNotInitialized

    def _require_not_stale(self) -> None:
        if self._readiness is Readiness.STALE:
            raise ProjectionStaleError(self._projection.revision)
