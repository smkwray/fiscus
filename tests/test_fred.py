from __future__ import annotations

import hashlib
from pathlib import Path

import httpx

from fiscus.service import FiscusService


def test_keyless_fred_fetch_is_cached_and_returns_provenance(
    tmp_path: Path,
    fixture_dir: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    content = (fixture_dir / "fred_wdtgal.csv").read_bytes()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/graph/fredgraph.csv"
        assert request.url.params["id"] == "WDTGAL"
        assert request.url.params["cosd"] == "2024-01-01"
        return httpx.Response(
            200,
            content=content,
            headers={
                "Content-Type": "text/csv",
                "Last-Modified": "Thu, 09 Jul 2026 20:32:00 GMT",
            },
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        first = service.get_series("fred.wdtgal", start="2024-01-01")
        second = service.get_series("fred.wdtgal", start="2024-01-01")
        payload = service.read_bytes(first["artifact"]["cache_key"])
        full_rows = list(service.iter_csv_rows(first["artifact"]["cache_key"][:20]))

    assert calls == 1
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert first["preview"]["row_count"] == 3
    assert first["preview"]["date_coverage"] == {
        "start": "2024-01-03",
        "end": "2024-01-17",
    }
    assert first["provenance"]["vintage"].startswith("http-last-modified:")
    assert set(first["artifact"]) == {
        "cache_key",
        "filename",
        "mime_type",
        "sha256",
        "byte_count",
    }
    assert first["artifact"]["filename"] == "data.csv"
    assert first["artifact"]["mime_type"] == "text/csv"
    assert first["artifact"]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert first["artifact"]["byte_count"] == len(payload)
    assert first["artifact"]["cache_key"] == first["provenance"]["cache_key"]
    assert "full_data_path" not in first
    assert "manifest_path" not in first
    assert len(full_rows) == 3
    assert {item["id"] for item in first["caveats"]} >= {"fred.wdtgal.wednesday_level"}
    client.close()


def test_keyed_fred_api_redacts_secret_and_uses_realtime_vintage(
    tmp_path: Path,
    fixture_dir: Path,
    monkeypatch,
) -> None:
    secret = "a" * 32
    monkeypatch.setenv("FRED_API_KEY", secret)
    payload = (fixture_dir / "fred_wdtgal_api.json").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fred/series/observations"
        assert request.url.params["api_key"] == secret
        return httpx.Response(
            200,
            content=payload,
            headers={"Content-Type": "application/json"},
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        result = service.get_series(
            "fred.wdtgal",
            start="2024-01-01",
            end="2024-01-31",
        )

    assert result["provenance"]["vintage"] == "fred-realtime:2026-07-13"
    assert secret not in result["provenance"]["source_url"]
    assert "REDACTED" in result["provenance"]["source_url"]
    assert result["preview"]["row_count"] == 2
    client.close()
