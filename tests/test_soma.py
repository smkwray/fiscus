from __future__ import annotations

import json
from pathlib import Path

import httpx

from fiscus.service import FiscusService

ASOF_PAYLOAD = {"soma": {"asOfDates": ["2026-07-08", "2026-07-01", "2026-06-24"]}}

HOLDINGS_PAYLOAD = {
    "soma": {
        "holdings": [
            {
                "asOfDate": "2026-07-08",
                "cusip": "912797RF6",
                "securityType": "Bills",
                "maturityDate": "2026-07-09",
                "parValue": "16617842100",
                "coupon": None,
            },
            {
                "asOfDate": "2026-07-08",
                "cusip": "91282CAV3",
                "securityType": "NotesBonds",
                "maturityDate": "2030-11-15",
                "parValue": "22016536500",
                "coupon": "0.875",
            },
        ]
    }
}


def _client(counters: dict[str, int]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        counters[path] = counters.get(path, 0) + 1
        if path.endswith("asofdates/list.json"):
            return httpx.Response(200, content=json.dumps(ASOF_PAYLOAD), request=request)
        assert path.endswith("/asof/2026-07-08.json")
        return httpx.Response(200, content=json.dumps(HOLDINGS_PAYLOAD), request=request)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_soma_latest_asof_resolves_before_cache(tmp_path: Path) -> None:
    """ "Latest" is a mutable alias; caching under it would freeze the first
    snapshot forever. The resolved as-of date must be the cache identity."""
    counters: dict[str, int] = {}
    client = _client(counters)
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        first = service.soma_holdings()
        second = service.soma_holdings()

    assert counters["/api/soma/asofdates/list.json"] == 2
    assert counters["/api/soma/tsy/get/all/asof/2026-07-08.json"] == 1
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert first["provenance"]["vintage"] == "soma-asof:2026-07-08"
    assert first["preview"]["row_count"] == 2
    assert first["artifact"]["mime_type"] == "text/csv"
    assert first["artifact"] == second["artifact"]
    assert "full_data_path" not in first
    assert "manifest_path" not in first
    client.close()


def test_soma_cusip_filter_and_explicit_asof(tmp_path: Path) -> None:
    counters: dict[str, int] = {}
    client = _client(counters)
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        result = service.soma_holdings(asof="2026-07-08", cusip="912797rf6")

    assert "/api/soma/asofdates/list.json" not in counters
    assert result["preview"]["row_count"] == 1
    assert result["preview"]["head"][0]["cusip"] == "912797RF6".replace("RF6", "RF6")
    assert result["provenance"]["source_metadata"]["cusip_filter"] == "912797RF6"
    assert {item["id"] for item in result["caveats"]} >= {"soma.weekly_snapshot"}
    client.close()
