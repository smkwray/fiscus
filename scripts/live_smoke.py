from __future__ import annotations

import hashlib
import json
import os
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fiscus.service import FiscusService


def _alfred_identity(service: FiscusService) -> dict[str, object]:
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        return {"status": "skipped_no_key"}

    bounds = {"start": "2019-10-01", "end": "2020-04-01"}
    first = service.get_series(
        "fred.gdpc1",
        **bounds,
        vintage_as_of="2020-05-01",
        refresh=True,
    )
    second = service.get_series(
        "fred.gdpc1",
        **bounds,
        vintage_as_of="2020-08-01",
        refresh=True,
    )
    weekend = service.get_series(
        "fred.gdpc1",
        **bounds,
        vintage_as_of="2020-05-02",
        refresh=True,
    )
    empty = service.get_series(
        "fred.gdpc1",
        start="1900-01-01",
        end="1900-12-31",
        vintage_as_of="2020-05-01",
        refresh=True,
    )
    results = {
        "first": first,
        "second": second,
        "weekend": weekend,
        "empty": empty,
    }
    if api_key in json.dumps(results, sort_keys=True):
        raise RuntimeError("FRED_API_KEY leaked into the ALFRED response contract")

    expected_vintages = {
        "first": "alfred-as-of:2020-05-01",
        "second": "alfred-as-of:2020-08-01",
        "weekend": "alfred-as-of:2020-05-02",
        "empty": "alfred-as-of:2020-05-01",
    }
    observed_vintages = {
        name: result["provenance"]["vintage"] for name, result in results.items()
    }
    if observed_vintages != expected_vintages:
        raise RuntimeError(f"ALFRED vintage identity failed: {observed_vintages}")

    cache_keys = {
        name: result["artifact"]["cache_key"] for name, result in results.items()
    }
    if len(set(cache_keys.values())) != len(cache_keys):
        raise RuntimeError(f"ALFRED request identities collided: {cache_keys}")

    def numeric_rows(result: dict[str, object]) -> dict[str, str]:
        artifact = result["artifact"]
        if not isinstance(artifact, dict):
            raise RuntimeError("ALFRED response omitted its artifact descriptor")
        return {
            row["date"]: row["value"]
            for row in service.iter_csv_rows(str(artifact["cache_key"]))
        }

    first_rows = numeric_rows(first)
    second_rows = numeric_rows(second)
    weekend_rows = numeric_rows(weekend)
    if first_rows != weekend_rows:
        raise RuntimeError("arbitrary non-release date changed the ALFRED information set")
    common_dates = sorted(set(first_rows) & set(second_rows))
    revised_dates = [
        observation
        for observation in common_dates
        if first_rows[observation] != second_rows[observation]
    ]
    if not revised_dates:
        raise RuntimeError("known-revised ALFRED snapshots did not differ")

    empty_source = empty["provenance"]["source_metadata"]
    if not isinstance(empty_source, dict):
        raise RuntimeError("ALFRED empty response omitted source metadata")
    empty_pagination = empty_source.get("pagination")
    if (
        empty["preview"]["row_count"] != 0
        or not isinstance(empty_pagination, dict)
        or empty_pagination.get("count") != 0
        or empty_pagination.get("page_count") != 1
        or empty_pagination.get("offsets_fetched") != [0]
    ):
        raise RuntimeError(f"ALFRED empty-history contract failed: {empty}")

    second_source = second["provenance"]["source_metadata"]
    if not isinstance(second_source, dict):
        raise RuntimeError("ALFRED revised response omitted source metadata")
    pagination = second_source.get("pagination")
    if not isinstance(pagination, dict) or pagination.get("count") != len(second_rows):
        raise RuntimeError(f"ALFRED pagination receipt is inconsistent: {pagination}")

    return {
        "status": "passed",
        "dataset": "fred.gdpc1",
        "observation_bounds": bounds,
        "vintages": {
            "first": expected_vintages["first"],
            "second": expected_vintages["second"],
            "arbitrary_date": expected_vintages["weekend"],
        },
        "first_rows": first_rows,
        "second_rows": second_rows,
        "arbitrary_date_matches_first": True,
        "revised_common_dates": revised_dates,
        "pagination": pagination,
        "empty_history": {
            "bounds": {"start": "1900-01-01", "end": "1900-12-31"},
            "rows": 0,
            "pagination": empty_pagination,
        },
        "credential_absent_from_response": True,
        "distinct_cache_identities": True,
    }


def _z1_identity(service: FiscusService, data_dir: Path) -> dict[str, object]:
    discovery = service.find_series(
        "households and nonprofit organizations net worth revaluation",
        family="z1",
        limit=20,
    )
    discovered = [item.get("id") for item in discovery.get("uncurated_upstream", [])]
    expected_revaluation = "FR152090005.Q"
    if expected_revaluation not in discovered:
        raise RuntimeError(
            f"Z.1 package dictionary did not discover {expected_revaluation}: {discovered}"
        )

    kinds = ("FC", "FU", "FR", "FV")
    fetched = {
        kind: service.z1_series(f"{kind}152090005.Q", kind)
        for kind in kinds
    }
    vintages = {result["provenance"]["vintage"] for result in fetched.values()}
    if len(vintages) != 1:
        raise RuntimeError(f"Z.1 accumulation components have mixed vintages: {vintages}")
    package_metadata = next(iter(fetched.values()))["provenance"]["source_metadata"]
    release_date = package_metadata.get("release_date")
    package_sha256 = package_metadata.get("package_sha256")

    numeric_rows: dict[str, dict[str, Decimal]] = {}
    for kind, result in fetched.items():
        rows: dict[str, Decimal] = {}
        for row in service.iter_csv_rows(result["artifact"]["cache_key"]):
            try:
                rows[row["date"]] = Decimal(row["value"])
            except (InvalidOperation, KeyError):
                continue
        numeric_rows[kind] = rows
    common_dates = set.intersection(*(set(rows) for rows in numeric_rows.values()))
    if not common_dates:
        raise RuntimeError("Z.1 accumulation components have no common numeric observation")
    latest = max(common_dates)
    values = {kind: numeric_rows[kind][latest] for kind in kinds}
    residual = values["FC"] - values["FU"] - values["FR"] - values["FV"]
    # Source observations are published in whole millions. Component rounding
    # can therefore leave a one-million residual even when the identity holds.
    if abs(residual) > Decimal(1):
        raise RuntimeError(
            f"Z.1 accumulation identity failed at {latest}: values={values}, residual={residual}"
        )

    packages = list((data_dir / "packages" / "z1").glob("*.zip"))
    if len(packages) != 1:
        raise RuntimeError(f"expected one downloaded Z.1 package, found {len(packages)}")
    package = packages[0]
    observed_sha256 = hashlib.sha256(package.read_bytes()).hexdigest()
    if package_sha256 != observed_sha256:
        raise RuntimeError(
            f"Z.1 public package digest differs from downloaded bytes: "
            f"{package_sha256} != {observed_sha256}"
        )
    return {
        "release_date": release_date,
        "discovered": expected_revaluation,
        "common_observation": latest,
        "values": {kind: str(value) for kind, value in values.items()},
        "residual_millions": str(residual),
        "rounding_tolerance_millions": "1",
        "package_sha256": observed_sha256,
        "package_bytes": package.stat().st_size,
    }


def main() -> int:
    with (
        tempfile.TemporaryDirectory(prefix="fiscus-smoke-") as temporary,
        FiscusService(data_dir=Path(temporary)) as service,
    ):
        fred = service.get_series(
            "fred.wdtgal",
            start="2024-01-01",
            end="2024-01-31",
            refresh=True,
        )
        alfred = _alfred_identity(service)
        fiscaldata = service.fiscaldata_query(
            "fiscaldata.dts_operating_cash_balance",
            params={
                "fields": "record_date,account_type,close_today_bal",
                "filter": "record_date:gte:2024-01-02,record_date:lte:2024-01-05",
                "sort": "record_date,account_type",
                "page[size]": "100",
            },
            max_pages=1,
            refresh=True,
        )
        # SOMA date discovery is cheap. Z.1 deliberately downloads the current
        # package so discovery, parsing, semantics, and accumulation identity
        # drift are covered rather than merely checking an HTTP header.
        soma_latest = service.soma.latest_asof()
        z1 = _z1_identity(service, Path(temporary))
        # Keyless single calls for the remaining machine families. BEA is
        # excluded: it requires a key, which CI does not hold.
        bls = service.get_series("bls.lns14000000", start="2026-01-01", refresh=True)
        ofr = service.get_series("ofr.mmf_total_assets", start="2026-01-01", refresh=True)
        nyfed = service.get_series(
            "nyfed.secured_reference_rates", start="2026-06-01", refresh=True
        )
        treasurydirect = service.get_series("treasurydirect.auctioned_securities", refresh=True)
        tic = service.get_file("tic.net_purchases_bonds_notes", refresh=True)
        census = service.get_file(
            "census.popest.v2025.nc_est2025_agesex_res",
            refresh=True,
        )
        bls_release = service.get_file(
            "bls.empsit.2020_01_10.usdl_20_0010.pdf",
            refresh=True,
        )

    checks = {
        "fred": {
            "dataset": fred["dataset"]["id"],
            "rows": fred["preview"]["row_count"],
            "vintage": fred["provenance"]["vintage"],
        },
        "alfred": alfred,
        "fiscaldata": {
            "dataset": fiscaldata["dataset"]["id"],
            "rows": fiscaldata["preview"]["row_count"],
            "vintage": fiscaldata["provenance"]["vintage"],
        },
        "soma": {"latest_asof": soma_latest},
        "z1": z1,
        "bls": {"rows": bls["preview"]["row_count"]},
        "ofr": {"rows": ofr["preview"]["row_count"]},
        "nyfed": {"rows": nyfed["preview"]["row_count"]},
        "treasurydirect": {"rows": treasurydirect["preview"]["row_count"]},
        "tic": {"bytes": tic["provenance"]["byte_count"]},
        "census": {
            "bytes": census["provenance"]["byte_count"],
            "vintage": census["provenance"]["vintage"],
        },
        "bls_release": {
            "bytes": bls_release["provenance"]["byte_count"],
            "vintage": bls_release["provenance"]["vintage"],
        },
    }
    thin = [
        family for family in ("bls", "ofr", "nyfed", "treasurydirect") if checks[family]["rows"] < 1
    ]
    if thin or checks["tic"]["bytes"] < 1000:
        raise RuntimeError(f"live smoke returned no rows for {thin or 'tic'}: {checks}")
    if checks["fred"]["rows"] < 1 or checks["fiscaldata"]["rows"] < 1:
        raise RuntimeError(f"live smoke returned no rows: {checks}")
    if checks["census"]["bytes"] < 1_000 or checks["bls_release"]["bytes"] < 10_000:
        raise RuntimeError(f"live direct-file smoke returned implausibly small artifacts: {checks}")
    if not str(checks["census"]["vintage"]).startswith(
        "census.popest.v2025.nc-est2025-agesex-res|csv|sha256:"
    ):
        raise RuntimeError(f"Census logical release identity failed: {checks['census']}")
    if not str(checks["bls_release"]["vintage"]).startswith(
        "bls.empsit.2020-01-10.usdl-20-0010|pdf|sha256:"
    ):
        raise RuntimeError(f"BLS logical release identity failed: {checks['bls_release']}")
    if len(soma_latest) != 10:
        raise RuntimeError(f"SOMA latest as-of date looks malformed: {soma_latest!r}")
    if str(z1["release_date"]).startswith("retrieved-"):
        raise RuntimeError("Z.1 package no longer reports Last-Modified; vintage degraded")
    print(json.dumps(checks, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
