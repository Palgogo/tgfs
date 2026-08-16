"""Non-secret identity of an import's source and target, and when it ran.

A `SourceDescriptor` is supplied by the caller, never discovered by asking a
live client what it is pointed at: the caller is the one who knows which
repository and which immutable ref or tree it means to import, and a report
that only echoed back what a mutable client happened to be configured with
would be worthless as a record once that configuration moved on.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Union


@dataclass(frozen=True)
class SourceDescriptor:
    """An immutable, non-secret pointer to what was imported.

    A repository label and the ref or tree SHA it was pinned to - never a
    token, never a URL with embedded credentials, just enough to name the
    source in a report.
    """

    repository: str
    ref: str

    def __post_init__(self) -> None:
        if not self.repository or not self.ref:
            raise ValueError(
                "a source descriptor needs both a non-empty repository and ref"
            )


@dataclass(frozen=True)
class TargetDescriptor:
    """The non-secret identity of a local SQLite target: its path."""

    path: str

    @classmethod
    def of(cls, path: Union[str, Path]) -> "TargetDescriptor":
        return cls(path=str(path))


def utc_now() -> str:
    """The current instant, as the UTC ISO-8601 string a report stamps itself with."""
    return datetime.now(timezone.utc).isoformat()


__all__ = ["SourceDescriptor", "TargetDescriptor", "utc_now"]
