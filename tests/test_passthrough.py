from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from fiscus.errors import DatasetNotFoundError
from fiscus.service import FiscusService
from fiscus.unit_inference import infer_passthrough_units

FRED_INFO = {
    "seriess": [
        {
            "id": "CPIAUCSL",
            "title": "Consumer Price Index for All Urban Consumers: All Items in U.S. City Average",
            "units": "Index 1982-1984=100",
            "frequency": "Monthly",
            "seasonal_adjustment": "Seasonally Adjusted",
            "observation_start": "1947-01-01",
        }
    ]
}

FRED_SAAR_INFO = {
    "seriess": [
        {
            "id": "BOGZ1FA105000995Q",
            "title": "Nonfinancial Corporate Business; Net Lending (+) or Borrowing (-)",
            "units": "Millions of U.S. Dollars, Seasonally Adjusted Annual Rate",
            "frequency": "Quarterly, End of Period",
            "seasonal_adjustment": "Seasonally Adjusted Annual Rate",
            "observation_start": "1946-10-01",
        }
    ]
}

FRED_SAAR_OBS = {
    "realtime_end": "2026-07-13",
    "observations": [
        {
            "date": "2026-01-01",
            "value": "857839",
            "realtime_start": "",
            "realtime_end": "",
        },
    ],
}

FRED_OBS = {
    "realtime_end": "2026-07-13",
    "observations": [
        {"date": "2026-05-01", "value": "321.4", "realtime_start": "", "realtime_end": ""},
    ],
}

FRED_SEARCH = {
    "seriess": [
        {
            "id": "CPIAUCSL",
            "title": "Consumer Price Index for All Urban Consumers",
            "units": "Index 1982-1984=100",
            "frequency": "Monthly",
            "seasonal_adjustment_short": "SA",
            "observation_start": "1947-01-01",
        }
    ]
}


def test_uncataloged_fred_passthrough_with_metadata(tmp_path: Path, monkeypatch) -> None:
    """The catalog is for caveats, not coverage: any FRED id must be fetchable
    as fred.<ID>, labeled from source metadata, and tagged uncataloged so the
    missing curated review is explicit."""
    monkeypatch.setenv("FRED_API_KEY", "k" * 32)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fred/series":
            return httpx.Response(200, content=json.dumps(FRED_INFO), request=request)
        assert request.url.path == "/fred/series/observations"
        assert request.url.params["series_id"] == "CPIAUCSL"
        return httpx.Response(200, content=json.dumps(FRED_OBS), request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        result = service.get_series("fred.cpiaucsl", start="2026-01-01")

    assert result["dataset"]["id"] == "fred.cpiaucsl"
    assert result["dataset"]["title"].startswith("Consumer Price Index")
    assert result["dataset"]["units"] == {
        "label": "Index 1982-1984=100",
        "scale": None,
        "seasonal_adjustment": "Seasonally Adjusted",
        "annualized": False,
        "unit_class": "index_sa",
    }
    assert {item["id"] for item in result["caveats"]} == {
        "fiscus.uncataloged",
        "fred.missing_value_dot",
    }
    client.close()


def test_uncataloged_fred_passthrough_surfaces_saar(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FRED_API_KEY", "k" * 32)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fred/series":
            return httpx.Response(200, content=json.dumps(FRED_SAAR_INFO), request=request)
        assert request.url.path == "/fred/series/observations"
        return httpx.Response(200, content=json.dumps(FRED_SAAR_OBS), request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        result = service.get_series("fred.bogz1fa105000995q")

    units = result["dataset"]["units"]
    assert units["annualized"] is True
    assert units["seasonal_adjustment"] == "Seasonally Adjusted Annual Rate"
    assert units["scale"] == "millions"
    assert units["unit_class"] == "usd_millions_saar"
    caveats = {item["id"]: item for item in result["caveats"]}
    assert set(caveats) == {
        "fiscus.annualized_flow",
        "fiscus.uncataloged",
        "fred.missing_value_dot",
    }
    assert caveats["fiscus.annualized_flow"]["severity"] == "critical"
    assert "Divide by 4" in caveats["fiscus.annualized_flow"]["message"]
    client.close()


def test_annualized_percent_metadata_is_not_labeled_as_a_flow() -> None:
    units, caveat = infer_passthrough_units(
        label="Percent Change from Preceding Period, Seasonally Adjusted Annual Rate",
        seasonal_adjustment="Seasonally Adjusted Annual Rate",
        frequency="Quarterly",
        unknown_label="unused",
    )

    assert units.annualized is True
    assert units.unit_class == "percent_annual_rate"
    assert caveat is not None
    assert caveat.id == "fiscus.annualized_nonflow"
    assert "rather than blindly applying" in caveat.message


def test_monthly_annualized_flow_uses_divide_by_twelve() -> None:
    units, caveat = infer_passthrough_units(
        label="Million U.S. Dollars, Seasonally Adjusted Annual Rate",
        seasonal_adjustment="Seasonally Adjusted Annual Rate",
        frequency="Monthly",
        unknown_label="unused",
    )

    assert units.scale == "millions"
    assert units.unit_class == "usd_millions_saar"
    assert caveat is not None
    assert caveat.id == "fiscus.annualized_flow"
    assert "Divide by 12" in caveat.message


def test_explicit_not_annualized_metadata_is_not_overridden() -> None:
    units, caveat = infer_passthrough_units(
        label="Millions of Dollars, Not Annualized",
        seasonal_adjustment="Not Seasonally Adjusted",
        frequency="Quarterly",
        unknown_label="unused",
    )

    assert units.annualized is False
    assert units.unit_class == "currency_millions_nsa"
    assert caveat is None


def test_seasonality_without_units_keeps_annualization_unknown() -> None:
    units, caveat = infer_passthrough_units(
        label=None,
        seasonal_adjustment="Seasonally Adjusted",
        frequency="Quarterly",
        unknown_label="units unavailable",
    )

    assert units.label == "units unavailable"
    assert units.seasonal_adjustment == "Seasonally Adjusted"
    assert units.annualized is None
    assert units.unit_class == "uncataloged_annualization_unknown"
    assert caveat is not None
    assert caveat.id == "fiscus.annualization_unknown"


def test_per_year_dollar_level_is_treated_as_an_annualized_flow() -> None:
    units, caveat = infer_passthrough_units(
        label="Millions of U.S. Dollars per Year",
        seasonal_adjustment="Not Seasonally Adjusted",
        frequency="Quarterly",
        unknown_label="unused",
    )

    assert units.annualized is True
    assert units.unit_class == "usd_millions_annualized"
    assert caveat is not None
    assert caveat.id == "fiscus.annualized_flow"
    assert "Divide by 4" in caveat.message


def test_keyless_fred_passthrough_marks_annualization_unknown(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    payload = b"observation_date,BOGZ1FA105000995Q\n2026-01-01,857839\n"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/graph/fredgraph.csv"
        return httpx.Response(200, content=payload, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        result = service.get_series("fred.bogz1fa105000995q")

    units = result["dataset"]["units"]
    assert units["annualized"] is None
    assert units["unit_class"] == "uncataloged_annualization_unknown"
    caveats = {item["id"]: item for item in result["caveats"]}
    assert "fiscus.annualization_unknown" in caveats
    assert "divide by 4" in caveats["fiscus.annualization_unknown"]["message"].casefold()
    assert "12 for monthly" in caveats["fiscus.annualization_unknown"]["message"]
    client.close()


def test_uncataloged_bls_passthrough_and_bad_ids(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("BLS_API_KEY", raising=False)
    payload = {
        "status": "REQUEST_SUCCEEDED",
        "Results": {
            "series": [
                {"data": [{"year": "2026", "period": "M01", "periodName": "Jan", "value": "7"}]}
            ]
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/data/CES0000000001")
        return httpx.Response(200, content=json.dumps(payload), request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        result = service.get_series("bls.ces0000000001")
        with pytest.raises(DatasetNotFoundError, match=r"fred\.<SERIES_ID>"):
            service.get_series("nonsense.id")
        with pytest.raises(DatasetNotFoundError):
            service.get_series("fred.no spaces allowed")

    assert result["dataset"]["id"] == "bls.ces0000000001"
    assert result["dataset"]["units"]["annualized"] is None
    assert result["dataset"]["units"]["unit_class"] == "uncataloged_annualization_unknown"
    assert {item["id"] for item in result["caveats"]} == {
        "fiscus.annualization_unknown",
        "fiscus.uncataloged",
        "bls.year_window_limit",
    }
    client.close()


def test_find_series_falls_back_to_upstream_search(tmp_path: Path, monkeypatch) -> None:
    """An empty find_series result must be a lead, not a dead end: with a key
    it returns FRED's own search results marked uncurated; without one it
    explains the passthrough form."""
    monkeypatch.setenv("FRED_API_KEY", "k" * 32)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fred/series/search"
        return httpx.Response(200, content=json.dumps(FRED_SEARCH), request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        keyed = service.find_series("consumer price index inflation")
    client.close()

    assert keyed["count"] == 0
    assert keyed["uncurated_upstream"][0]["id"] == "fred.cpiaucsl"
    assert keyed["uncurated_upstream"][0]["curated"] is False
    assert "no curated caveat review" in keyed["note"].casefold()

    monkeypatch.delenv("FRED_API_KEY", raising=False)
    offline = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: (_ for _ in ()).throw(AssertionError("keyless fallback must not call out"))
        )
    )
    with FiscusService(data_dir=tmp_path, http_client=offline, sleep=lambda _: None) as service:
        keyless = service.find_series("consumer price index inflation")
    offline.close()
    assert keyless["uncurated_upstream"] == []
    assert "fred.<SERIES_ID>" in keyless["note"]


def test_preview_caps_wide_rows(tmp_path: Path) -> None:
    """TreasuryDirect rows carry ~130 fields; previews must stay compact in
    MCP returns while the full CSV keeps every column."""
    wide_row = {f"field{i:03d}": str(i) for i in range(120)}
    wide_row["auctionDate"] = "2026-07-09T00:00:00"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps([wide_row]), request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        result = service.get_series("treasurydirect.auctioned_securities")
        full_rows = list(service.iter_csv_rows(result["artifact"]["cache_key"]))
    client.close()

    preview = result["preview"]
    assert preview["truncated_columns"]["total"] == 121
    assert preview["truncated_columns"]["shown"] == 24
    assert len(preview["head"][0]) == 24
    assert "auctionDate" in preview["head"][0]
    assert len(preview["columns"]) == 121
    assert len(full_rows[0]) == 121
    assert "full_data_path" not in result
    assert "manifest_path" not in result
