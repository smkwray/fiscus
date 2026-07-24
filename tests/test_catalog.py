from __future__ import annotations

import json
from importlib.metadata import version
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from fiscus import __version__
from fiscus.catalog import Catalog, catalog_schema_json
from fiscus.caveats import CaveatContext, binding_caveats
from fiscus.errors import CatalogError
from fiscus.models import CatalogEntry
from fiscus.service import FiscusService


def test_bundled_catalog_loads_and_search_ranks_semantics() -> None:
    catalog = Catalog.load()
    assert len(catalog.entries) == 33
    assert all(not source.startswith("/") for source in catalog.sources)
    with FiscusService(catalog=catalog) as service:
        result = service.find_series("TGA Wednesday level")
    assert result["results"][0]["id"] == "fred.wdtgal"
    assert result["results"][0]["caveat_count"] >= 1
    assert result["results"][0]["vintage_support"] == {
        "supported": True,
        "mode": "alfred_as_of",
        "parameter": "vintage_as_of",
        "requires_env": "FRED_API_KEY",
        "coverage": "series-specific; not preflighted",
        "semantics": "information available to ALFRED as of a closed past date",
    }
    assert result["results"][0]["units"]["scale"] == "millions"


def test_package_version_is_unique_release_version() -> None:
    assert __version__ == "0.3.0"
    assert version("fiscus") == "0.3.0"


def test_cataloged_saar_flow_levels_always_explain_period_conversion() -> None:
    saar_entries = [
        entry
        for entry in Catalog.load().entries
        if entry.units.annualized is True and entry.units.unit_class.endswith("_saar")
    ]
    assert saar_entries
    for entry in saar_entries:
        caveats = binding_caveats(entry, CaveatContext())
        assert any("divide by" in item["message"].casefold() for item in caveats), entry.id


def test_curated_direct_files_bind_release_identity_and_machine_access() -> None:
    catalog = Catalog.load()
    ids = {
        "census.popest.v2025.nc_est2025_agesex_res",
        "census.popproj.2023.d1_main",
        "bls.empsit.2020_01_10.usdl_20_0010.pdf",
        "bls.empsit.2020_01_10.usdl_20_0010.html",
    }
    entries = [catalog.get(dataset_id) for dataset_id in ids]

    assert {entry.access.mode.value for entry in entries} == {"machine"}
    assert {entry.access.executor.value for entry in entries} == {"file"}
    assert {
        entry.response.vintage_strategy.value for entry in entries
    } == {"logical_release_plus_sha256"}
    assert all(entry.release is not None for entry in entries)
    assert {
        entry.release.representation for entry in entries if entry.release is not None
    } == {"csv", "pdf", "html"}
    assert {
        entry.release.logical_id
        for entry in entries
        if entry.release is not None and entry.family == "bls"
    } == {"bls.empsit.2020-01-10.usdl-20-0010"}

    with FiscusService(catalog=catalog) as service:
        found = service.find_series("NC-EST2025-AGESEX-RES")
    assert found["results"][0]["release"]["logical_id"] == (
        "census.popest.v2025.nc-est2025-agesex-res"
    )


def test_dts_seam_caveat_binds_only_when_range_crosses() -> None:
    with FiscusService() as service:
        crossing = service.source_caveats(
            "fiscaldata.dts_operating_cash_balance",
            start="2022-03-31",
            end="2022-04-02",
        )
        after = service.source_caveats(
            "fiscaldata.dts_operating_cash_balance",
            start="2022-05-01",
            end="2022-05-31",
        )
        boundary_onward = service.source_caveats(
            "fiscaldata.dts_operating_cash_balance",
            start="2022-04-01",
            end="2022-04-30",
        )
    crossing_by_id = {item["id"]: item["binds"] for item in crossing["caveats"]}
    after_by_id = {item["id"]: item["binds"] for item in after["caveats"]}
    boundary_by_id = {item["id"]: item["binds"] for item in boundary_onward["caveats"]}
    assert crossing_by_id["fiscaldata.dts.schema_seam_2022_04"] is True
    assert after_by_id["fiscaldata.dts.schema_seam_2022_04"] is False
    assert boundary_by_id["fiscaldata.dts.schema_seam_2022_04"] is False


def test_bea_annualization_and_unit_mixing_are_separate_caveats() -> None:
    with FiscusService() as service:
        bare = service.source_caveats("bea.nipa_t10106")
        monthly = service.source_caveats("bea.pce_underlying_u20405")
        mixed = service.source_caveats(
            "bea.nipa_t10106",
            unit_classes=["usd_millions_nsa"],
        )

    bare_by_id = {item["id"]: item["binds"] for item in bare["caveats"]}
    monthly_by_id = {item["id"]: item for item in monthly["caveats"]}
    mixed_by_id = {item["id"]: item["binds"] for item in mixed["caveats"]}
    assert bare_by_id["bea.saar_units"] is True
    assert bare_by_id["bea.saar_unit_mixing"] is None
    assert monthly_by_id["bea.saar_units"]["binds"] is True
    assert "divide by twelve" in monthly_by_id["bea.saar_units"]["message"].casefold()
    assert mixed_by_id["bea.saar_units"] is True
    assert mixed_by_id["bea.saar_unit_mixing"] is True


def test_overlay_deep_merges_mappings_and_replaces_lists(tmp_path: Path) -> None:
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        """\
schema_version: 1
family: fred
entries:
  - id: fred.wdtgal
    aliases:
      - local point-in-time TGA
    refresh:
      notes: Locally reviewed release timing.
""",
        encoding="utf-8",
    )
    catalog = Catalog.load(extra_paths=[overlay])
    entry = catalog.get("fred.wdtgal")
    assert entry.aliases == ["local point-in-time TGA"]
    assert entry.refresh.cadence == "weekly"
    assert entry.refresh.notes == "Locally reviewed release timing."


def test_generated_schema_is_valid_json() -> None:
    rendered = catalog_schema_json()
    schema = json.loads(rendered)
    assert schema["title"] == "CatalogFile"
    assert "schema_version" in schema["properties"]
    committed = Path(__file__).parents[1] / "catalog" / "schema.json"
    assert committed.read_text(encoding="utf-8") == rendered


def test_executor_tool_and_browser_mode_contracts_match_runtime_and_schema() -> None:
    catalog = Catalog.load()
    fred = catalog.get("fred.wdtgal").model_dump(mode="json")
    fred["tools"] = ["get_file"]
    with pytest.raises(ValidationError, match=r"must declare exactly tools: \[get_series\]"):
        CatalogEntry.model_validate(fred)

    duplicate = catalog.get("fred.wdtgal").model_dump(mode="json")
    duplicate["tools"] = ["get_series", "get_series"]
    with pytest.raises(ValidationError, match=r"must declare exactly tools: \[get_series\]"):
        CatalogEntry.model_validate(duplicate)

    browser = next(
        entry
        for entry in catalog.entries
        if entry.access.executor.value == "browser_download"
    ).model_dump(mode="json")
    browser["access"]["mode"] = "machine"
    with pytest.raises(ValidationError, match="browser_download_only mode"):
        CatalogEntry.model_validate(browser)

    schema = json.loads(catalog_schema_json())
    document = {"schema_version": 1, "family": "fred", "entries": [fred]}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, schema)


def test_overlay_rejects_file_family_mismatch(tmp_path: Path) -> None:
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        """\
schema_version: 1
family: fred
entries:
  - id: fiscaldata.example
    family: fiscaldata
""",
        encoding="utf-8",
    )
    with pytest.raises(CatalogError, match="does not match file family"):
        Catalog.load(extra_paths=[overlay])


def test_catalog_rejects_inert_vintage_strategy_patch(tmp_path: Path) -> None:
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        """\
schema_version: 1
family: fred
entries:
  - id: fred.wdtgal
    response:
      vintage_strategy: retrieval
""",
        encoding="utf-8",
    )
    with pytest.raises(CatalogError, match="fred_realtime_or_http"):
        Catalog.load(extra_paths=[overlay])
