"""The non-secret identity a source and a target are recorded under.

`SourceDescriptor` is supplied by whoever calls the importer - it is never
discovered by asking a live client what it happens to be pointed at right
now, which is the only way a report stays meaningful after that client's
configuration has moved on.
"""

import pytest

from tgfs.core.metadata_import.provenance import (
    SourceDescriptor,
    TargetDescriptor,
    utc_now,
)


class TestSourceDescriptor:
    def test_a_repository_and_ref_are_both_required(self):
        SourceDescriptor(repository="octo/demo", ref="deadbeef")

        with pytest.raises(ValueError):
            SourceDescriptor(repository="", ref="deadbeef")

        with pytest.raises(ValueError):
            SourceDescriptor(repository="octo/demo", ref="")

    def test_it_carries_no_token_or_credential_field(self):
        descriptor = SourceDescriptor(repository="octo/demo", ref="deadbeef")

        field_names = {f for f in vars(descriptor)}
        assert "token" not in field_names
        assert "access_token" not in field_names
        assert "credential" not in field_names


class TestTargetDescriptor:
    def test_it_records_the_target_path_as_a_plain_string(self, tmp_path):
        path = tmp_path / "target.sqlite3"

        descriptor = TargetDescriptor.of(path)

        assert descriptor.path == str(path)


class TestUtcNow:
    def test_it_returns_a_utc_iso8601_timestamp(self):
        stamp = utc_now()

        assert stamp.endswith("+00:00")
        # Sortable/parseable fixed-width ISO-8601 - the "T" separator is the
        # cheapest proof the format is what it claims.
        assert "T" in stamp
