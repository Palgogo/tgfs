"""Ways of looking at the namespace these tests share.

A restart is only interesting if what comes back is the same thing that went in,
and 'the same thing' has to mean the paths and the messages they point at -
not the objects, which are new every time, and not the node ids, which a copy
mints fresh on purpose.
"""

from tgfs.core.model import TGFSDirectory
from tgfs.core.repository.impl.metadata.sqlite_metadata import SqliteDirectory


def sqlite_dir(directory: TGFSDirectory, name: str) -> SqliteDirectory:
    """The named child, as the durable node it has to be.

    Looking a directory up returns the plain type the rest of TGFS works with,
    which carries no node id. Going through here is how a test says it expects
    a durable node - and finds out if a rebuild ever left a plain one behind.
    """
    child = directory.find_dir(name)
    assert isinstance(child, SqliteDirectory), f"{name} is a {type(child).__name__}"
    return child


def view(directory: TGFSDirectory, path: str = "") -> list[tuple[str, str, int]]:
    """Every path below this directory, sorted, with what each one points at."""
    entries: list[tuple[str, str, int]] = []

    for child in directory.find_dirs():
        child_path = f"{path}/{child.name}"
        entries.append((child_path, "D", 0))
        entries.extend(view(child, child_path))

    for file_ref in directory.find_files():
        entries.append((f"{path}/{file_ref.name}", "FR", file_ref.message_id))

    return sorted(entries)
