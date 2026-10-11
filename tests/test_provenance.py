from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from fiscus.errors import CacheError
from fiscus.service import FiscusService


def test_provenance_resolves_immutable_artifact_by_key_and_prefix(
    tmp_path: Path,
    fixture_dir: Path,
    monkeypatch,
) -> None:
    """provenance() must resolve only immutable artifact identities: the full
    cache key or a unique prefix, never a mutable dataset alias."""
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    content = (fixture_dir / "fred_wdtgal.csv").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        fetched = service.get_series("fred.wdtgal", start="2024-01-01")
        key = fetched["provenance"]["cache_key"]

        by_full_key = service.provenance(key)
        by_prefix = service.provenance(key[:16])
        assert by_full_key["manifest"]["cache_key"] == key
        assert by_prefix["manifest"]["cache_key"] == key
        assert by_full_key["artifact"] == fetched["artifact"]
        assert by_prefix["artifact"] == fetched["artifact"]
        assert by_full_key["sha256_verified"] is True
        assert "full_data_path" not in by_full_key
        assert "manifest_path" not in by_full_key

        with pytest.raises(CacheError, match="at least 12 hex"):
            service.provenance("fred.wdtgal")
        with pytest.raises(CacheError, match="no cached artifact"):
            service.provenance("0" * 64)
    client.close()


def test_mutable_latest_expires_without_overwriting_old_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            content=f"DATE,WDTGAL\n2024-01-03,{calls}\n".encode(),
            headers={"etag": f'"revision-{calls}"'},
            request=request,
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service,
    ):
        first = service.get_series("fred.wdtgal")
        old_key = first["artifact"]["cache_key"]
        old_bytes = service.read_bytes(old_key)
        service.cache._connect().execute(
            "UPDATE latest SET updated_at = ?",
            ("2024-01-01T00:00:00+00:00",),
        )
        refreshed = service.get_series("fred.wdtgal")
        repeated = service.get_series("fred.wdtgal")
        assert refreshed["cache_hit"] is False
        assert refreshed["preview"]["head"][0]["value"] == "2"
        assert refreshed["artifact"]["cache_key"] != old_key
        assert service.read_bytes(old_key) == old_bytes
        assert repeated["cache_hit"] is True
    assert calls == 2


def test_historical_snapshot_remains_cacheable_after_latest_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "observations": [{
                    "date": "2024-01-03", "value": "1",
                    "realtime_start": "2024-02-01", "realtime_end": "2024-02-01",
                }],
                "realtime_start": "2024-02-01", "realtime_end": "2024-02-01",
                "output_type": 1, "units": "lin", "sort_order": "asc",
                "count": 1, "offset": 0, "limit": 100000,
            },
            request=request,
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service,
    ):
        first = service.get_series("fred.wdtgal", vintage_as_of="2024-02-01")
        service.cache._connect().execute(
            "UPDATE latest SET updated_at = ?",
            ("2024-01-01T00:00:00+00:00",),
        )
        second = service.get_series("fred.wdtgal", vintage_as_of="2024-02-01")
        assert first["artifact"] == second["artifact"]
        assert second["cache_hit"] is True
    assert calls == 1


def test_z1_series_cache_tracks_the_loaded_package_vintage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fiscus.executors.z1 import Z1Loaded, Z1SeriesMeta

    old = Z1Loaded(
        rows=[{"date": "2024-03-31", "value": "1"}],
        meta=Z1SeriesMeta(
            code="FL313020005.Q", description="Federal deposits", table="L.106",
            units="Millions of dollars", csv_member="data.csv",
        ),
        vintage="z1-release:2024-06-01|sha256:old",
        release_date="2024-06-01",
        package_source_url="https://example.test/z1.zip",
        package_retrieved_at_utc="2024-06-01T00:00:00+00:00",
        package_metadata={},
    )
    current = replace(
        old,
        rows=[{"date": "2024-03-31", "value": "2"}],
        vintage="z1-release:2024-09-01|sha256:new",
        release_date="2024-09-01",
    )
    with FiscusService(data_dir=tmp_path) as service:
        monkeypatch.setattr(service.z1, "load_series", lambda *args, **kwargs: old)
        first = service.z1_series("FL313020005.Q", "FL")
        monkeypatch.setattr(service.z1, "load_series", lambda *args, **kwargs: current)
        second = service.z1_series("FL313020005.Q", "FL")
        assert second["cache_hit"] is False
        assert second["preview"]["head"][0]["value"] == "2"
        assert second["provenance"]["vintage"] == current.vintage
        assert service.read_bytes(first["artifact"]["cache_key"]) != service.read_bytes(
            second["artifact"]["cache_key"]
        )
