"""The smallest store-facing extension a local outbox consumer needs.

Nothing here changes what a mutation does; it only lets a reader ask the
store what is in the outbox after some point, and let the store durably
remember that a sequence was acknowledged - the two facts a restart-safe,
idempotent replay needs and cannot get from raw SQL without reaching past the
store's own internals.
"""

from tgfs.core.commands import ROOT_NODE_ID, CreateDir, new_node_id, new_operation_id


class TestReadOutbox:
    async def test_reading_after_zero_returns_every_row_in_sequence_order(
        self, store, make_dir
    ) -> None:
        await make_dir("a")
        await make_dir("b")

        entries = await store.read_outbox(after=0, limit=10)

        assert [e.sequence for e in entries] == [1, 2]

    async def test_reading_after_a_sequence_returns_only_what_comes_next(
        self, store, make_dir
    ) -> None:
        await make_dir("a")
        await make_dir("b")
        await make_dir("c")

        entries = await store.read_outbox(after=1, limit=10)

        assert [e.sequence for e in entries] == [2, 3]

    async def test_reading_is_bounded_by_limit(self, store, make_dir) -> None:
        await make_dir("a")
        await make_dir("b")
        await make_dir("c")

        entries = await store.read_outbox(after=0, limit=2)

        assert [e.sequence for e in entries] == [1, 2]

    async def test_an_entry_carries_its_operation_id_and_command_json(
        self, store
    ) -> None:
        operation_id = new_operation_id()
        await store.accept(
            CreateDir(
                operation_id=operation_id,
                parent_id=ROOT_NODE_ID,
                name="docs",
                node_id=new_node_id(),
            )
        )

        entries = await store.read_outbox(after=0, limit=10)

        assert entries[0].operation_id == operation_id
        assert "docs" in entries[0].command_json

    async def test_an_unacknowledged_entry_reports_as_such(
        self, store, make_dir
    ) -> None:
        await make_dir("a")

        entries = await store.read_outbox(after=0, limit=10)

        assert entries[0].acknowledged is False


class TestAcknowledgeOutbox:
    async def test_an_acknowledged_sequence_reports_as_acknowledged(
        self, store, make_dir
    ) -> None:
        await make_dir("a")

        await store.acknowledge_outbox(1)
        entries = await store.read_outbox(after=0, limit=10)

        assert entries[0].acknowledged is True

    async def test_acknowledging_twice_does_not_error(self, store, make_dir) -> None:
        await make_dir("a")

        await store.acknowledge_outbox(1)
        await store.acknowledge_outbox(1)

        entries = await store.read_outbox(after=0, limit=10)
        assert entries[0].acknowledged is True

    async def test_acknowledgement_survives_a_reopen(
        self, store, make_dir, open_store
    ) -> None:
        await make_dir("a")
        await store.acknowledge_outbox(1)
        await store.close()

        reopened = await open_store()
        entries = await reopened.read_outbox(after=0, limit=10)

        assert entries[0].acknowledged is True
