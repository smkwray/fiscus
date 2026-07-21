from __future__ import annotations

import json
from pathlib import Path

import pytest

from fiscus.catalog import Catalog, catalog_schema_json
from fiscus.caveats import CaveatContext, binding_caveats
from fiscus.errors import CatalogError
from fiscus.service import FiscusService


def test_bundled_catalog_loads_and_search_ranks_semantics() -> None:
    catalog = Catalog.load()
    assert len(catalog.entries) == 29
    assert all(not source.startswith("/") for source in catalog.sources)
    with FiscusService(catalog=catalog) as service:
        result = service.find_series("TGA Wednesday level")
    assert result["results"][0]["id"] == "fred.wdtgal"
    assert result["results"][0]["caveat_count"] >= 1
    assert result["results"][0]["units"]["scale"] == "millions"


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
