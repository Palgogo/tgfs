"""A read view of the tree, and the one function allowed to move it forward.

The projection exists to be read; it is never the thing a write is applied to
first. `patch_projection` takes an AppliedCommand - a change the store has
already accepted - and returns a new projection at the next revision. It takes
no commands, keeps no state and writes nothing, so there is no path by which the
view could run ahead of the store.

This is the detached projection the ordering contract is tested against. It
deliberately mirrors nodes as flat snapshots rather than as TGFSDirectory /
TGFSFileRef: making the real model patchable is separate work, and doing it here
would tie the ordering contract to a mutable, parent-linked tree before that
contract has anywhere to run.
"""

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Optional

from tgfs.errors import ProjectionPatchError

from .applied import AppliedCommand, NodeSnapshot
from .ids import ROOT_NODE_ID, NodeId

_NO_NODES: Mapping[NodeId, NodeSnapshot] = MappingProxyType({})


@dataclass(frozen=True)
class Projection:
    """Every node except the root, by id, as of one revision.

    The root is not held as a snapshot because no command may create, move or
    delete it: it is the fixed point the rest of the tree hangs from.
    """

    revision: int = 0
    nodes: Mapping[NodeId, NodeSnapshot] = field(default=_NO_NODES)

    @classmethod
    def empty(cls) -> "Projection":
        return cls()

    def contains(self, node_id: NodeId) -> bool:
        return node_id == ROOT_NODE_ID or node_id in self.nodes

    def node(self, node_id: NodeId) -> NodeSnapshot:
        try:
            return self.nodes[node_id]
        except KeyError:
            raise ProjectionPatchError(f"{node_id!r} is not in the projection")

    def children(self, node_id: NodeId) -> tuple[NodeSnapshot, ...]:
        return tuple(n for n in self.nodes.values() if n.parent_id == node_id)


def patch_projection(projection: Projection, applied: AppliedCommand) -> Projection:
    """The projection with the accepted change applied, as a new value.

    Refuses rather than guesses. Anything it cannot apply exactly - a parent it
    has never seen, a revision that skips one - means the view and the store
    have already diverged, and the caller has to be told that rather than handed
    a projection that is quietly missing a write.
    """
    if not isinstance(projection, Projection):
        raise ProjectionPatchError(f"{type(projection).__name__} is not a projection")
    if not isinstance(applied, AppliedCommand):
        raise ProjectionPatchError(
            f"{type(applied).__name__} is not a durably accepted change"
        )
    if applied.revision != projection.revision + 1:
        raise ProjectionPatchError(
            f"revision {applied.revision} does not follow {projection.revision}"
        )

    nodes = dict(projection.nodes)

    # Deletions first: a store may retire a node and put another in its place
    # within one revision, and the creation is the part that has to survive.
    for node_id in applied.deleted:
        if node_id not in nodes:
            raise ProjectionPatchError(f"cannot delete unknown {node_id!r}")
        for gone in _subtree(nodes, node_id):
            del nodes[gone]

    for snapshot in applied.created:
        if snapshot.node_id in nodes:
            raise ProjectionPatchError(f"cannot create existing {snapshot.node_id!r}")
        _require_parent(nodes, snapshot)
        nodes[snapshot.node_id] = snapshot

    for snapshot in applied.updated:
        existing = nodes.get(snapshot.node_id)
        if existing is None:
            raise ProjectionPatchError(f"cannot update unknown {snapshot.node_id!r}")
        if existing.kind is not snapshot.kind:
            raise ProjectionPatchError(
                f"{snapshot.node_id!r} is a {existing.kind.name}, "
                f"not a {snapshot.kind.name}"
            )
        _require_parent(nodes, snapshot)
        if snapshot.parent_id in _subtree(nodes, snapshot.node_id):
            raise ProjectionPatchError(f"cannot move {snapshot.node_id!r} below itself")
        nodes[snapshot.node_id] = snapshot

    return Projection(revision=applied.revision, nodes=MappingProxyType(nodes))


def _require_parent(
    nodes: Mapping[NodeId, NodeSnapshot], snapshot: NodeSnapshot
) -> None:
    # Created snapshots are read in order, so a copied subtree resolves as long
    # as it names parents before their children - which is what the store knows
    # and the projection does not.
    if snapshot.parent_id != ROOT_NODE_ID and snapshot.parent_id not in nodes:
        raise ProjectionPatchError(
            f"{snapshot.node_id!r} hangs from unknown parent {snapshot.parent_id!r}"
        )


def _subtree(
    nodes: Mapping[NodeId, NodeSnapshot], root: NodeId
) -> frozenset[NodeId]:
    found = {root}
    frontier: Optional[set[NodeId]] = {root}
    while frontier:
        frontier = {
            node_id
            for node_id, snapshot in nodes.items()
            if snapshot.parent_id in frontier and node_id not in found
        }
        found |= frontier
    return frozenset(found)
