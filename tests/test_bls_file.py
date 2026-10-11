from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from fiscus.catalog import Catalog
from fiscus.errors import BrowserDownloadOnlyError, InvalidRequestError, SourceRequestError
from fiscus.executors.bls import _period_end_date, _request_windows
from fiscus.models import CatalogEntry
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


def test_bls_month_end_handles_gregorian_leap_years() -> None:
    assert _period_end_date("2000", "M02") == "2000-02-29"
    assert _period_end_date("2024", "M02") == "2024-02-29"
    assert _period_end_date("2100", "M02") == "2100-02-28"


def test_bls_preserves_preliminary_footnotes_in_artifact_and_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BLS_API_KEY", raising=False)
    footnotes = [{"code": "P", "text": "Preliminary."}, {"code": "R", "text": "Revised."}]
    payload = {
        "status": "REQUEST_SUCCEEDED",
        "Results": {"series": [{
            "seriesID": "CES0500000003",
            "data": [{
                "year": "2026", "period": "M08", "periodName": "August",
                "value": "36.12", "footnotes": footnotes,
            }],
        }]},
    }
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=payload, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        first = service.get_series("bls.ces0500000003")
        cached = service.get_series("bls.ces0500000003")
        rows = list(service.iter_csv_rows(first["artifact"]["cache_key"]))

    assert calls == 1
    assert json.loads(first["preview"]["head"][0]["footnotes"]) == footnotes
    assert json.loads(rows[0]["footnotes"]) == footnotes
    assert cached["preview"] == first["preview"]
    assert cached["cache_hit"] is True
    client.close()


def test_bls_rejects_wrong_series_in_successful_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BLS_API_KEY", raising=False)
    payload = json.loads(json.dumps(BLS_PAYLOAD))
    payload["Results"]["series"][0]["seriesID"] = "CES0500000003"
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload, request=request)
    ))
    with (
        FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service,
        pytest.raises(SourceRequestError, match="different series"),
    ):
        service.get_series("bls.lns14000000")
    client.close()


def test_bls_end_before_catalog_history_still_forms_a_valid_window() -> None:
    entry = Catalog.load().get("bls.lns14000000")

    assert _request_windows(entry, None, "1900-01-31", keyed=False) == [(1900, 1900)]


def test_bls_future_start_only_fails_before_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BLS_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid BLS year range must not reach the source")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    start = f"{datetime.now(UTC).year + 1}-01-01"
    with (
        FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service,
        pytest.raises(InvalidRequestError, match="current UTC year") as captured,
    ):
        service.get_series("bls.lns14000000", start=start)
    assert captured.value.code == "invalid_request"
    client.close()


def test_bls_end_only_request_chunks_history_and_excludes_annual_averages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BLS_API_KEY", raising=False)
    base = Catalog.load().get("bls.lns14000000").model_dump(mode="json")
    base["id"] = "bls.end_only_fixture"
    base["history"]["start"] = "2010-01-31"
    entry = CatalogEntry.model_validate(base)
    catalog = Catalog([entry], ["test:bls-end-only"])
    requested: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        first = request.url.params["startyear"]
        last = request.url.params["endyear"]
        requested.append((first, last))
        data = (
            [
                {"year": "2019", "period": "M01", "periodName": "January", "value": "4.0"},
                {"year": "2019", "period": "M13", "periodName": "Annual", "value": "4.1"},
            ]
            if first == "2010"
            else [
                {"year": "2024", "period": "M02", "periodName": "February", "value": "4.2"},
                {"year": "2024", "period": "M13", "periodName": "Annual", "value": "4.3"},
            ]
        )
        payload = {
            "status": "REQUEST_SUCCEEDED",
            "Results": {"series": [{"seriesID": entry.endpoint.series_id, "data": data}]},
        }
        return httpx.Response(200, json=payload, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with FiscusService(
        catalog=catalog,
        data_dir=tmp_path,
        http_client=client,
        sleep=lambda _: None,
    ) as service:
        result = service.get_series(entry.id, end="2024-02-29")

    assert requested == [("2010", "2019"), ("2020", "2024")]
    assert [row["date"] for row in result["preview"]["head"]] == [
        "2019-01-31",
        "2024-02-29",
    ]
    assert result["provenance"]["source_metadata"]["year_windows"] == [
        {"start": 2010, "end": 2019},
        {"start": 2020, "end": 2024},
    ]
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
        parsed_bytes = service.read_bytes(parsed["artifact"]["cache_key"])
        raw_bytes = service.read_bytes(raw["artifact"]["cache_key"])

    assert parsed["preview"]["row_count"] == 2
    assert parsed["preview"]["head"][0]["baseline_date"] == "1984-02-01"
    assert parsed["artifact"]["filename"] == "data.csv"
    assert parsed["artifact"]["mime_type"] == "text/csv"
    assert len(parsed_rows) == 2
    assert parsed_bytes == csv_bytes

    assert raw["preview"]["row_count"] == 0
    assert raw["artifact"]["filename"] == "hqmeom_qh_pars.xls"
    assert raw["artifact"]["mime_type"] == "application/vnd.ms-excel"
    assert raw_bytes == xls_bytes
    assert raw["artifact"]["byte_count"] == len(xls_bytes)
    assert raw["provenance"]["byte_count"] == len(xls_bytes)
    assert "full_data_path" not in raw
    assert "manifest_path" not in raw
    client.close()


@pytest.mark.parametrize("content", [
    b"baseline_date,value,value\n2026-01-01,1,2\n",
    b"baseline_date,value\n2026-01-01,1,2\n",
    b"<!doctype html>\n<html>Access denied</html>\n",
    b"baseline_date,value\n2026-01-01,\xff\n",
])
def test_file_executor_rejects_lossy_or_wrong_tabular_representation(
    tmp_path: Path, content: bytes
) -> None:
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=content, request=request)
    ))
    with (
        FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service,
        pytest.raises(SourceRequestError, match="malformed csv"),
    ):
        service.get_file("cbo.baselines_github")
    client.close()


def test_remote_tsv_preserves_representation_and_round_trips_rows(tmp_path: Path) -> None:
    base = Catalog.load().get("cbo.baselines_github").model_dump(mode="json")
    base["id"] = "cbo.remote_tsv_fixture"
    base["title"] = "Remote TSV fixture"
    base["endpoint"]["url"] = "https://example.test/data.tsv"
    base["response"]["format"] = "tsv"
    base["response"]["date_fields"] = ["date"]
    entry = CatalogEntry.model_validate(base)
    catalog = Catalog([entry], ["test:remote-tsv"])
    source = b"date\tvalue\n2026-01-31\t1\n2026-02-28\t2\n"
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            content=source,
            headers={
                "content-type": "text/tab-separated-values",
                "etag": '"remote-tsv"',
            },
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with FiscusService(
        catalog=catalog,
        data_dir=tmp_path,
        http_client=client,
        sleep=lambda _: None,
    ) as service:
        first = service.get_file(entry.id)
        cached = service.get_file(entry.id)
        rows = list(service.iter_csv_rows(first["artifact"]["cache_key"]))
        exact = service.read_bytes(first["artifact"]["cache_key"])

    assert calls == 1
    assert first["cache_hit"] is False
    assert cached["cache_hit"] is True
    assert first["artifact"] == cached["artifact"]
    assert first["artifact"]["filename"] == "data.tsv"
    assert first["artifact"]["mime_type"] == "text/tab-separated-values"
    assert first["preview"]["columns"] == ["date", "value"]
    assert first["preview"]["date_coverage"] == {
        "start": "2026-01-31",
        "end": "2026-02-28",
    }
    assert rows == [
        {"date": "2026-01-31", "value": "1"},
        {"date": "2026-02-28", "value": "2"},
    ]
    assert exact == source
    client.close()


def test_logical_release_file_identity_keeps_exact_bytes_and_content_revisions(
    tmp_path: Path,
) -> None:
    base = Catalog.load().get("cbo.baselines_github").model_dump(mode="json")
    base["id"] = "cbo.logical_release_fixture"
    base["title"] = "Logical release fixture"
    base["endpoint"]["url"] = "https://example.test/releases/table.csv"
    base["response"]["date_fields"] = ["date"]
    base["response"]["vintage_strategy"] = "logical_release_plus_sha256"
    base["release"] = {
        "logical_id": "cbo.example.2026-07",
        "representation": "csv",
        "published_on": "2026-07-01",
        "corrected_on": "2026-07-15",
        "discovery_url": "https://example.test/releases/",
        "layout_url": "https://example.test/releases/layout",
        "errata_url": "https://example.test/releases/errata",
    }
    entry = CatalogEntry.model_validate(base)
    catalog = Catalog([entry], ["test:logical-release"])
    first_bytes = b"\xef\xbb\xbfdate,value\r\n2026-01-01,1\r\n"
    corrected_bytes = b"\xef\xbb\xbfdate,value\r\n2026-01-01,2\r\n"
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = first_bytes if calls == 1 else corrected_bytes
        return httpx.Response(
            200,
            content=payload,
            headers={
                "Content-Type": "text/csv",
                "ETag": '"unchanged-transport-validator"',
                "Last-Modified": "Wed, 15 Jul 2026 12:00:00 GMT",
            },
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with FiscusService(
        catalog=catalog,
        data_dir=tmp_path,
        http_client=client,
        sleep=lambda _: None,
    ) as service:
        first = service.get_file(entry.id)
        corrected = service.get_file(entry.id, refresh=True)
        repeated = service.get_file(entry.id, refresh=True)
        assert service.read_bytes(first["artifact"]["cache_key"]) == first_bytes
        assert service.read_bytes(corrected["artifact"]["cache_key"]) == corrected_bytes

    assert calls == 3
    assert first["preview"]["head"][0]["value"] == "1"
    assert corrected["preview"]["head"][0]["value"] == "2"
    assert first["artifact"]["cache_key"] != corrected["artifact"]["cache_key"]
    assert corrected["artifact"]["cache_key"] == repeated["artifact"]["cache_key"]
    assert first["provenance"]["vintage"].startswith("cbo.example.2026-07|csv|sha256:")
    assert corrected["provenance"]["vintage"].startswith("cbo.example.2026-07|csv|sha256:")
    metadata = corrected["provenance"]["source_metadata"]
    assert metadata["identity_strategy"] == "logical_release_plus_sha256"
    assert metadata["release"] == base["release"]
    assert metadata["content_sha256"] == corrected["artifact"]["sha256"]
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
