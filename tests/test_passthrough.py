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


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("units", "Billions of Chained 2017 Dollars"),
        ("frequency", "Monthly"),
        ("seasonal_adjustment", "Not Seasonally Adjusted"),
    ],
)
def test_current_fred_cache_binds_units_seasonality_and_frequency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, replacement: str,
) -> None:
    monkeypatch.setenv("FRED_API_KEY", "k" * 32)
    info = {
        "id": "EXAMPLE",
        "title": "Example real series",
        "units": "Billions of Chained 2012 Dollars",
        "frequency": "Quarterly",
        "seasonal_adjustment": "Seasonally Adjusted Annual Rate",
        "observation_start": "1947-01-01",
    }
    observation_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observation_calls
        if request.url.path == "/fred/series":
            return httpx.Response(200, json={"seriess": [info]}, request=request)
        assert request.url.path == "/fred/series/observations"
        observation_calls += 1
        return httpx.Response(
            200,
            json={"observations": [{"date": "2024-01-01", "value": str(observation_calls)}]},
            headers={"etag": '"unchanged-observation-validator"'},
            request=request,
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service,
    ):
        first = service.get_series("fred.example")
        old_key = first["artifact"]["cache_key"]
        old_bytes = service.read_bytes(old_key)
        old_provenance = service.provenance(old_key)
        info[field] = replacement
        second = service.get_series("fred.example")
        third = service.get_series("fred.example")
        assert second["cache_hit"] is False
        assert third["cache_hit"] is True
        assert second["preview"]["head"][0]["value"] == "2"
        assert first["artifact"]["cache_key"] != second["artifact"]["cache_key"]
        assert third["artifact"] == second["artifact"]
        assert first["dataset"]["units"]["label"] == "Billions of Chained 2012 Dollars"
        assert first["dataset"]["frequency"] == "Quarterly"
        assert service.provenance(old_key) == old_provenance
        assert service.read_bytes(old_key) == old_bytes
        metadata = old_provenance["manifest"]["params"]["series_metadata"]
        assert metadata["units"]["label"] == "Billions of Chained 2012 Dollars"
        assert metadata["frequency"] == "Quarterly"
        interpretation = second["provenance"]["source_metadata"]["series_interpretation"]
        assert interpretation["units"] == second["dataset"]["units"]
        assert interpretation["frequency"] == second["dataset"]["frequency"]
        if field == "frequency":
            caveat = next(
                item for item in second["caveats"] if item["id"] == "fiscus.annualized_flow"
            )
            assert "Divide by 12" in caveat["message"]
        elif field == "seasonal_adjustment":
            assert second["dataset"]["units"]["annualized"] is False
        else:
            assert second["dataset"]["units"]["label"] == replacement
    assert observation_calls == 2


@pytest.mark.parametrize("initially_available", [True, False])
def test_current_fred_cache_rechecks_observations_when_metadata_availability_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initially_available: bool,
) -> None:
    monkeypatch.setenv("FRED_API_KEY", "k" * 32)
    available = initially_available
    observation_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observation_calls
        if request.url.path == "/fred/series":
            return httpx.Response(
                200,
                json=FRED_INFO if available else {"seriess": []},
                request=request,
            )
        observation_calls += 1
        return httpx.Response(
            200,
            json={"observations": [{"date": "2024-01-01", "value": str(observation_calls)}]},
            request=request,
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service,
    ):
        first = service.get_series("fred.cpiaucsl")
        available = not available
        second = service.get_series("fred.cpiaucsl")
        third = service.get_series("fred.cpiaucsl")
        assert second["cache_hit"] is False
        assert second["preview"]["head"][0]["value"] == "2"
        assert third["cache_hit"] is True
        assert first["artifact"]["cache_key"] != second["artifact"]["cache_key"]
        assert (second["dataset"]["units"]["annualized"] is None) == (not available)
        assert (first["dataset"]["units"]["annualized"] is None) == (not initially_available)
    assert observation_calls == 2


def test_alfred_chained_dollar_snapshots_do_not_inherit_current_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # BEA changed the reference year from 2012 to 2017 on 2023-09-28.
    # Current FRED metadata is not evidence of the old snapshot's units.
    monkeypatch.setenv("FRED_API_KEY", "k" * 32)
    values = {"2023-07-27": "8939.984", "2023-10-26": "10023.101"}
    observation_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observation_calls
        if request.url.path == "/fred/series":
            return httpx.Response(
                200,
                json={"seriess": [{
                    "id": "PCESVC96",
                    "title": "Real Personal Consumption Expenditures: Services",
                    "units": "Billions of Chained 2017 Dollars",
                    "frequency": "Quarterly",
                    "seasonal_adjustment": "Seasonally Adjusted Annual Rate",
                    "observation_start": "1947-01-01",
                }]},
                request=request,
            )
        observation_calls += 1
        vintage = request.url.params["realtime_start"]
        return httpx.Response(
            200,
            json={
                "realtime_start": vintage,
                "realtime_end": vintage,
                "observation_start": "2023-04-01",
                "observation_end": "2023-04-01",
                "units": "lin",
                "output_type": 1,
                "sort_order": "asc",
                "count": 1,
                "offset": 0,
                "limit": 100_000,
                "observations": [{
                    "date": "2023-04-01",
                    "value": values[vintage],
                    "realtime_start": vintage,
                    "realtime_end": vintage,
                }],
            },
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        results = [
            service.get_series(
                "fred.pcesvc96", start="2023-04-01", end="2023-04-01", vintage_as_of=vintage
            )
            for vintage in values
        ]
        cached = service.get_series(
            "fred.pcesvc96", start="2023-04-01", end="2023-04-01", vintage_as_of="2023-07-27"
        )

    assert observation_calls == 2
    for result, (vintage, value) in zip(results, values.items(), strict=True):
        assert result["preview"]["head"][0]["value"] == value
        units = result["dataset"]["units"]
        assert units["label"].startswith(f"unknown for ALFRED as-of {vintage}")
        assert "current series metadata: Billions of Chained 2017 Dollars" in units["label"]
        assert units["annualized"] is None
        assert units["scale"] is None
        assert units["seasonal_adjustment"] is None
        caveats = {item["id"]: item for item in result["caveats"]}
        assert caveats["fred.alfred_units_not_vintage_verified"]["severity"] == "critical"
        assert "fiscus.annualized_flow" not in caveats
    assert cached["cache_hit"] is True
    assert cached["dataset"]["units"] == results[0]["dataset"]["units"]
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


def test_preview_cap_applies_to_repeated_and_many_preferred_fields() -> None:
    from fiscus.return_format import PREVIEW_COLUMN_CAP, preview_rows_for_fields

    row = {f"field{i}": str(i) for i in range(100)}
    preferred = ["field99", "field99", *row]

    head, tail, truncated = preview_rows_for_fields([row], preferred)

    assert len(head[0]) == PREVIEW_COLUMN_CAP
    assert next(iter(head[0])) == "field99"
    assert tail == []
    assert truncated == {"shown": PREVIEW_COLUMN_CAP, "total": len(row)}
