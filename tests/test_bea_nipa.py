from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from fiscus.catalog import Catalog
from fiscus.errors import (
    InvalidCredentialError,
    InvalidRequestError,
    SourceQueryRejectedError,
    SourceSchemaChangedError,
)
from fiscus.service import FiscusService, bea_nipa_coverage, normalize_bea_nipa_inventory

INVENTORY = [
    {
        "TableName": "T10101",
        "Description": "Table 1.1.1. Percent Change From Preceding Period in Real GDP (A) (Q)",
    },
    {
        "TableName": "T10106",
        "Description": "Table 1.1.6. Real Gross Domestic Product, Chained Dollars (A) (Q)",
    },
    {
        "TableName": "T11400",
        "Description": "Table 1.14. Gross Value Added of Domestic Corporate Business (A) (Q)",
    },
    {
        "TableName": "T20200B",
        "Description": "Table 2.2B. Wages and Salaries by Industry (A) (Q)",
    },
    {
        "TableName": "T20600",
        "Description": "Table 2.6. Personal Income and Its Disposition, Monthly (M)",
    },
    {
        "TableName": "T10306",
        "Description": "Table 1.3.6. Real Gross Value Added by Sector (A) (Q)",
    },
    {
        "TableName": "T31003",
        "Description": "Table 3.10.3. Real Government Consumption and Investment (Q)",
    },
    {
        "TableName": "T31006",
        "Description": "Table 3.10.6. Real Government Consumption and Investment (Q)",
    },
]


def inventory_payload(secret: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"BEAAPI": {"Results": {"ParamValue": INVENTORY}}}
    if secret:
        payload["BEAAPI"]["Request"] = {
            "RequestParam": [{"ParameterName": "USERID", "ParameterValue": secret}]
        }
    return payload


def row(
    table: str,
    line: str,
    period: str,
    *,
    series: str | None = None,
    metric: str = "Current Dollars",
    cl_unit: str = "Level",
    unit_mult: str = "6",
) -> dict[str, str]:
    return {
        "TableName": table,
        "SeriesCode": series or f"S{line}",
        "LineNumber": line,
        "LineDescription": f"Line {line}",
        "TimePeriod": period,
        "METRIC_NAME": metric,
        "CL_UNIT": cl_unit,
        "UNIT_MULT": unit_mult,
        "DataValue": "1,000",
    }


def data_payload(rows: Any, *, secret: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "BEAAPI": {
            "Results": {
                "Statistic": "NIPA Table",
                "UTCProductionTime": "2026-07-26T00:00:00.000",
                "Dimensions": [{"Name": "TableName"}],
                "Notes": [{"NoteText": f"safe note {secret or ''}"}],
                "Data": rows,
            }
        }
    }
    if secret:
        payload["BEAAPI"]["Request"] = {
            "RequestParam": [
                {"ParameterName": "USERID", "ParameterValue": secret},
                {"ParameterName": "TABLENAME", "ParameterValue": "T10106"},
            ]
        }
    return payload


def mock_client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_bea_nipa_sentinel_secret_is_absent_from_every_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "SENTINEL-BEA-SECRET-DO-NOT-LEAK"
    monkeypatch.setenv("BEA_API_KEY", secret)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["method"] == "GetParameterValues":
            return httpx.Response(200, json=inventory_payload(secret), request=request)
        if request.url.params["TableName"] == "T31003":
            return httpx.Response(
                200,
                json={
                    "BEAAPI": {
                        "Request": {
                            "RequestParam": [
                                {"ParameterName": "USERID", "ParameterValue": secret}
                            ]
                        },
                        "Results": {
                            "Error": {
                                "APIErrorCode": "1",
                                "APIErrorDescription": (
                                    f"Invalid Request - Invalid API UserId. {secret}"
                                ),
                            }
                        },
                    }
                },
                request=request,
            )
        return httpx.Response(
            200,
            json=data_payload(
                [
                    row(
                        "T10106",
                        "1",
                        "2024Q1",
                        metric="Chained Dollars",
                    )
                ],
                secret=secret,
            ),
            request=request,
        )

    client = mock_client(handler)
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        result = service.bea_nipa_table("T10106", "Q", 2024, 2024)
        cache_key = result["artifact"]["cache_key"]
        artifact_bytes = service.read_bytes(cache_key)
        provenance = service.provenance(cache_key)
        with pytest.raises(InvalidCredentialError) as captured:
            service.bea_nipa_table("T31003", "Q", 2024, 2024)

    serialized_result = json.dumps(result, sort_keys=True)
    serialized_provenance = json.dumps(provenance, sort_keys=True)
    assert secret not in serialized_result
    assert secret.encode() not in artifact_bytes
    assert secret not in serialized_provenance
    assert secret not in str(captured.value)
    assert secret not in caplog.text
    assert "RequestParam" not in serialized_result
    assert "RequestParam" not in serialized_provenance
    assert "catalog_verified_on" not in provenance["manifest"]
    assert result["provenance"]["source_metadata"]["bea_api"]["Notes"][0][
        "NoteText"
    ].endswith("REDACTED")
    client.close()


def test_t10106_annual_and_quarterly_coverage_differ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEA_API_KEY", "valid-key")
    responses = {
        "A": [
            row("T10106", "1", "1929", metric="Chained Dollars"),
            row("T10106", "2", "1967", metric="Chained Dollars"),
            row("T10106", "3", "1970", metric="Chained Dollars"),
            row("T10106", "4", "2007", metric="Chained Dollars"),
        ],
        "Q": [
            row("T10106", "1", "1947Q1", metric="Chained Dollars"),
            row("T10106", "3", "1970Q1", metric="Chained Dollars"),
            row("T10106", "4", "2007Q1", metric="Chained Dollars"),
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["method"] == "GetParameterValues":
            return httpx.Response(200, json=inventory_payload(), request=request)
        return httpx.Response(
            200,
            json=data_payload(responses[request.url.params["Frequency"]]),
            request=request,
        )

    client = mock_client(handler)
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        annual = service.bea_nipa_table("T10106", "A", 1929, 2026)
        quarterly = service.bea_nipa_table("T10106", "Q", 1947, 2026)

    annual_by_line = annual["observed"]["coverage"]["by_line"]
    quarterly_by_line = quarterly["observed"]["coverage"]["by_line"]
    assert annual_by_line != quarterly_by_line
    assert annual["observed"]["coverage"]["table"]["first_observed_period_any_line"] == "1929"
    assert quarterly["observed"]["coverage"]["table"][
        "first_observed_period_any_line"
    ] == "1947Q1"
    client.close()


def test_table_extents_and_mixed_metrics_are_observed_not_invented(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEA_API_KEY", "valid-key")
    table_rows = {
        "T31003": [row("T31003", "1", "1947Q1"), row("T31003", "1", "2026Q1")],
        "T31006": [row("T31006", "1", "2007Q1"), row("T31006", "1", "2026Q1")],
        "T11400": [
            row("T11400", "1", "2024", metric="Current Dollars"),
            row("T11400", "2", "2024", metric="Chained Dollars"),
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["method"] == "GetParameterValues":
            return httpx.Response(200, json=inventory_payload(), request=request)
        return httpx.Response(
            200,
            json=data_payload(table_rows[request.url.params["TableName"]]),
            request=request,
        )

    client = mock_client(handler)
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        early = service.bea_nipa_table("T31003", "Q", 1947, 2026)
        late = service.bea_nipa_table("T31006", "Q", 1947, 2026)
        mixed = service.bea_nipa_table("T11400", "A", 2024, 2024)

    assert early["observed"]["coverage"]["table"]["first_observed_period_any_line"] == "1947Q1"
    assert late["observed"]["coverage"]["table"]["first_observed_period_any_line"] == "2007Q1"
    assert mixed["observed"]["metric_names"] == ["Chained Dollars", "Current Dollars"]
    assert mixed["observed"]["unit_labels"] == ["Level"]
    assert mixed["observed"]["unit_multipliers"] == ["6"]
    assert "bea.nipa_quantity_chained_concepts" in {
        caveat["id"] for caveat in mixed["caveats"]
    }
    client.close()


def test_advertised_annual_quarterly_monthly_and_explicit_year_lists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEA_API_KEY", "valid-key")
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["method"] == "GetParameterValues":
            return httpx.Response(200, json=inventory_payload(), request=request)
        params = dict(request.url.params)
        seen.append(params)
        period = {"A": "2023", "Q": "2023Q1", "M": "2023M01"}[params["Frequency"]]
        return httpx.Response(
            200,
            json=data_payload([row(params["TableName"], "1", period)]),
            request=request,
        )

    client = mock_client(handler)
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        service.bea_nipa_table("T10101", "A", 2023, 2024)
        service.bea_nipa_table("T10101", "Q", 2023, 2024)
        service.bea_nipa_table("T20200B", "A", 2023, 2024)
        service.bea_nipa_table("T20600", "M", 2023, 2024)

    assert {params["Frequency"] for params in seen} == {"A", "Q", "M"}
    assert {params["Year"] for params in seen} == {"2023,2024"}
    assert {params["datasetname"] for params in seen} == {"NIPA"}
    assert all(params["Year"] != "X" for params in seen)
    client.close()


def test_invalid_requests_and_inventory_miss_refresh_before_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"inventory": 0, "data": 0}
    monkeypatch.setenv("BEA_API_KEY", "valid-key")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["method"] == "GetParameterValues":
            calls["inventory"] += 1
            return httpx.Response(200, json=inventory_payload(), request=request)
        calls["data"] += 1
        raise AssertionError("invalid inventory pair must not reach GetData")

    client = mock_client(handler)
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        with pytest.raises(InvalidRequestError):
            service.bea_nipa_table("bad", "Q", 2024, 2024)
        with pytest.raises(InvalidRequestError):
            service.bea_nipa_table("T10106", "W", 2024, 2024)
        with pytest.raises(InvalidRequestError):
            service.bea_nipa_table("T10106", "Q", 2025, 2024)
        assert calls == {"inventory": 0, "data": 0}
        with pytest.raises(SourceQueryRejectedError, match="does not advertise frequency") as exc:
            service.bea_nipa_table("T20600", "Q", 2024, 2024)
        assert exc.value.code == "source_query_rejected"

    assert calls == {"inventory": 2, "data": 0}
    client.close()


def test_valid_empty_response_and_schema_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEA_API_KEY", "valid-key")
    mode = {"value": "empty"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["method"] == "GetParameterValues":
            return httpx.Response(200, json=inventory_payload(), request=request)
        if mode["value"] == "empty":
            return httpx.Response(200, json=data_payload([]), request=request)
        if mode["value"] == "missing":
            return httpx.Response(200, json={"BEAAPI": {"Results": {}}}, request=request)
        return httpx.Response(
            200,
            json={"BEAAPI": {"Results": {"Data": {"not": "a list"}}}},
            request=request,
        )

    client = mock_client(handler)
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        empty = service.bea_nipa_table("T31003", "Q", 1900, 1900)
        assert empty["preview"]["row_count"] == 0
        assert empty["observed"]["empty_response"] is True
        assert empty["observed"]["coverage"]["table"] == {
            "first_observed_period_any_line": None,
            "last_observed_period_any_line": None,
            "row_count": 0,
            "line_count": 0,
        }
        mode["value"] = "missing"
        with pytest.raises(SourceSchemaChangedError) as missing:
            service.bea_nipa_table("T31003", "Q", 1901, 1901, refresh=True)
        assert missing.value.code == "source_schema_changed"
        mode["value"] = "object"
        with pytest.raises(SourceSchemaChangedError):
            service.bea_nipa_table("T31003", "Q", 1902, 1902, refresh=True)
    client.close()


def test_duplicate_line_period_and_conflicting_series_codes_fail_closed() -> None:
    duplicate = [row("T10106", "1", "2024Q1"), row("T10106", "1", "2024Q1")]
    with pytest.raises(SourceSchemaChangedError, match="repeats line 1 period 2024Q1"):
        bea_nipa_coverage(duplicate, "T10106")

    conflicting = [
        row("T10106", "1", "2024Q1", series="ONE"),
        row("T10106", "1", "2024Q2", series="TWO"),
    ]
    with pytest.raises(SourceSchemaChangedError, match="conflicting SeriesCode"):
        bea_nipa_coverage(conflicting, "T10106")


def test_cache_identity_and_curation_overlay_do_not_change_source_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEA_API_KEY", "valid-key")
    requests: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["method"] == "GetParameterValues":
            return httpx.Response(200, json=inventory_payload(), request=request)
        params = dict(request.url.params)
        requests.append(params)
        period = "2024" if params["Frequency"] == "A" else "2024Q1"
        return httpx.Response(
            200,
            json=data_payload(
                [row("T10106", "1", period, metric="Chained Dollars")]
            ),
            request=request,
        )

    full_catalog = Catalog.load()
    uncurated_catalog = Catalog(
        [entry for entry in full_catalog.entries if entry.id != "bea.nipa_t10106"],
        full_catalog.sources,
    )
    client = mock_client(handler)
    with FiscusService(
        catalog=uncurated_catalog,
        data_dir=tmp_path,
        http_client=client,
        sleep=lambda _: None,
    ) as service:
        uncurated = service.bea_nipa_table("T10106", "Q", 2024, 2024)
        annual = service.bea_nipa_table("T10106", "A", 2024, 2024)
        different_year = service.bea_nipa_table("T10106", "Q", 2023, 2024)

    offline = mock_client(
        lambda request: (_ for _ in ()).throw(AssertionError("cache hit must not call source"))
    )
    with FiscusService(
        catalog=full_catalog,
        data_dir=tmp_path,
        http_client=offline,
        sleep=lambda _: None,
    ) as service:
        curated = service.bea_nipa_table("T10106", "Q", 2024, 2024)

    assert len(
        {
            uncurated["artifact"]["cache_key"],
            annual["artifact"]["cache_key"],
            different_year["artifact"]["cache_key"],
        }
    ) == 3
    assert uncurated["dataset"]["curation"]["status"] == "uncurated"
    assert curated["dataset"]["curation"]["status"] == "curated"
    assert curated["catalog"]["id"] == "bea.nipa_t10106"
    assert curated["artifact"] == uncurated["artifact"]
    assert curated["cache_hit"] is True
    assert "catalog_verified_on" not in curated["provenance"]
    assert len(requests) == 3
    client.close()
    offline.close()


def test_integral_years_are_accepted_and_fractional_years_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEA_API_KEY", "valid-key")
    calls = {"inventory": 0, "data": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["method"] == "GetParameterValues":
            calls["inventory"] += 1
            return httpx.Response(200, json=inventory_payload(), request=request)
        calls["data"] += 1
        return httpx.Response(
            200,
            json=data_payload([row("T10106", "1", "2024Q1")]),
            request=request,
        )

    client = mock_client(handler)
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        accepted = service.bea_nipa_table("T10106", "Q", 2024.0, 2024.0)
        with pytest.raises(InvalidRequestError, match="four-digit integers") as fractional:
            service.bea_nipa_table("T10106", "Q", 2024.9, 2025.1)

    assert accepted["preview"]["row_count"] == 1
    assert fractional.value.code == "invalid_request"
    assert calls == {"inventory": 1, "data": 1}
    client.close()


def test_inventory_skips_malformed_rows_without_blocking_tables_or_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEA_API_KEY", "valid-key")
    inventory = [
        {
            "Key": "T10106",
            "Desc": "Table 1.1.6. Real Gross Domestic Product, Chained Dollars (A) (Q)",
        },
        {
            "TableName": "T10306",
            "Description": "Table 1.3.6. Real Gross Value Added by Sector (A) (Q)",
        },
        {"TableName": "not-a-table", "Description": "provider drift row"},
    ]
    data_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["method"] == "GetParameterValues":
            return httpx.Response(
                200,
                json={"BEAAPI": {"Results": {"ParamValue": inventory}}},
                request=request,
            )
        table_id = request.url.params["TableName"]
        data_calls.append(table_id)
        return httpx.Response(
            200,
            json=data_payload([row(table_id, "1", "2024Q1")]),
            request=request,
        )

    client = mock_client(handler)
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        t10106 = service.bea_nipa_table("T10106", "Q", 2024, 2024)
        t10306 = service.bea_nipa_table("T10306", "Q", 2024, 2024)
        discovery = service.find_series("T10306", family="bea")

    assert data_calls == ["T10106", "T10306"]
    for result in (t10106, t10306):
        diagnostics = result["dataset"]["source_reported"]["inventory_diagnostics"]
        assert diagnostics["skipped_row_count"] == 1
        assert diagnostics["sample_issues"][0]["row_index"] == 2
    assert discovery["uncurated_upstream"][0]["table_id"] == "T10306"
    assert discovery["uncurated_upstream_diagnostics"]["skipped_row_count"] == 1
    assert "skipped 1 malformed source row" in discovery["note"]
    client.close()


def test_inventory_raises_schema_changed_only_when_every_row_is_unusable() -> None:
    with pytest.raises(SourceSchemaChangedError) as captured:
        normalize_bea_nipa_inventory(
            [
                {"TableName": "bad", "Description": "no frequencies"},
                {"Key": "", "Desc": ""},
            ]
        )

    assert captured.value.code == "source_schema_changed"
    message = str(captured.value)
    assert "no usable table rows" in message
    assert "skipped_rows=2" in message
    assert "TableName/Key" in message


def test_find_series_uses_cached_bea_inventory_without_promoting_source_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEA_API_KEY", "valid-key")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["method"] == "GetParameterValues"
        return httpx.Response(200, json=inventory_payload(), request=request)

    client = mock_client(handler)
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        found = service.find_series("NIPA 3.10.3", family="bea")

    assert len(found["uncurated_upstream"]) == 1
    result = found["uncurated_upstream"][0]
    assert result["table_id"] == "T31003"
    assert result["tools"] == ["bea_nipa_table"]
    assert result["curation"] == {"status": "uncurated", "catalog_id": None}
    assert {"history_start", "units", "verified_on"}.isdisjoint(result)
    assert "platform Fiscus SQLite cache" in found["note"]

    monkeypatch.delenv("BEA_API_KEY", raising=False)
    offline = mock_client(
        lambda request: (_ for _ in ()).throw(AssertionError("cached discovery must be keyless"))
    )
    with FiscusService(data_dir=tmp_path, http_client=offline, sleep=lambda _: None) as service:
        cached = service.find_series("T31003", family="bea")
    assert cached["uncurated_upstream"][0]["table_id"] == "T31003"
    client.close()
    offline.close()
