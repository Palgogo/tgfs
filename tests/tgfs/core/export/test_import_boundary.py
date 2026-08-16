"""P2b stays out of runtime, config, client, and GitHub territory.

Read statically rather than imported and inspected at runtime, for the same
reason P2a's outbox boundary test does: importing `tgfs.core.client` to
prove this module doesn't transitively pull it in would drag in every one of
the client's own dependencies just to make the point, and risks a side
effect from an import this package must never make.
"""

import ast
from pathlib import Path

import tgfs.core.export as export_package

_FORBIDDEN_PREFIXES = (
    "tgfs.config",
    "tgfs.core.client",
    "tgfs.app",
    "tgfs.telegram",
    "tgfs.core.repository.impl.metadata.github_repo",
    "github",
    "PyGithub",
)


def _imported_names(source_path: Path) -> set[str]:
    tree = ast.parse(source_path.read_text(), filename=str(source_path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def _source_files() -> list[Path]:
    package_dir = Path(export_package.__file__).parent
    return sorted(package_dir.rglob("*.py"))


def test_no_export_source_file_imports_a_forbidden_module() -> None:
    offenders: dict[str, set[str]] = {}
    for path in _source_files():
        imported = _imported_names(path)
        hits = {
            name
            for name in imported
            if any(
                name == prefix or name.startswith(prefix + ".")
                for prefix in _FORBIDDEN_PREFIXES
            )
        }
        if hits:
            offenders[str(path)] = hits

    assert offenders == {}


def test_there_is_at_least_one_source_file_to_check() -> None:
    # A boundary test over an empty set proves nothing; this keeps the
    # assertion above honest if the package layout ever changes.
    assert len(_source_files()) >= 4
