from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from fiscus.catalog import Catalog
from fiscus.errors import (
    InvalidCredentialError,
    MissingKeyError,
    SourceQueryRejectedError,
    SourceRequestError,
)
from fiscus.executors.json_api import bea_period_end
from fiscus.models import CatalogEntry
from fiscus.service import FiscusService

BEA_INVENTORY_PAYLOAD = {
    "BEAAPI": {
        "Results": {
            "ParamValue": [
                {
                    "TableName": "T10106",
                    "Description": "Table 1.1.6. Real Gross Domestic Product (A) (Q)",
                }
            ]
        }
    }
}

BEA_PAYLOAD = {
    "BEAAPI": {
        "Results": {
            "Data": [
                {
                    "TableName": "T10106",
                    "SeriesCode": "A191RX",
                    "LineNumber": "1",
                    "TimePeriod": "2025Q2",
                    "DataValue": "23,687,553",
                },
                {
                    "TableName": "T10106",
                    "SeriesCode": "A191RX",
                    "LineNumber": "1",
                    "TimePeriod": "2025Q1",
                    "DataValue": "23,548,210",
                },
            ]
        }
    }
}

BEA_CREDENTIAL_ERROR_PAYLOADS = [
    (
        "4",
        "This UserId is not active. Please activate it and try again.",
        {
            "BEAAPI": {
                "Results": {
                    "Error": {
                        "APIErrorCode": "4",
                        "APIErrorDescription": (
                            "This UserId is not active. Please activate it and try again."
                        ),
                    }
                }
            }
        },
    ),
    (
        "1",
        "Invalid Request - Invalid API UserId.",
        {
            "BEAAPI": {
                "Results": {
                    "Error": {
                        "APIErrorCode": "1",
                        "APIErrorDescription": "Invalid Request - Invalid API UserId.",
                    }
                }
            }
        },
    ),
]

BEA_QUERY_ERROR_PAYLOADS = [
    (
        "Invalid TableName was requested.",
        {
            "BEAAPI": {
                "Error": {
                    "APIErrorDescription": "Error retrieving NIPA data.",
                    "APIErrorCode": "201",
                    "ErrorDetail": {"Description": "Invalid TableName was requested."},
                }
            }
        },
    ),
    (
        "Data for this table and frequency are not currently available.",
        {
            "BEAAPI": {
                "Error": {
                    "APIErrorDescription": "Error retrieving NIPA data.",
                    "APIErrorCode": "201",
                    "ErrorDetail": {
                        "Description": (
                            "Data for this table and frequency are not currently available. "
                            "Please check BEAs release schedule for more information."
                        )
                    },
                }
            }
        },
    ),
]

OFR_PAYLOAD = [["2026-04-30", 8351000000000.12], ["2026-05-31", 8400986581507.48]]


def test_custom_credential_is_absent_from_success_and_cached_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "synthetic-private-source-key"
    monkeypatch.setenv("SYNTHETIC_SOURCE_API_KEY", secret)
    raw = Catalog.load().get("ofr.mmf_total_assets").model_dump(mode="python")
    raw["endpoint"]["url"] = "https://example.test/data"
    raw["endpoint"]["api_key_param"] = "CustomCredential"
    raw["access"]["auth"] = {"mode": "required", "env_var": "SYNTHETIC_SOURCE_API_KEY"}
    catalog = Catalog([CatalogEntry.model_validate(raw)], ["synthetic-overlay"])
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.params["CustomCredential"] == secret
        return httpx.Response(
            200,
            json=[{"date": "2026-05-31", "value": "1", secret: secret}],
            headers={"etag": secret},
            request=request,
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        FiscusService(
            catalog=catalog, data_dir=tmp_path, http_client=client, sleep=lambda _: None,
        ) as service,
    ):
        first = service.get_series("ofr.mmf_total_assets")
        repeated = service.get_series("ofr.mmf_total_assets")
        key = first["artifact"]["cache_key"]
        assert "CustomCredential=REDACTED" in first["provenance"]["source_url"]
        assert repeated["cache_hit"] is True
        assert secret.encode() not in service.read_bytes(key)
        assert secret not in json.dumps([first, repeated, service.provenance(key)])
    assert calls == 1


def test_bea_period_end_normalization() -> None:
    assert bea_period_end("2025Q1") == "2025-03-31"
    assert bea_period_end("2025M03") == "2025-03-31"
    assert bea_period_end("2000M02") == "2000-02-29"
    assert bea_period_end("2024M02") == "2024-02-29"
    assert bea_period_end("2100M02") == "2100-02-28"
    assert bea_period_end("2025") == "2025-12-31"
    assert bea_period_end("garbage") == ""
    assert bea_period_end("oopsQ1") == ""


def test_bea_requires_key_and_redacts_it(tmp_path: Path, monkeypatch) -> None:
    """BEA is a required-key family: without BEA_API_KEY the call must fail
    with the env-var name, and with one the key must never appear in
    recorded provenance."""
    secret = "b" * 36

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["UserID"] == secret
        if request.url.params["method"] == "GetParameterValues":
            return httpx.Response(200, json=BEA_INVENTORY_PAYLOAD, request=request)
        assert request.url.params["TableName"] == "T10106"
        assert request.url.params["Year"] != "X"
        payload = json.loads(json.dumps(BEA_PAYLOAD))
        payload["BEAAPI"]["Request"] = {
            "RequestParam": [
                {"ParameterName": "USERID", "ParameterValue": secret},
                {"ParameterName": "TABLENAME", "ParameterValue": "T10106"},
            ]
        }
        return httpx.Response(200, content=json.dumps(payload), request=request)

    monkeypatch.delenv("BEA_API_KEY", raising=False)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        with pytest.raises(MissingKeyError, match="BEA_API_KEY is unset") as absent:
            service.get_series("bea.nipa_t10106")
        assert absent.value.code == "missing_key"

        monkeypatch.setenv("BEA_API_KEY", "")
        with pytest.raises(MissingKeyError, match="BEA_API_KEY is unset") as empty:
            service.get_series("bea.nipa_t10106")
        assert empty.value.code == "missing_key"

        monkeypatch.setenv("BEA_API_KEY", secret)
        result = service.get_series(
            "bea.nipa_t10106",
            start="2025-01-01",
            end="2025-12-31",
        )
        bounded = service.get_series(
            "bea.nipa_t10106",
            start="2025-04-01",
            end="2025-12-31",
        )
        repeated = service.get_series(
            "bea.nipa_t10106",
            start="2025-04-01",
            end="2025-12-31",
        )
        canonical = service.bea_nipa_table("T10106", "Q", 2025, 2025)
        artifact_bytes = service.read_bytes(result["artifact"]["cache_key"])
        stored_contract = service.provenance(result["artifact"]["cache_key"])
        bounded_bytes = service.read_bytes(bounded["artifact"]["cache_key"])
        bounded_rows = list(service.iter_csv_rows(bounded["artifact"]["cache_key"]))
        bounded_manifest = service.provenance(bounded["artifact"]["cache_key"])["manifest"]
        canonical_rows = list(service.iter_csv_rows(canonical["artifact"]["cache_key"]))

    assert secret not in json.dumps(result)
    assert secret.encode() not in artifact_bytes
    assert secret not in json.dumps(stored_contract)
    assert secret not in result["provenance"]["source_url"]
    assert "REDACTED" in result["provenance"]["source_url"]
    # bea_period normalization: rows sorted by synthesized ISO date
    assert [row["date"] for row in result["preview"]["head"]] == ["2025-03-31", "2025-06-30"]
    bounded_dates = [row["date"] for row in bounded["preview"]["head"]]
    assert bounded_dates == ["2025-06-30"]
    assert all("2025-04-01" <= value <= "2025-12-31" for value in bounded_dates)
    assert bounded["preview"]["row_count"] == 1
    assert bounded["preview"]["date_coverage"] == {
        "start": "2025-06-30",
        "end": "2025-06-30",
    }
    assert bounded["provenance"]["row_count"] == 1
    assert bounded["provenance"]["data_coverage"] == bounded["preview"]["date_coverage"]
    assert bounded["observed"]["coverage"]["table"] == {
        "first_observed_period_any_line": "2025Q2",
        "last_observed_period_any_line": "2025Q2",
        "row_count": 1,
        "line_count": 1,
    }
    assert bounded["artifact"]["cache_key"] != result["artifact"]["cache_key"]
    assert [row["date"] for row in bounded_rows] == ["2025-06-30"]
    assert len(bounded_rows) == bounded["preview"]["row_count"]
    assert bounded["artifact"]["sha256"] == hashlib.sha256(bounded_bytes).hexdigest()
    assert bounded["provenance"]["sha256"] == bounded["artifact"]["sha256"]
    assert bounded_manifest["row_count"] == len(bounded_rows)
    assert bounded_manifest["params"]["start"] == "2025-04-01"
    assert bounded_manifest["params"]["end"] == "2025-12-31"
    assert bounded["provenance"]["source_artifact"] == canonical["artifact"]
    assert bounded["provenance"]["source_request"] == canonical["provenance"]["source_request"]
    assert bounded["provenance"]["transform"] == {
        "operation": "inclusive_calendar_date_filter",
        "field": "date",
        "start": "2025-04-01",
        "end": "2025-12-31",
        "values_changed": False,
    }
    assert [row["date"] for row in canonical_rows] == ["2025-03-31", "2025-06-30"]
    assert canonical["preview"]["row_count"] == 2
    assert bounded["cache_hit"] is False
    assert repeated["cache_hit"] is True
    assert repeated["artifact"] == bounded["artifact"]
    caveats = {item["id"]: item for item in result["caveats"]}
    assert set(caveats) >= {
        "bea.nipa_observed_rows",
        "bea.saar_units",
        "bea.datavalue_commas",
    }
    assert "bea.saar_unit_mixing" not in caveats
    assert caveats["bea.saar_units"]["severity"] == "critical"
    assert "Divide by four" in caveats["bea.saar_units"]["message"]
    client.close()


@pytest.mark.parametrize(("start", "end", "expected_dates"), [
    (None, None, ["2025-03-31", "2025-06-30"]),
    (None, "2025-04-01", ["2025-03-31"]),
    ("2025-04-01", None, ["2025-06-30"]),
    ("2025-04-01", "2025-04-15", []),
])
def test_bea_alias_artifact_preserves_open_or_empty_date_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    start: str | None,
    end: str | None,
    expected_dates: list[str],
) -> None:
    monkeypatch.setenv("BEA_API_KEY", "test-bea-key")

    def handler(request: httpx.Request) -> httpx.Response:
        payload = (
            BEA_INVENTORY_PAYLOAD
            if request.url.params["method"] == "GetParameterValues"
            else BEA_PAYLOAD
        )
        return httpx.Response(200, json=payload, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        first = service.get_series("bea.nipa_t10106", start=start, end=end)
        repeated = service.get_series("bea.nipa_t10106", start=start, end=end)
        rows = list(service.iter_csv_rows(first["artifact"]["cache_key"]))
        receipt = service.provenance(first["artifact"]["cache_key"])
    assert [row["date"] for row in rows] == expected_dates
    assert len(rows) == first["preview"]["row_count"] == first["provenance"]["row_count"]
    assert receipt["manifest"]["row_count"] == len(rows)
    assert first["cache_hit"] is False
    assert repeated["cache_hit"] is True
    assert first["artifact"] == repeated["artifact"]
    if start is not None or end is not None:
        assert first["provenance"]["transform"]["start"] == start
        assert first["provenance"]["transform"]["end"] == end
        assert first["provenance"]["source_artifact"]["sha256"]
    else:
        assert "transform" not in first["provenance"]
    client.close()


@pytest.mark.parametrize(
    ("api_error_code", "expected_detail", "recorded_payload"),
    BEA_CREDENTIAL_ERROR_PAYLOADS,
)
def test_bea_rejected_key_is_a_credential_fault(
    api_error_code: str,
    expected_detail: str,
    recorded_payload: dict[str, object],
    tmp_path: Path,
    monkeypatch,
) -> None:
    secret = "inactive-bea-key"
    monkeypatch.setenv("BEA_API_KEY", secret)
    payload = json.loads(json.dumps(recorded_payload))
    payload["BEAAPI"]["Request"] = {
        "RequestParam": [{"ParameterName": "USERID", "ParameterValue": secret}]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with (
        FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service,
        pytest.raises(InvalidCredentialError, match="BEA_API_KEY") as captured,
    ):
        service.get_series("bea.nipa_t10106")

    assert captured.value.code == "credential_rejected"
    assert f"APIErrorCode {api_error_code}" in str(captured.value)
    assert expected_detail in str(captured.value)
    assert secret not in str(captured.value)
    client.close()


@pytest.mark.parametrize(("expected_detail", "payload"), BEA_QUERY_ERROR_PAYLOADS)
def test_bea_query_errors_surface_code_and_specific_detail(
    expected_detail: str,
    payload: dict[str, object],
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("BEA_API_KEY", "valid-bea-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with (
        FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service,
        pytest.raises(SourceQueryRejectedError, match=expected_detail) as captured,
    ):
        service.get_series("bea.nipa_t10106")

    assert captured.value.code == "source_query_rejected"
    assert "APIErrorCode 201" in str(captured.value)
    client.close()


def test_json_api_pair_rows_and_start_param(tmp_path: Path) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, content=json.dumps(OFR_PAYLOAD), request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        result = service.get_series("ofr.mmf_total_assets", start="2026-04-01")

    assert seen["mnemonic"] == "MMF-MMF_TOT-M"
    assert seen["start_date"] == "2026-04-01"
    assert result["preview"]["row_count"] == 2
    assert result["preview"]["head"][0] == {"date": "2026-04-30", "value": "8351000000000.12"}
    client.close()


def test_json_api_rejects_undeclared_params(tmp_path: Path) -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
    with (
        FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service,
        pytest.raises(SourceRequestError, match="does not accept parameter"),
    ):
        service.json_api.fetch(
            service.catalog.get("ofr.mmf_total_assets"),
            params={"mnemonic": "SOMETHING-ELSE"},
        )
    client.close()


@pytest.mark.parametrize("start", [None, "2026-10-08"])
def test_json_api_date_bounds_include_source_timestamp_on_end_date(
    tmp_path: Path, start: str | None
) -> None:
    payload = [
        {"auctionDate": "2026-10-08T00:00:00", "totalAccepted": "1"},
        {"auctionDate": "2026-10-09T00:00:00", "totalAccepted": "2"},
        {"auctionDate": "", "totalAccepted": "3"},
        {"auctionDate": "bad-date", "totalAccepted": "4"},
    ]
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload, request=request)
    ))
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        result = service.get_series(
            "treasurydirect.auctioned_securities", start=start, end="2026-10-08"
        )
    assert result["preview"]["row_count"] == 1
    assert result["preview"]["head"][0]["auctionDate"] == "2026-10-08T00:00:00"
    client.close()
