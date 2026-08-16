"""`read_outbox`'s `limit` must be a positive, finite bound.

SQLite's own `LIMIT` treats a negative value as "no limit" rather than as
"nothing" - passed through unchecked, a negative or zero limit here would
silently turn a bounded page read into an unbounded scan of the outbox.
"""

import pytest


class TestReadOutboxLimitValidation:
    async def test_a_zero_limit_is_rejected(self, store, make_dir) -> None:
        await make_dir("a")

        with pytest.raises(ValueError):
            await store.read_outbox(after=0, limit=0)

    async def test_a_negative_limit_is_rejected_rather_than_reading_everything(
        self, store, make_dir
    ) -> None:
        await make_dir("a")
        await make_dir("b")

        with pytest.raises(ValueError):
            await store.read_outbox(after=0, limit=-1)
