from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from fiscus.errors import BrowserDownloadOnlyError
from fiscus.service import FiscusService

BLS_PAYLOAD = {
    "status": "REQUEST_SUCCEEDED",
    "Results": {
        "series": [
            {
                "seriesID": "LNS14000000",
                "data": [
                    {"year": "2026", "period": "M02", "periodName": "February", "value": "4.1"},
                    {"year": "2026", "period": "M01", "periodName": "January", "value": "4.2"},
                    {"year": "2025", "period": "M13", "periodName": "Annual", "value": "4.0"},
                ],
            }
        ]
    },
}


def test_bls_synthesizes_period_end_dates(tmp_path: Path, monkeypatch) -> None:
    """BLS reports year+period codes, not dates. Rows must gain period-end
    ISO dates, and annual-average periods (M13) must carry an empty date so
    they can never be silently averaged into a monthly series."""
    monkeypatch.delenv("BLS_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/data/LNS14000000")
        return httpx.Response(200, content=json.dumps(BLS_PAYLOAD), request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        result = service.get_series("bls.lns14000000")

    rows = result["preview"]["head"]
    by_period = {row["period"]: row["date"] for row in rows}
    assert by_period["M01"] == "2026-01-31"
    assert by_period["M02"] == "2026-02-28"
    assert by_period["M13"] == ""
    assert {item["id"] for item in result["caveats"]} >= {"bls.year_window_limit"}
    client.close()


def test_file_executor_parses_csv_and_binary_passthrough(tmp_path: Path) -> None:
    csv_bytes = b"baseline_date,component,value\n1984-02-01,debt,1330.0\n2026-02-01,debt,30100.5\n"
    xls_bytes = b"\xd0\xcf\x11\xe0not-really-a-workbook"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("baselines.csv"):
            return httpx.Response(200, content=csv_bytes, request=request)
        return httpx.Response(200, content=xls_bytes, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        parsed = service.get_file("cbo.baselines_github")
        raw = service.get_file("treasury.hqm_yield_curve_pars")
        parsed_rows = list(service.iter_csv_rows(parsed["artifact"]["cache_key"]))
        raw_bytes = service.read_bytes(raw["artifact"]["cache_key"])

    assert parsed["preview"]["row_count"] == 2
    assert parsed["preview"]["head"][0]["baseline_date"] == "1984-02-01"
    assert parsed["artifact"]["filename"] == "data.csv"
    assert parsed["artifact"]["mime_type"] == "text/csv"
    assert len(parsed_rows) == 2

    assert raw["preview"]["row_count"] == 0
    assert raw["artifact"]["filename"] == "hqmeom_qh_pars.xls"
    assert raw["artifact"]["mime_type"] == "application/vnd.ms-excel"
    assert raw_bytes == xls_bytes
    assert raw["artifact"]["byte_count"] == len(xls_bytes)
    assert raw["provenance"]["byte_count"] == len(xls_bytes)
    assert "full_data_path" not in raw
    assert "manifest_path" not in raw
    client.close()


def test_browser_download_only_entries_diagnose_instead_of_fetching(tmp_path: Path) -> None:
    """The catalog marks sources that 403-block agents (bls.gov tables,
    cbo.gov workbooks). Fetching them must fail with the explicit diagnosis,
    not a retry loop that masquerades as an outage."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("browser-download-only entries must never hit the network")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        with pytest.raises(BrowserDownloadOnlyError, match="browser-download-only"):
            service.get_file("bls.cpi_relative_importance")
        with pytest.raises(BrowserDownloadOnlyError, match="browser-download-only"):
            service.get_file("cbo.budget_data_official")
    client.close()
