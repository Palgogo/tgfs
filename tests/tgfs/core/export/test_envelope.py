"""Mapping a durable outbox event into a canonical, deterministic envelope.

Two envelopes built from equal event data must serialize to byte-for-byte
identical bytes and digests across runs - no wall-clock timestamp,
process-specific object repr, or other nondeterminism may leak in. Malformed
or unsupported event data must be rejected through an explicit, typed error
rather than producing a partially-built envelope.
"""

import pytest

from tgfs.core.export.envelope import (
    EnvelopeValidationError,
    SourceEvent,
    build_envelope,
)


def _event(**overrides: object) -> SourceEvent:
    fields: dict = dict(
        event_id="op-1",
        sequence=1,
        event_type="metadata.command",
        payload='{"b": 2, "a": 1}',
    )
    fields.update(overrides)
    return SourceEvent(**fields)


def test_equal_event_data_yields_a_byte_identical_envelope_across_runs() -> None:
    first = build_envelope(_event())
    second = build_envelope(_event())

    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.digest() == second.digest()


def test_the_payload_digest_is_stable_regardless_of_source_key_order() -> None:
    same_content_different_order = build_envelope(_event(payload='{"a": 1, "b": 2}'))
    original = build_envelope(_event(payload='{"b": 2, "a": 1}'))

    assert original.payload_bytes == same_content_different_order.payload_bytes
    assert original.payload_digest == same_content_different_order.payload_digest
    assert original.digest() == same_content_different_order.digest()


def test_the_envelope_carries_stable_identity_and_ordering_fields() -> None:
    envelope = build_envelope(_event())

    assert envelope.event_id == "op-1"
    assert envelope.sequence == 1
    assert envelope.event_type == "metadata.command"
    assert envelope.schema_version >= 1


def test_a_different_payload_produces_a_different_digest() -> None:
    a = build_envelope(_event(payload='{"a": 1}'))
    b = build_envelope(_event(payload='{"a": 2}'))

    assert a.payload_digest != b.payload_digest
    assert a.digest() != b.digest()


def test_a_non_json_payload_is_rejected() -> None:
    with pytest.raises(EnvelopeValidationError):
        build_envelope(_event(payload="not-json"))


def test_an_empty_event_id_is_rejected() -> None:
    with pytest.raises(EnvelopeValidationError):
        build_envelope(_event(event_id=""))


def test_a_non_positive_sequence_is_rejected() -> None:
    with pytest.raises(EnvelopeValidationError):
        build_envelope(_event(sequence=0))


def test_an_empty_event_type_is_rejected() -> None:
    with pytest.raises(EnvelopeValidationError):
        build_envelope(_event(event_type=""))
