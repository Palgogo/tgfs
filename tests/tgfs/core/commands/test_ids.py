import pytest

from tgfs.core.commands import ROOT_NODE_ID, new_node_id, parse_node_id
from tgfs.errors import InvalidNodeId


class TestNewNodeId:
    def test_generates_unique_opaque_ids(self):
        ids = {new_node_id() for _ in range(100)}

        assert len(ids) == 100
        assert all(isinstance(node_id, str) for node_id in ids)


class TestParseNodeId:
    def test_accepts_a_generated_id_unchanged(self):
        node_id = new_node_id()

        assert parse_node_id(node_id) == node_id

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "not-a-node-id",
            "/some/path",
            "42",
            "550e8400-e29b-41d4-a716-4466554400",  # one character short
            None,
            42,
            ["550e8400-e29b-41d4-a716-446655440000"],
        ],
    )
    def test_rejects_malformed_ids(self, value):
        with pytest.raises(InvalidNodeId):
            parse_node_id(value)


class TestRootNodeId:
    def test_is_a_stable_well_known_id(self):
        # The root is the one node no command can create, so its id has to be
        # known in advance and identical in every store, before and after a
        # reload.
        assert ROOT_NODE_ID == "00000000-0000-0000-0000-000000000000"
        assert parse_node_id(ROOT_NODE_ID) == ROOT_NODE_ID

    def test_is_never_minted_for_a_new_node(self):
        assert ROOT_NODE_ID not in {new_node_id() for _ in range(100)}
