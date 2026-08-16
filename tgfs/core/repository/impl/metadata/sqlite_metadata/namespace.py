"""The directory tree the existing APIs see, over durable node ids.

Every node here is the same `TGFSDirectory` / `TGFSFileRef` the rest of TGFS
already works with, with one thing added: the durable id the store knows it by.
That id is what lets a change the caller makes in memory be expressed as a
command about a specific node rather than about a path, which is the whole
reason the tree can be rebuilt from the store and still be the same tree.

Mutating methods do not write. They record the command that says what the caller
asked for and change the visible tree so the caller can read what it just did;
the file is told at push, and what comes back from the store is then the only
thing this tree is rebuilt from. Nothing here reaches the database.
"""

from typing import Optional, Protocol

from tgfs.core.commands import (
    ROOT_NODE_ID,
    ClearRoot,
    CopySubtree,
    CreateDir,
    CreateFileRef,
    DeleteNode,
    MutationCommand,
    NodeId,
    NodeKind,
    NodeSnapshot,
    Projection,
    RelinkFileRef,
    new_node_id,
    new_operation_id,
)
from tgfs.core.model import TGFSDirectory, TGFSFileRef
from tgfs.errors import FileOrDirectoryAlreadyExists, InvalidCommandPayload


class CommandSink(Protocol):
    """Whoever collects the commands this tree's changes mean."""

    def enqueue(self, command: MutationCommand) -> None: ...


class SqliteFileRef(TGFSFileRef):
    """A file ref that knows which durable node it is."""

    def __init__(
        self,
        sink: CommandSink,
        node_id: NodeId,
        message_id: int,
        name: str,
        location: TGFSDirectory,
    ):
        self._sink = sink
        self.node_id = node_id
        self._message_id: int = message_id
        # Set before the base constructor assigns through the property below,
        # which needs to know whether it is being built or being changed.
        self._attached = False
        super().__init__(message_id=message_id, name=name, location=location)
        self._attached = True

    @property
    def message_id(self) -> int:
        return self._message_id

    @message_id.setter
    def message_id(self, value: int) -> None:
        """Point this ref at another file descriptor message.

        The existing file API relinks a ref by assigning this field and then
        pushing, so the assignment is where the intention arrives and where the
        command has to be recorded. Writing the id straight into the tree would
        make the ref point at a message no store was ever told about.
        """
        if self._attached and value != self._message_id:
            self._sink.enqueue(
                RelinkFileRef(
                    operation_id=new_operation_id(),
                    node_id=self.node_id,
                    message_id=value,
                )
            )
        self._message_id = value

    def adopt(self, snapshot: NodeSnapshot, location: TGFSDirectory) -> None:
        """Take on what the store says this node now is.

        Assigned behind the property on purpose: this is the store telling the
        tree what it holds, so turning it into another command would ask the
        store to accept what it just reported.
        """
        self.name = snapshot.name
        self.location = location
        self._message_id = _message_of(snapshot)


class SqliteDirectory(TGFSDirectory):
    """A directory that knows which durable node it is.

    The mutating methods of `TGFSDirectory` are overridden rather than extended:
    the base ones build plain nodes with no id, which the store could never be
    told about afterwards.
    """

    def __init__(
        self,
        sink: CommandSink,
        node_id: NodeId,
        name: str,
        parent: Optional[TGFSDirectory],
        children: Optional[list[TGFSDirectory]] = None,
        files: Optional[list[TGFSFileRef]] = None,
    ):
        self._sink = sink
        self.node_id = node_id
        super().__init__(name, parent, children or [], files or [])

    def create_dir(
        self, name: str, dir_to_copy: Optional[TGFSDirectory] = None
    ) -> "SqliteDirectory":
        if self.find_dirs([name]):
            raise FileOrDirectoryAlreadyExists(name)

        node_id = new_node_id()
        command: MutationCommand
        if dir_to_copy is None:
            command = CreateDir(
                operation_id=new_operation_id(),
                parent_id=self.node_id,
                name=name,
                node_id=node_id,
            )
        else:
            command = CopySubtree(
                operation_id=new_operation_id(),
                source_id=_node_id_of(dir_to_copy),
                parent_id=self.node_id,
                name=name,
                node_id=node_id,
            )

        # Built empty even when it is a copy: what the copy contains is decided
        # by the store, which mints an id for every node it duplicates, and is
        # filled in when this tree is rebuilt from what the store accepted.
        child = SqliteDirectory(self._sink, node_id, name, self)
        self.children.append(child)
        self._sink.enqueue(command)
        return child

    def create_file_ref(self, name: str, fd_message_id: int) -> SqliteFileRef:
        if self.find_files([name]):
            raise FileOrDirectoryAlreadyExists(name)

        node_id = new_node_id()
        # The command is built first: it validates the name and the message id,
        # and a refusal here must leave the visible tree untouched.
        command = CreateFileRef(
            operation_id=new_operation_id(),
            parent_id=self.node_id,
            name=name,
            message_id=fd_message_id,
            node_id=node_id,
        )

        file_ref = SqliteFileRef(self._sink, node_id, fd_message_id, name, self)
        self.files.append(file_ref)
        self._sink.enqueue(command)
        return file_ref

    def delete_file_ref(self, fr: TGFSFileRef) -> None:
        node_id = _node_id_of(fr)
        # Removed from the visible tree first: a ref that is not in this
        # directory is the caller's mistake, and the store should not be asked
        # to delete a node on the strength of it.
        super().delete_file_ref(fr)
        self._sink.enqueue(
            DeleteNode(operation_id=new_operation_id(), node_id=node_id)
        )

    def delete(self) -> None:
        """Remove this directory and everything below it.

        The root is the exception every store makes: it cannot be removed, only
        emptied, and a root that is already empty is nothing to ask for - the
        store has no revision to give a command that changes nothing.
        """
        if self.parent is None:
            if not (self.children or self.files):
                return
            command: MutationCommand = ClearRoot(operation_id=new_operation_id())
        else:
            command = DeleteNode(
                operation_id=new_operation_id(), node_id=self.node_id
            )

        super().delete()
        self._sink.enqueue(command)

    def adopt(self, snapshot: NodeSnapshot, parent: TGFSDirectory) -> None:
        self.name = snapshot.name
        self.parent = parent


def _node_id_of(node: object) -> NodeId:
    """The durable id of a node this tree is allowed to name in a command.

    A directory or ref built by some other backend carries no id the store has
    ever heard of, so a command about it could only name the wrong node or none
    at all. Refused here, where the caller still knows what it passed in.
    """
    if isinstance(node, (SqliteDirectory, SqliteFileRef)):
        return node.node_id
    raise InvalidCommandPayload(
        f"a {type(node).__name__} does not belong to this metadata store"
    )


def rebuild(root: SqliteDirectory, projection: Projection) -> None:
    """Make the visible tree say exactly what the store's projection says.

    Objects are kept wherever the store still has their node, so a caller
    holding a directory it created keeps holding the same directory. Everything
    else - what is under it, what it is called, which message a ref points at -
    comes from the projection, so a change the store refused cannot survive in
    memory as if it had happened.
    """
    known = _by_node_id(root)

    children_of: dict[NodeId, list[NodeSnapshot]] = {}
    for snapshot in projection.nodes.values():
        children_of.setdefault(snapshot.parent_id, []).append(snapshot)

    frontier = [root]
    while frontier:
        directory = frontier.pop()
        children: list[TGFSDirectory] = []
        files: list[TGFSFileRef] = []

        # Sorted so that two runs over the same store list a directory the same
        # way: the projection is a mapping and owes nobody an order.
        for snapshot in sorted(
            children_of.get(directory.node_id, ()), key=lambda s: s.name
        ):
            if snapshot.kind is NodeKind.DIRECTORY:
                child = _directory(known.get(snapshot.node_id), snapshot, directory)
                children.append(child)
                frontier.append(child)
            else:
                files.append(_file_ref(known.get(snapshot.node_id), snapshot, directory))

        directory.children = children
        directory.files = files


def _directory(
    existing: object, snapshot: NodeSnapshot, parent: SqliteDirectory
) -> SqliteDirectory:
    if isinstance(existing, SqliteDirectory):
        existing.adopt(snapshot, parent)
        return existing
    return SqliteDirectory(parent._sink, snapshot.node_id, snapshot.name, parent)


def _file_ref(
    existing: object, snapshot: NodeSnapshot, location: SqliteDirectory
) -> SqliteFileRef:
    if isinstance(existing, SqliteFileRef):
        existing.adopt(snapshot, location)
        return existing
    return SqliteFileRef(
        location._sink,
        snapshot.node_id,
        _message_of(snapshot),
        snapshot.name,
        location,
    )


def _message_of(snapshot: NodeSnapshot) -> int:
    """The message a file ref points at.

    A snapshot's message id is optional because a directory has none, and a file
    ref always has one - the store refuses to record one without. Read through
    here so that a snapshot which somehow lacks it is reported as the broken
    record it is, rather than quietly becoming a ref pointing at nothing.
    """
    if snapshot.message_id is None:
        raise InvalidCommandPayload(
            f"the file ref {snapshot.name!r} points at no message"
        )
    return snapshot.message_id


def _by_node_id(root: SqliteDirectory) -> dict[NodeId, object]:
    """Every node this tree currently holds, by the id the store knows it by."""
    known: dict[NodeId, object] = {ROOT_NODE_ID: root}
    frontier: list[TGFSDirectory] = [root]

    while frontier:
        directory = frontier.pop()
        for file_ref in directory.files:
            if isinstance(file_ref, SqliteFileRef):
                known[file_ref.node_id] = file_ref
        for child in directory.children:
            if isinstance(child, SqliteDirectory):
                known[child.node_id] = child
            frontier.append(child)

    return known


def empty_root(sink: CommandSink) -> SqliteDirectory:
    """The one node no command creates and none may remove."""
    return SqliteDirectory(sink, ROOT_NODE_ID, "root", None)


__all__ = [
    "CommandSink",
    "SqliteDirectory",
    "SqliteFileRef",
    "empty_root",
    "rebuild",
]
