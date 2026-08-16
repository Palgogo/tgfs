"""A durable command store backed by one SQLite file.

This is the `IDurableCommandStore` side of the ordering contract: the thing that
decides a write happened. Every mutation is validated against the database
inside the same write transaction that records it, so what a command was checked
against and what it was applied to cannot drift apart between the two.

One transaction per accepted command carries three things: the new state of the
namespace, the operation record that makes a retry idempotent, and exactly one
outbox row for whoever publishes changes onward. Either all three are on disk or
none of them are; there is no window in which a node exists that nothing will
ever be told about.

Nothing here is wired into the running application yet - no configuration reads
it, no repository is built from it. It is opened by a caller that names a file.
"""

import asyncio
import dataclasses
import json
import sqlite3
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Optional, Union

from tgfs.core.commands import (
    ROOT_NODE_ID,
    AppliedCommand,
    ClearRoot,
    CopySubtree,
    CreateDir,
    CreateFileRef,
    DeleteNode,
    MoveNode,
    MutationCommand,
    NodeId,
    NodeKind,
    NodeSnapshot,
    Projection,
    RelinkFileRef,
    applied_command_from_dict,
    new_node_id,
)
from tgfs.errors import (
    DurableStoreError,
    FileOrDirectoryAlreadyExists,
    FileOrDirectoryDoesNotExist,
    InvalidCommandPayload,
    IsADirectory,
    MetadataNotInitialized,
    NotADirectory,
    NothingToApply,
    OperationIdConflict,
)

from .codec import (
    canonical_json,
    command_to_dict,
    command_type,
    operation_id,
    payload_hash,
)
from .rows import snapshot_from_row
from .schema import migrate


class SqliteCommandStore:
    """The durable store, as one file.

    Opened explicitly and closed explicitly. All database work happens on a
    worker thread under one lock: SQLite calls are blocking, and serialising
    them here means a single writer, which is what the file wants anyway.
    """

    def __init__(
        self,
        path: Union[str, Path],
        *,
        before_commit: Optional[Callable[[], None]] = None,
    ):
        self._path = Path(path)
        # A seam, so a test can make the process fail at the one instant that
        # matters: after everything has been written and before it is durable.
        self._before_commit = before_commit
        self._connection: Optional[sqlite3.Connection] = None
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    async def open(self) -> "SqliteCommandStore":
        async with self._lock:
            if self._connection is None:
                self._connection = await asyncio.to_thread(self._connect)
        return self

    async def close(self) -> None:
        async with self._lock:
            connection, self._connection = self._connection, None
            if connection is not None:
                await asyncio.to_thread(self._close, connection)

    async def checkpoint(self) -> None:
        """Fold the write-ahead log back into the main file."""
        connection = self._require_open()
        async with self._lock:
            await asyncio.to_thread(
                connection.execute, "PRAGMA wal_checkpoint(TRUNCATE)"
            )

    async def load(self) -> Projection:
        connection = self._require_open()
        async with self._lock:
            return await asyncio.to_thread(self._load, connection)

    async def accept(self, command: MutationCommand) -> AppliedCommand:
        connection = self._require_open()
        async with self._lock:
            return await asyncio.to_thread(self._accept, connection, command)

    def pragma(self, name: str) -> Any:
        """What this connection is actually set to.

        Durability settings like `synchronous` and `foreign_keys` are per
        connection, so the only honest way to report them is to ask the
        connection that does the writing.
        """
        connection = self._require_open()
        row = connection.execute(f"PRAGMA {name}").fetchone()
        return row[0] if row else None

    def _accept(
        self, connection: sqlite3.Connection, command: MutationCommand
    ) -> AppliedCommand:
        """One command, one transaction, on the calling worker thread.

        IMMEDIATE rather than deferred: the validation reads below decide
        whether a write is allowed, so the write lock has to be held before
        them, not taken half way through once the answer is already stale.
        """
        _guard(connection, "BEGIN IMMEDIATE")
        try:
            already = _replayed(connection, command)
            if already is not None:
                # Read only, so there is nothing to keep. Ending the
                # transaction here also means a retry cannot hold the write
                # lock for as long as a real mutation would.
                _guard(connection, "ROLLBACK")
                return already

            applied = self._apply(connection, command)
            self._record(connection, command, applied)
            if self._before_commit is not None:
                self._before_commit()
            _guard(connection, "COMMIT")
            return applied
        except BaseException:
            # Nothing partial survives: either the command is on disk whole or
            # it never happened, and the caller is told by the exception.
            connection.execute("ROLLBACK")
            raise

    def _apply(
        self, connection: sqlite3.Connection, command: MutationCommand
    ) -> AppliedCommand:
        revision = self._revision(connection) + 1
        created: tuple[NodeSnapshot, ...] = ()
        updated: tuple[NodeSnapshot, ...] = ()
        deleted: tuple[NodeId, ...] = ()

        match command:
            case CreateDir():
                created = (
                    _new_node(
                        connection,
                        node_id=command.node_id,
                        parent_id=command.parent_id,
                        name=command.name,
                        kind=NodeKind.DIRECTORY,
                    ),
                )
            case CreateFileRef():
                created = (
                    _new_node(
                        connection,
                        node_id=command.node_id,
                        parent_id=command.parent_id,
                        name=command.name,
                        kind=NodeKind.FILE_REF,
                        message_id=command.message_id,
                    ),
                )
            case RelinkFileRef():
                updated = (_relinked(connection, command),)
            case CopySubtree():
                created = _copy_subtree(connection, command)
            case MoveNode():
                updated = (_moved(connection, command),)
            case DeleteNode():
                _require_node(connection, command.node_id)
                deleted = (command.node_id,)
            case ClearRoot():
                deleted = tuple(
                    NodeId(row["node_id"])
                    for row in _guard(
                        connection,
                        "SELECT node_id FROM nodes WHERE parent_id = ?",
                        (ROOT_NODE_ID,),
                    )
                )
                if not deleted:
                    raise NothingToApply("the root is already empty")
            case _:
                raise InvalidCommandPayload(
                    f"{type(command).__name__} is not a command this store applies"
                )

        for snapshot in created:
            _insert_node(connection, snapshot)
        for snapshot in updated:
            _update_node(connection, snapshot)
        for node_id in deleted:
            _delete_subtree(connection, node_id)

        return AppliedCommand(
            revision=revision,
            operation_id=operation_id(command),
            created=created,
            updated=updated,
            deleted=deleted,
        )

    @staticmethod
    def _record(
        connection: sqlite3.Connection,
        command: MutationCommand,
        applied: AppliedCommand,
    ) -> None:
        """The operation and its outbox row, in the transaction that made it true.

        Both are written here rather than by a later step on purpose: an
        operation record written afterwards would leave a window in which a
        retry re-applies a mutation that already happened, and an outbox row
        written afterwards would leave a change nobody is ever told about.
        """
        applied_json = canonical_json(applied.to_dict())
        command_json = canonical_json(command_to_dict(command))

        connection.execute(
            "INSERT INTO operations "
            "(operation_id, revision, command_type, payload_hash, applied_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                operation_id(command),
                applied.revision,
                command_type(command),
                payload_hash(command),
                applied_json,
            ),
        )
        connection.execute(
            "INSERT INTO outbox (operation_id, revision, command_json, applied_json) "
            "VALUES (?, ?, ?, ?)",
            (operation_id(command), applied.revision, command_json, applied_json),
        )

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # isolation_level=None: transactions are opened by hand, because
            # where one begins and ends is the whole guarantee and is not
            # something to leave to a driver's heuristics.
            connection = sqlite3.connect(
                self._path, isolation_level=None, check_same_thread=False
            )
            connection.row_factory = sqlite3.Row
            # Readers never block the writer and the file survives a crash
            # mid-write...
            connection.execute("PRAGMA journal_mode=WAL")
            # ...and a commit does not return until the disk says so.
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            migrate(connection)
            return connection
        except sqlite3.Error as e:
            raise DurableStoreError(f"cannot open {self._path}", cause=str(e)) from e

    @staticmethod
    def _close(connection: sqlite3.Connection) -> None:
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            # Closing is best effort: the data is already durable, the log is
            # only a detail of how it is stored.
            pass
        connection.close()

    def _load(self, connection: sqlite3.Connection) -> Projection:
        nodes: dict[NodeId, NodeSnapshot] = {}
        for row in connection.execute(
            "SELECT node_id, parent_id, name, kind, message_id "
            "FROM nodes WHERE node_id != ?",
            (ROOT_NODE_ID,),
        ):
            snapshot = snapshot_from_row(row)
            nodes[snapshot.node_id] = snapshot

        return Projection(revision=self._revision(connection), nodes=MappingProxyType(nodes))

    @staticmethod
    def _revision(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(revision), 0) FROM operations"
        ).fetchone()
        return row[0]

    def _require_open(self) -> sqlite3.Connection:
        if self._connection is None:
            raise MetadataNotInitialized()
        return self._connection


def _guard(connection: sqlite3.Connection, sql: str, params: tuple = ()) -> Any:
    """Run one statement, and let no sqlite exception out.

    Anything SQLite has to say about a statement means the same thing to a
    caller - the write did not happen - and saying it in the backend's own
    exception type would make every caller import sqlite3 to find that out.
    """
    try:
        return connection.execute(sql, params)
    except sqlite3.Error as e:
        raise DurableStoreError(_detail(e), cause=str(e)) from e


def _detail(error: sqlite3.Error) -> str:
    return f"{type(error).__name__.removesuffix('Error').lower()} while writing"


def _new_node(
    connection: sqlite3.Connection,
    *,
    node_id: NodeId,
    parent_id: NodeId,
    name: str,
    kind: NodeKind,
    message_id: Optional[int] = None,
) -> NodeSnapshot:
    """The snapshot of a node this store is prepared to create.

    The checks are here, ahead of the INSERT, rather than left to the file's own
    constraints. The constraints stay - they are what makes the rule true for
    every writer - but reaching them means the store has already lost the
    information a caller needs: which node was in the way, and whether that is
    the caller's mistake or a broken database.
    """
    _require_directory(connection, parent_id)
    _require_absent(connection, node_id)
    _require_name_free(connection, parent_id, name)

    return NodeSnapshot(
        node_id=node_id,
        parent_id=parent_id,
        name=name,
        kind=kind,
        message_id=message_id,
    )


def _replayed(
    connection: sqlite3.Connection, command: MutationCommand
) -> Optional[AppliedCommand]:
    """What this operation was answered the first time, if it already was.

    The record is compared by payload hash rather than by re-running the
    command: the tree has moved on since, so applying it again would either
    fail or - worse - succeed differently. What the caller is owed is the
    answer it did not hear, which is the one that was written down.
    """
    row = _guard(
        connection,
        "SELECT payload_hash, applied_json FROM operations WHERE operation_id = ?",
        (operation_id(command),),
    ).fetchone()
    if row is None:
        return None

    if row["payload_hash"] != payload_hash(command):
        raise OperationIdConflict(operation_id(command))

    try:
        return applied_command_from_dict(json.loads(row["applied_json"]))
    except (ValueError, InvalidCommandPayload) as e:
        # The record was written by this store and cannot be read by it. Saying
        # so is the only honest answer: re-applying would double the mutation.
        raise DurableStoreError(
            f"the record of operation {operation_id(command)} cannot be read back",
            cause=str(e),
        ) from e


def _relinked(connection: sqlite3.Connection, command: RelinkFileRef) -> NodeSnapshot:
    """The file ref pointing at its new message, or the reason it cannot.

    A relink to the message the ref already holds is refused for the same reason
    a move to where a node already is refused: the revision it would take says
    nothing became true, and every reader of the outbox would be handed a change
    to apply that changes nothing.
    """
    node = _require_file_ref(connection, command.node_id)
    if node.message_id == command.message_id:
        raise NothingToApply(
            f"{command.node_id!r} already points at message {command.message_id}"
        )

    return dataclasses.replace(node, message_id=command.message_id)


def _moved(connection: sqlite3.Connection, command: MoveNode) -> NodeSnapshot:
    """The moved node as it will be, or the reason it cannot be.

    Only this one node is touched. Its subtree follows because every child
    still points at an id that did not change, which is also why a move must
    not land inside its own subtree: the nodes would still be in the file, all
    pointing at each other, reachable from the root by nothing.
    """
    node = snapshot_from_row(_require_node(connection, command.node_id))
    if node.parent_id == command.new_parent_id and node.name == command.new_name:
        raise NothingToApply(f"{command.node_id!r} is already there under that name")

    _require_directory(connection, command.new_parent_id)
    if _is_below(connection, command.new_parent_id, command.node_id):
        raise InvalidCommandPayload(
            f"{command.node_id!r} cannot be moved below itself"
        )
    if node.name != command.new_name or node.parent_id != command.new_parent_id:
        _require_name_free(connection, command.new_parent_id, command.new_name)

    return dataclasses.replace(
        node, parent_id=command.new_parent_id, name=command.new_name
    )


# Walks up from a node rather than down from an ancestor: the question is
# whether one node sits below another, and the path to the root is bounded by
# the depth of the tree where the subtree below it is not.
_ANCESTRY = """
    WITH RECURSIVE ancestry (node_id, parent_id) AS (
        SELECT node_id, parent_id FROM nodes WHERE node_id = ?
        UNION ALL
        SELECT n.node_id, n.parent_id FROM nodes n
        JOIN ancestry ON n.node_id = ancestry.parent_id
    )
    SELECT 1 FROM ancestry WHERE node_id = ? LIMIT 1
"""


def _is_below(
    connection: sqlite3.Connection, node_id: NodeId, ancestor_id: NodeId
) -> bool:
    """Whether `node_id` is `ancestor_id` or sits somewhere under it."""
    return (
        _guard(connection, _ANCESTRY, (node_id, ancestor_id)).fetchone() is not None
    )


def _node_row(connection: sqlite3.Connection, node_id: NodeId) -> Optional[sqlite3.Row]:
    return _guard(
        connection,
        "SELECT node_id, parent_id, name, kind, message_id FROM nodes WHERE node_id = ?",
        (node_id,),
    ).fetchone()


def _require_node(connection: sqlite3.Connection, node_id: NodeId) -> sqlite3.Row:
    row = _node_row(connection, node_id)
    if row is None:
        raise FileOrDirectoryDoesNotExist(node_id)
    return row


def _require_directory(connection: sqlite3.Connection, node_id: NodeId) -> sqlite3.Row:
    row = _require_node(connection, node_id)
    if row["kind"] != NodeKind.DIRECTORY.value:
        raise NotADirectory(node_id)
    return row


def _require_absent(connection: sqlite3.Connection, node_id: NodeId) -> None:
    if _node_row(connection, node_id) is not None:
        raise FileOrDirectoryAlreadyExists(node_id)


def _require_name_free(
    connection: sqlite3.Connection, parent_id: NodeId, name: str
) -> None:
    taken = _guard(
        connection,
        "SELECT node_id FROM nodes WHERE parent_id = ? AND name = ?",
        (parent_id, name),
    ).fetchone()
    if taken is not None:
        raise FileOrDirectoryAlreadyExists(name)


def _require_file_ref(
    connection: sqlite3.Connection, node_id: NodeId
) -> NodeSnapshot:
    row = _require_node(connection, node_id)
    if row["kind"] != NodeKind.FILE_REF.value:
        raise IsADirectory(node_id)
    return snapshot_from_row(row)


def _insert_node(connection: sqlite3.Connection, snapshot: NodeSnapshot) -> None:
    _guard(
        connection,
        "INSERT INTO nodes (node_id, parent_id, name, kind, message_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            snapshot.node_id,
            snapshot.parent_id,
            snapshot.name,
            snapshot.kind.value,
            snapshot.message_id,
        ),
    )


_DELETE_SUBTREE = """
    WITH RECURSIVE subtree (node_id) AS (
        SELECT node_id FROM nodes WHERE node_id = ?
        UNION ALL
        SELECT nodes.node_id FROM nodes
        JOIN subtree ON nodes.parent_id = subtree.node_id
    )
    DELETE FROM nodes WHERE node_id IN (SELECT node_id FROM subtree)
"""

# Depth comes back with each row so the copy can be written parents first: the
# reader on the other side places a node under a parent it has already seen.
_DESCENDANTS = """
    WITH RECURSIVE subtree (node_id, parent_id, name, kind, message_id, depth) AS (
        SELECT node_id, parent_id, name, kind, message_id, 0
        FROM nodes WHERE node_id = ?
        UNION ALL
        SELECT n.node_id, n.parent_id, n.name, n.kind, n.message_id, subtree.depth + 1
        FROM nodes n JOIN subtree ON n.parent_id = subtree.node_id
    )
    SELECT node_id, parent_id, name, kind, message_id FROM subtree
    WHERE depth > 0 ORDER BY depth
"""


def _copy_subtree(
    connection: sqlite3.Connection, command: CopySubtree
) -> tuple[NodeSnapshot, ...]:
    """The source subtree written again under fresh ids.

    The source is read once, inside this transaction, and every node written
    afterwards comes from that reading. Copying a tree into its own subtree is
    therefore finite and means what it says: the copy is of what was there when
    the command was accepted, not of what the copy is making it become.
    """
    # Refused here rather than left to the row decoder: the root carries no name
    # and no parent, so reading it as a snapshot fails on a field the caller
    # never sent, and reports a node id it never wrote.
    if command.source_id == ROOT_NODE_ID:
        raise InvalidCommandPayload(
            "the root cannot be copied: it has no name or parent to reproduce"
        )

    source = snapshot_from_row(_require_node(connection, command.source_id))
    root = _new_node(
        connection,
        node_id=command.node_id,
        parent_id=command.parent_id,
        name=command.name,
        kind=source.kind,
        message_id=source.message_id,
    )

    # Old id to new id, so a child copied later finds the copy of its parent
    # rather than the original.
    minted: dict[NodeId, NodeId] = {source.node_id: root.node_id}
    created = [root]

    for row in _guard(connection, _DESCENDANTS, (command.source_id,)).fetchall():
        original = snapshot_from_row(row)
        minted[original.node_id] = new_node_id()
        created.append(
            dataclasses.replace(
                original,
                node_id=minted[original.node_id],
                parent_id=minted[original.parent_id],
            )
        )

    return tuple(created)


def _delete_subtree(connection: sqlite3.Connection, node_id: NodeId) -> None:
    """A node and everything under it, in one statement.

    The descendants are found by the database rather than walked in Python: a
    walk would read the tree in one statement and delete in another, and a
    child created between the two would survive its parent. The recursion runs
    inside the delete, on the same snapshot of the file.
    """
    _guard(connection, _DELETE_SUBTREE, (node_id,))


def _update_node(connection: sqlite3.Connection, snapshot: NodeSnapshot) -> None:
    """Write a node that already exists back as the snapshot says it now is.

    Deliberately whole-row: an update names every field a snapshot carries, so
    there is no path by which the row and the snapshot the caller is handed
    could describe different things.
    """
    _guard(
        connection,
        "UPDATE nodes SET parent_id = ?, name = ?, message_id = ? WHERE node_id = ?",
        (
            snapshot.parent_id,
            snapshot.name,
            snapshot.message_id,
            snapshot.node_id,
        ),
    )
