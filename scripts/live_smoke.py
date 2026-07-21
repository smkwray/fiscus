from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fiscus.service import FiscusService


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
        # Cheap family checks: the SOMA date list is tiny, and the Z.1 probe
        # is a HEAD (no package download) that must yield a dated release.
        soma_latest = service.soma.latest_asof()
        z1_release, _ = service.z1._current_release_date()
        # Keyless single calls for the remaining machine families. BEA is
        # excluded: it requires a key, which CI does not hold.
        bls = service.get_series("bls.lns14000000", start="2026-01-01", refresh=True)
        ofr = service.get_series("ofr.mmf_total_assets", start="2026-01-01", refresh=True)
        nyfed = service.get_series(
            "nyfed.secured_reference_rates", start="2026-06-01", refresh=True
        )
        treasurydirect = service.get_series("treasurydirect.auctioned_securities", refresh=True)
        tic = service.get_file("tic.net_purchases_bonds_notes", refresh=True)

    checks = {
        "fred": {
            "dataset": fred["dataset"]["id"],
            "rows": fred["preview"]["row_count"],
            "vintage": fred["provenance"]["vintage"],
        },
        "fiscaldata": {
            "dataset": fiscaldata["dataset"]["id"],
            "rows": fiscaldata["preview"]["row_count"],
            "vintage": fiscaldata["provenance"]["vintage"],
        },
        "soma": {"latest_asof": soma_latest},
        "z1": {"release_date": z1_release},
        "bls": {"rows": bls["preview"]["row_count"]},
        "ofr": {"rows": ofr["preview"]["row_count"]},
        "nyfed": {"rows": nyfed["preview"]["row_count"]},
        "treasurydirect": {"rows": treasurydirect["preview"]["row_count"]},
        "tic": {"bytes": tic["provenance"]["byte_count"]},
    }
    thin = [
        family for family in ("bls", "ofr", "nyfed", "treasurydirect") if checks[family]["rows"] < 1
    ]
    if thin or checks["tic"]["bytes"] < 1000:
        raise RuntimeError(f"live smoke returned no rows for {thin or 'tic'}: {checks}")
    if checks["fred"]["rows"] < 1 or checks["fiscaldata"]["rows"] < 1:
        raise RuntimeError(f"live smoke returned no rows: {checks}")
    if len(soma_latest) != 10:
        raise RuntimeError(f"SOMA latest as-of date looks malformed: {soma_latest!r}")
    if z1_release.startswith("retrieved-"):
        raise RuntimeError("Z.1 package no longer reports Last-Modified; vintage degraded")
    print(json.dumps(checks, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
