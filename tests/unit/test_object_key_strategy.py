"""Unit and security tests for the Tenant/Venue Object-Key Strategy (Task 9.5).

Validates:
- Centralized key generation across all media categories (recordings, evidence, reports, analytics, temporary)
- Strict tenant and venue namespace isolation
- Category separation and collision prevention
- Deterministic output and stable UUID-based identity
- Round-trip parsing and component extraction
- Malicious input rejection (path traversal, backslashes, null bytes, double slashes, invalid UUIDs)
- Safe extension normalization
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from backend.app.infrastructure.storage import (
    InvalidObjectKeyError,
    ObjectCategory,
    build_analytics_key,
    build_evidence_key,
    build_object_key,
    build_recording_key,
    build_report_key,
    build_temporary_key,
    normalize_extension,
    parse_object_key,
)


class TestObjectKeyGeneration:
    """Tests verifying deterministic generation for all domain media categories."""

    @pytest.fixture
    def fixed_time(self) -> datetime:
        return datetime(2026, 8, 10, 14, 30, 0, tzinfo=UTC)

    def test_build_recording_key(self, fixed_time: datetime) -> None:
        tenant_id = UUID("c7a10f82-84b2-4d7a-b50a-bdfd189196b0")
        venue_id = UUID("4a87265a-063a-4a6c-9c70-7613768b4ad3")
        recording_id = UUID("8f3b23c1-0731-419b-a3d2-d17e3f2824b2")

        key = build_recording_key(
            tenant_id=tenant_id,
            venue_id=venue_id,
            recording_id=recording_id,
            extension="mp4",
            capture_time=fixed_time,
        )

        expected = (
            "tenants/c7a10f82-84b2-4d7a-b50a-bdfd189196b0/"
            "venues/4a87265a-063a-4a6c-9c70-7613768b4ad3/"
            "recordings/2026/08/10/8f3b23c1-0731-419b-a3d2-d17e3f2824b2.mp4"
        )
        assert key == expected

    def test_build_evidence_key(self, fixed_time: datetime) -> None:
        tenant_id = uuid4()
        venue_id = uuid4()
        evidence_id = uuid4()

        key = build_evidence_key(
            tenant_id=tenant_id,
            venue_id=venue_id,
            evidence_id=evidence_id,
            extension="jpg",
            capture_time=fixed_time,
        )

        assert f"tenants/{tenant_id}/venues/{venue_id}/evidence/2026/08/10/{evidence_id}.jpg" == key

    def test_build_report_key(self, fixed_time: datetime) -> None:
        tenant_id = uuid4()
        venue_id = uuid4()
        report_id = uuid4()

        key = build_report_key(
            tenant_id=tenant_id,
            venue_id=venue_id,
            report_id=report_id,
            extension="pdf",
            capture_time=fixed_time,
        )

        assert f"tenants/{tenant_id}/venues/{venue_id}/reports/2026/08/10/{report_id}.pdf" == key

    def test_build_analytics_key(self, fixed_time: datetime) -> None:
        tenant_id = uuid4()
        venue_id = uuid4()
        artifact_id = uuid4()

        key = build_analytics_key(
            tenant_id=tenant_id,
            venue_id=venue_id,
            artifact_id=artifact_id,
            extension="json.gz",
            capture_time=fixed_time,
        )

        assert (
            f"tenants/{tenant_id}/venues/{venue_id}/analytics/2026/08/10/{artifact_id}.json.gz"
            == key
        )

    def test_build_temporary_key(self, fixed_time: datetime) -> None:
        tenant_id = uuid4()
        venue_id = uuid4()
        upload_id = uuid4()

        key = build_temporary_key(
            tenant_id=tenant_id,
            venue_id=venue_id,
            upload_id=upload_id,
            extension="bin",
            capture_time=fixed_time,
        )

        assert f"tenants/{tenant_id}/venues/{venue_id}/temporary/2026/08/10/{upload_id}.bin" == key

    def test_accepts_valid_string_uuids(self, fixed_time: datetime) -> None:
        t_str = "c7a10f82-84b2-4d7a-b50a-bdfd189196b0"
        v_str = "4a87265a-063a-4a6c-9c70-7613768b4ad3"
        r_str = "8f3b23c1-0731-419b-a3d2-d17e3f2824b2"

        key = build_recording_key(t_str, v_str, r_str, "mp4", capture_time=fixed_time)
        parsed = parse_object_key(key)
        assert parsed.tenant_id == UUID(t_str)
        assert parsed.venue_id == UUID(v_str)
        assert parsed.artifact_id == UUID(r_str)


class TestNamespaceIsolation:
    """Tests verifying strict isolation between tenants, venues, and categories."""

    def test_tenant_namespace_isolation(self) -> None:
        tenant_a = uuid4()
        tenant_b = uuid4()
        shared_venue = uuid4()
        media_id = uuid4()

        key_a = build_recording_key(tenant_a, shared_venue, media_id)
        key_b = build_recording_key(tenant_b, shared_venue, media_id)

        assert key_a != key_b
        assert key_a.startswith(f"tenants/{tenant_a}/")
        assert key_b.startswith(f"tenants/{tenant_b}/")

    def test_venue_namespace_isolation(self) -> None:
        tenant = uuid4()
        venue_1 = uuid4()
        venue_2 = uuid4()
        media_id = uuid4()

        key_1 = build_evidence_key(tenant, venue_1, media_id)
        key_2 = build_evidence_key(tenant, venue_2, media_id)

        assert key_1 != key_2
        assert f"/venues/{venue_1}/" in key_1
        assert f"/venues/{venue_2}/" in key_2

    def test_category_namespace_isolation(self) -> None:
        tenant = uuid4()
        venue = uuid4()
        media_id = uuid4()

        recording_key = build_recording_key(tenant, venue, media_id)
        evidence_key = build_evidence_key(tenant, venue, media_id)
        report_key = build_report_key(tenant, venue, media_id)
        analytics_key = build_analytics_key(tenant, venue, media_id)
        temporary_key = build_temporary_key(tenant, venue, media_id)

        all_keys = {recording_key, evidence_key, report_key, analytics_key, temporary_key}
        assert len(all_keys) == 5, "All categories must produce mutually exclusive object paths"

    def test_deterministic_output_stability(self) -> None:
        tenant = uuid4()
        venue = uuid4()
        media_id = uuid4()
        ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

        key_1 = build_evidence_key(tenant, venue, media_id, "jpg", capture_time=ts)
        key_2 = build_evidence_key(tenant, venue, media_id, "jpg", capture_time=ts)
        assert key_1 == key_2


class TestKeyParsingAndRoundtrip:
    """Tests verifying parse_object_key roundtrip functionality."""

    @pytest.mark.parametrize(
        ("category", "ext"),
        [
            (ObjectCategory.RECORDINGS, "mp4"),
            (ObjectCategory.EVIDENCE, "jpg"),
            (ObjectCategory.REPORTS, "pdf"),
            (ObjectCategory.ANALYTICS, "json.gz"),
            (ObjectCategory.TEMPORARY, "bin"),
        ],
    )
    def test_roundtrip_all_categories(self, category: ObjectCategory, ext: str) -> None:
        tenant_id = uuid4()
        venue_id = uuid4()
        artifact_id = uuid4()
        ts = datetime(2026, 9, 21, 15, 0, 0, tzinfo=UTC)

        key = build_object_key(tenant_id, venue_id, category, artifact_id, ext, capture_time=ts)
        parsed = parse_object_key(key)

        assert parsed.tenant_id == tenant_id
        assert parsed.venue_id == venue_id
        assert parsed.category == category
        assert parsed.artifact_id == artifact_id
        assert parsed.year == 2026
        assert parsed.month == 9
        assert parsed.day == 21
        assert parsed.extension == ext.lstrip(".").lower()


class TestSecurityAndMaliciousInputs:
    """Security tests verifying rejection of attack vectors (path traversal, control chars, injection)."""

    def test_rejects_path_traversal_in_parser(self) -> None:
        malicious_keys = [
            "tenants/../../etc/passwd",
            "tenants/c7a10f82-84b2-4d7a-b50a-bdfd189196b0/../../../secret",
            "/tenants/c7a10f82-84b2-4d7a-b50a-bdfd189196b0/venues/4a87265a-063a-4a6c-9c70-7613768b4ad3/evidence/2026/01/01/8f3b23c1-0731-419b-a3d2-d17e3f2824b2.jpg",
            "tenants//venues/evidence/2026/01/01/8f3b23c1-0731-419b-a3d2-d17e3f2824b2.jpg",
            "tenants/c7a10f82-84b2-4d7a-b50a-bdfd189196b0\\venues\\4a87265a-063a-4a6c-9c70-7613768b4ad3\\evidence\\2026\\01\\01\\8f3b23c1-0731-419b-a3d2-d17e3f2824b2.jpg",
            "tenants/c7a10f82-84b2-4d7a-b50a-bdfd189196b0/venues/4a87265a-063a-4a6c-9c70-7613768b4ad3/evidence/2026/01/01/8f3b23c1-0731-419b-a3d2-d17e3f2824b2.jpg\x00.png",
        ]

        for key in malicious_keys:
            with pytest.raises(InvalidObjectKeyError):
                parse_object_key(key)

    def test_rejects_invalid_uuids_in_builder(self) -> None:
        with pytest.raises(InvalidObjectKeyError, match="Invalid tenant_id"):
            build_recording_key("../malicious-tenant", uuid4(), uuid4())

        with pytest.raises(InvalidObjectKeyError, match="Invalid venue_id"):
            build_recording_key(uuid4(), "not-a-uuid-string", uuid4())

        with pytest.raises(InvalidObjectKeyError, match="Invalid artifact_id"):
            build_recording_key(uuid4(), uuid4(), "")

    def test_rejects_malicious_extensions(self) -> None:
        invalid_extensions = [
            "",
            "mp4; rm -rf /",
            "exe\x00.mp4",
            "mp4/../png",
            "\\windows\\path",
            "mp4\n",
            "mp4\r\n",
            "mp4\t",
            " mp4 ",
        ]

        for ext in invalid_extensions:
            with pytest.raises(InvalidObjectKeyError):
                normalize_extension(ext)

            with pytest.raises(InvalidObjectKeyError):
                build_recording_key(uuid4(), uuid4(), uuid4(), extension=ext)

    def test_non_string_key_raises(self) -> None:
        with pytest.raises(InvalidObjectKeyError):
            parse_object_key(12345)  # type: ignore[arg-type]
