"""Mapping a durable outbox event into a canonical, deterministic envelope.

`SourceEvent` is the minimal shape a caller (P2a's outbox, in this repo)
supplies: a stable event identity, its position in the durable sequence, a
declared event type, and a JSON-serialized payload. `build_envelope` maps
that into an `ExportEnvelope` whose bytes are reproducible: equal event data
always yields byte-for-byte identical `canonical_bytes()`/`digest()`, so two
independent processes - or the same process replaying after a restart - agree
on what a given event means without comparing wall-clock time, object
identity, or any other nondeterministic detail.
"""

import dataclasses
import hashlib
import json

SCHEMA_VERSION = 1


class EnvelopeValidationError(Exception):
    """A source event cannot be mapped into a valid export envelope."""


@dataclasses.dataclass(frozen=True)
class SourceEvent:
    event_id: str
    sequence: int
    event_type: str
    payload: str


@dataclasses.dataclass(frozen=True)
class ExportEnvelope:
    event_id: str
    sequence: int
    event_type: str
    schema_version: int
    payload_digest: str
    payload_bytes: bytes

    def canonical_bytes(self) -> bytes:
        header = {
            "event_id": self.event_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "schema_version": self.schema_version,
            "payload_digest": self.payload_digest,
        }
        header_bytes = json.dumps(
            header, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return header_bytes + b"\n" + self.payload_bytes

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def build_envelope(event: SourceEvent) -> ExportEnvelope:
    if not event.event_id:
        raise EnvelopeValidationError("event_id must be non-empty")
    if event.sequence <= 0:
        raise EnvelopeValidationError(
            f"sequence must be a positive integer, got {event.sequence!r}"
        )
    if not event.event_type:
        raise EnvelopeValidationError("event_type must be non-empty")

    try:
        parsed = json.loads(event.payload)
    except json.JSONDecodeError as e:
        raise EnvelopeValidationError(f"payload is not valid JSON: {e}") from e

    payload_bytes = json.dumps(parsed, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    payload_digest = hashlib.sha256(payload_bytes).hexdigest()

    return ExportEnvelope(
        event_id=event.event_id,
        sequence=event.sequence,
        event_type=event.event_type,
        schema_version=SCHEMA_VERSION,
        payload_digest=payload_digest,
        payload_bytes=payload_bytes,
    )
