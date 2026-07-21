from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from fiscus.errors import MissingKeyError, SourceRequestError
from fiscus.executors.json_api import bea_period_end
from fiscus.service import FiscusService

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

OFR_PAYLOAD = [["2026-04-30", 8351000000000.12], ["2026-05-31", 8400986581507.48]]


def test_bea_period_end_normalization() -> None:
    assert bea_period_end("2025Q1") == "2025-03-31"
    assert bea_period_end("2025M03") == "2025-03-31"
    assert bea_period_end("2025") == "2025-12-31"
    assert bea_period_end("garbage") == ""


def test_bea_requires_key_and_redacts_it(tmp_path: Path, monkeypatch) -> None:
    """BEA is a required-key family: without BEA_API_KEY the call must fail
    with the env-var name, and with one the key must never appear in
    recorded provenance."""
    secret = "b" * 36

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["UserID"] == secret
        assert request.url.params["TableName"] == "T10106"
        return httpx.Response(200, content=json.dumps(BEA_PAYLOAD), request=request)

    monkeypatch.delenv("BEA_API_KEY", raising=False)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        with pytest.raises(MissingKeyError, match="BEA_API_KEY"):
            service.get_series("bea.nipa_t10106")

        monkeypatch.setenv("BEA_API_KEY", secret)
        result = service.get_series("bea.nipa_t10106", start="2025-01-01")

    assert secret not in result["provenance"]["source_url"]
    assert "REDACTED" in result["provenance"]["source_url"]
    # bea_period normalization: rows sorted by synthesized ISO date
    assert [row["date"] for row in result["preview"]["head"]] == ["2025-03-31", "2025-06-30"]
    caveats = {item["id"]: item for item in result["caveats"]}
    assert set(caveats) >= {"bea.saar_units", "bea.datavalue_commas"}
    assert "bea.saar_unit_mixing" not in caveats
    assert caveats["bea.saar_units"]["severity"] == "critical"
    assert "Divide by four" in caveats["bea.saar_units"]["message"]
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
