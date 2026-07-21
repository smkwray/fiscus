from __future__ import annotations

import io
import zipfile
from pathlib import Path

import httpx
import pytest

from fiscus.errors import DatasetNotFoundError
from fiscus.service import FiscusService, normalize_z1_code


def _package_bytes() -> bytes:
    dictionary = (
        "FL313020005.Q\tFederal government; checkable deposits; asset\tLine 25\t"
        "F2.2.s Checkable deposits and currency\t"
        "Millions of dollars; amounts outstanding end of period, not seasonally adjusted\n"
        "FU313020005.Q\tFederal government; checkable deposits; asset\tLine 25\t"
        "F2.2.s Checkable deposits and currency\t"
        "Millions of dollars; quarterly figures, not seasonally adjusted\n"
        "FL999000000.Q\tExample uncataloged series\tLine 1\tF9.9 Example\t"
        "Millions of dollars; amounts outstanding end of period, not seasonally adjusted\n"
    )
    table = (
        "date,FL313020005.Q,FU313020005.Q,FL999000000.Q\n"
        "1945:Q4,26937,ND,10\n"
        "2025:Q4,830000,-1200,20\n"
        "2026:Q1,896092,18306,30\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("data_dictionary/F2_2_s.txt", dictionary)
        archive.writestr("csv/F2_2_s.csv", table)
    return buffer.getvalue()


def _client(package: bytes, counters: dict[str, int]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("z1_csv_files.zip")
        counters[request.method] = counters.get(request.method, 0) + 1
        headers = {"Last-Modified": "Thu, 11 Jun 2026 16:00:11 GMT"}
        if request.method == "HEAD":
            return httpx.Response(200, headers=headers, request=request)
        return httpx.Response(200, content=package, headers=headers, request=request)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_z1_code_normalization() -> None:
    assert normalize_z1_code("FL313020005") == "FL313020005.Q"
    assert normalize_z1_code("fl313020005.q") == "FL313020005.Q"
    assert normalize_z1_code("BOGZ1FL313020005Q") == "FL313020005.Q"
    assert normalize_z1_code("BOGZ1LM153061105A") == "LM153061105.A"


def test_z1_kind_enforcement_refuses_mismatch(tmp_path: Path) -> None:
    """The whole point of z1_series: an FL level must never be served as a
    flow (or vice versa), because level changes include valuation and
    reclassification effects that are not transactions."""
    counters: dict[str, int] = {}
    client = _client(_package_bytes(), counters)
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        with pytest.raises(ValueError, match=r"refusing to present FL313020005\.Q as FU"):
            service.z1_series("FL313020005.Q", "FU")
        with pytest.raises(ValueError, match="seasonally adjusted annualized"):
            service.z1_series("FA313020005.Q", "FU")
        with pytest.raises(ValueError, match="kind must be FL"):
            service.z1_series("FL313020005.Q", "XX")
    client.close()


def test_z1_cataloged_series_uses_package_and_caches(tmp_path: Path) -> None:
    counters: dict[str, int] = {}
    client = _client(_package_bytes(), counters)
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        first = service.z1_series("BOGZ1FL313020005Q", "FL", start="2025-01-01")
        second = service.z1_series("FL313020005.Q", "FL", start="2025-01-01")

    assert counters.get("GET") == 1
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert first["dataset"]["id"] == "z1.federal_checkable_deposits_level"
    assert first["preview"]["row_count"] == 2
    assert first["preview"]["date_coverage"] == {"start": "2025-12-31", "end": "2026-03-31"}
    assert first["provenance"]["vintage"] == "z1-release:2026-06-11"
    assert first["artifact"]["mime_type"] == "text/csv"
    assert first["artifact"] == second["artifact"]
    assert "full_data_path" not in first
    assert "manifest_path" not in first
    assert {item["id"] for item in first["caveats"]} >= {"z1.fl.level_not_flow"}
    client.close()


def test_z1_uncataloged_series_synthesizes_entry_from_dictionary(tmp_path: Path) -> None:
    counters: dict[str, int] = {}
    client = _client(_package_bytes(), counters)
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        result = service.z1_series("FL999000000", "FL")
        with pytest.raises(DatasetNotFoundError, match="not present in release"):
            service.z1_series("FL000000000.Q", "FL")

    assert result["dataset"]["id"] == "z1.fl999000000_q"
    assert result["dataset"]["title"] == "Example uncataloged series"
    assert result["preview"]["row_count"] == 3
    assert {item["id"] for item in result["caveats"]} == {
        "z1.fl.level_not_flow",
        "z1.revisions",
    }
    client.close()
