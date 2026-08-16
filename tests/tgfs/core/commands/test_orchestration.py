"""The ordering contract every command repository has to keep.

The whole point of these tests is what happens between the durable write and the
in-memory view: which one goes first, and what is true after either of them
fails. They run against a fake store so a failure can be injected at exactly the
step being described - a real backend cannot be asked to fail on the third call.

The fake is not a stand-in for backend support. Adapting the pinned-message and
GitHub metadata repositories to accept commands is separate work: until then
`IMetaDataRepository` stays as it is (see TestTheExistingRepositoryInterface).
"""

import dataclasses
from typing import Optional

import pytest

from tgfs.core.commands import (
    ROOT_NODE_ID,
    AppliedCommand,
    CommandRepository,
    CreateDir,
    MutationCommand,
    NodeKind,
    NodeSnapshot,
    Projection,
    Readiness,
    new_node_id,
    new_operation_id,
)
from tgfs.core.repository.interface import IMetaDataRepository
from tgfs.errors import (
    MetadataNotInitialized,
    ProjectionPatchError,
    ProjectionStaleError,
)


class DurableFailure(Exception):
    """What a store raises when it could not write the command down at all."""


class FakeDurableStore:
    """A durable store that records what it was asked and can be told to fail.

    It keeps its accepted revisions forever: that is what lets a test show that
    a durable acceptance survives a projection that could not follow it.
    """

    def __init__(self, fail_on: Optional[int] = None, unappliable_from: int = 0):
        self.accepted: list[AppliedCommand] = []
        self.commands: list[MutationCommand] = []
        self.events: list[str] = []
        self.attempts = 0
        self._fail_on = fail_on
        self._unappliable_from = unappliable_from

    async def load(self) -> Projection:
        return Projection.empty()

    async def accept(self, command: MutationCommand) -> AppliedCommand:
        self.events.append("accept")
        self.attempts += 1
        if self._fail_on == self.attempts:
            raise DurableFailure("the store could not write this down")

        self.commands.append(command)
        revision = len(self.accepted) + 1
        # A change set the projection cannot apply: the parent is a node the
        # store knows about and the in-memory view has never seen.
        parent = (
            new_node_id()
            if self._unappliable_from and revision >= self._unappliable_from
            else ROOT_NODE_ID
        )
        applied = AppliedCommand(
            revision=revision,
            operation_id=command.operation_id,
            created=(
                NodeSnapshot(
                    node_id=getattr(command, "node_id", new_node_id()),
                    parent_id=parent,
                    name=getattr(command, "name", "node"),
                    kind=NodeKind.DIRECTORY,
                ),
            ),
        )
        self.accepted.append(applied)
        return applied


def a_create(name="documents") -> CreateDir:
    return CreateDir(
        operation_id=new_operation_id(),
        parent_id=ROOT_NODE_ID,
        name=name,
        node_id=new_node_id(),
    )


async def a_ready_repository(store: FakeDurableStore, **kwargs) -> CommandRepository:
    repository = CommandRepository(store, **kwargs)
    await repository.init()
    return repository


class TestBeforeItIsReady:
    async def test_refuses_to_apply_anything_until_it_is_initialized(self):
        store = FakeDurableStore()
        repository = CommandRepository(store)

        assert repository.readiness is Readiness.NOT_READY

        with pytest.raises(MetadataNotInitialized):
            await repository.apply(a_create())

        # The readiness check comes first, so nothing was written down either.
        assert store.events == []
        assert store.accepted == []

    async def test_refuses_to_be_read_until_it_is_initialized(self):
        with pytest.raises(MetadataNotInitialized):
            CommandRepository(FakeDurableStore()).projection()

    async def test_init_loads_the_projection_from_the_store(self):
        repository = await a_ready_repository(FakeDurableStore())

        assert repository.readiness is Readiness.READY
        assert repository.projection().revision == 0


class TestASuccessfulApply:
    async def test_writes_the_command_down_before_it_touches_the_projection(self):
        store = FakeDurableStore()
        events: list[str] = []

        def recording_patch(projection, applied):
            events.append("patch")
            from tgfs.core.commands import patch_projection

            return patch_projection(projection, applied)

        store.events = events
        repository = await a_ready_repository(store, patcher=recording_patch)

        await repository.apply(a_create())

        assert events == ["accept", "patch"]

    async def test_accepts_once_and_patches_once(self):
        store = FakeDurableStore()
        patches: list[AppliedCommand] = []
        repository = await a_ready_repository(
            store,
            patcher=lambda projection, applied: (
                patches.append(applied) or _patched(projection, applied)
            ),
        )
        command = a_create()

        await repository.apply(command)

        assert store.commands == [command]
        assert len(store.accepted) == 1
        assert patches == store.accepted

    async def test_returns_the_durable_record_and_advances_the_projection(self):
        repository = await a_ready_repository(FakeDurableStore())
        command = a_create()

        applied = await repository.apply(command)

        assert applied.revision == 1
        assert applied.operation_id == command.operation_id
        assert repository.projection().revision == 1
        assert repository.projection().contains(command.node_id)

    async def test_returns_a_value_the_caller_cannot_change(self):
        repository = await a_ready_repository(FakeDurableStore())

        applied = await repository.apply(a_create())

        with pytest.raises(dataclasses.FrozenInstanceError):
            applied.revision = 99  # type: ignore[misc]

    async def test_publishes_the_revision_it_accepted(self):
        published: list[int] = []
        repository = await a_ready_repository(
            FakeDurableStore(), on_revision=published.append
        )

        await repository.apply(a_create())
        await repository.apply(a_create(name="pictures"))

        assert published == [1, 2]


class TestWhenTheStoreRefusesToWrite:
    async def test_the_failure_reaches_the_caller_unchanged(self):
        repository = await a_ready_repository(FakeDurableStore(fail_on=1))

        with pytest.raises(DurableFailure):
            await repository.apply(a_create())

    async def test_the_projection_is_left_exactly_as_it_was(self):
        store = FakeDurableStore(fail_on=2)
        patches: list[AppliedCommand] = []
        repository = await a_ready_repository(
            store,
            patcher=lambda projection, applied: (
                patches.append(applied) or _patched(projection, applied)
            ),
        )
        await repository.apply(a_create())
        before = repository.projection()

        with pytest.raises(DurableFailure):
            await repository.apply(a_create(name="pictures"))

        assert repository.projection() is before
        assert len(patches) == 1

    async def test_the_repository_stays_usable(self):
        published: list[int] = []
        repository = await a_ready_repository(
            FakeDurableStore(fail_on=1), on_revision=published.append
        )

        with pytest.raises(DurableFailure):
            await repository.apply(a_create())

        assert repository.readiness is Readiness.READY
        assert published == []

        applied = await repository.apply(a_create(name="pictures"))
        assert applied.revision == 1


class TestWhenTheProjectionCannotFollowAnAcceptedWrite:
    async def a_stale_repository(self, published=None) -> tuple:
        store = FakeDurableStore(unappliable_from=1)
        repository = await a_ready_repository(
            store, on_revision=(published.append if published is not None else None)
        )

        with pytest.raises(ProjectionStaleError) as raised:
            await repository.apply(a_create())

        return repository, store, raised.value

    async def test_it_says_so_explicitly_rather_than_reporting_success(self):
        _, _, error = await self.a_stale_repository()

        assert isinstance(error, ProjectionStaleError)
        assert error.revision == 1
        assert isinstance(error.__cause__, ProjectionPatchError)

    async def test_the_durable_acceptance_is_not_rolled_back(self):
        # The store said yes and there is no undo: pretending otherwise would
        # lose a write that is already on disk.
        _, store, _ = await self.a_stale_repository()

        assert len(store.accepted) == 1
        assert store.accepted[0].revision == 1
        assert store.events == ["accept"]

    async def test_the_repository_goes_stale(self):
        repository, _, _ = await self.a_stale_repository()

        assert repository.readiness is Readiness.STALE

    async def test_it_does_not_publish_a_revision_it_could_not_apply(self):
        published: list[int] = []
        await self.a_stale_repository(published=published)

        assert published == []

    async def test_every_later_write_is_refused_without_reaching_the_store(self):
        repository, store, _ = await self.a_stale_repository()

        with pytest.raises(ProjectionStaleError):
            await repository.apply(a_create(name="pictures"))

        assert len(store.accepted) == 1
        assert store.events == ["accept"]

    async def test_every_later_read_is_refused(self):
        repository, _, _ = await self.a_stale_repository()

        with pytest.raises(ProjectionStaleError):
            repository.projection()

    async def test_it_stays_stale_even_if_asked_to_initialize_again(self):
        # Re-reading the store is how this is recovered from, but a repository
        # that has served a diverged view does not get to decide it is fine now.
        repository, _, _ = await self.a_stale_repository()

        with pytest.raises(ProjectionStaleError):
            await repository.init()

        assert repository.readiness is Readiness.STALE


class TestTheExistingRepositoryInterface:
    def test_has_not_been_given_a_default_apply(self):
        # IMetaDataRepository is implemented by backends that know nothing about
        # commands. Giving it an `apply` that pushed whole-tree metadata would
        # make every one of them look like it kept the ordering contract above
        # while keeping none of it. The adapters are the next piece of work.
        assert not hasattr(IMetaDataRepository, "apply")


def _patched(projection, applied):
    from tgfs.core.commands import patch_projection

    return patch_projection(projection, applied)
