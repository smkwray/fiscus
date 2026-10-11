from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from fiscus.catalog import Catalog, catalog_schema_json
from fiscus.errors import FiscusError
from fiscus.models import Z1_KIND_CODES
from fiscus.service import FiscusService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fiscus",
        description="Curated U.S. economic-data catalog and fetch client.",
        epilog=(
            "Cache-backed fetches print a compact preview, provenance, and an opaque "
            "artifact descriptor. Use 'fiscus cache export CACHE_KEY --out PATH' to write "
            "the complete payload to a caller-owned file; local_file sources remain in place."
        ),
    )
    parser.add_argument("--data-dir", help="Override the platform cache directory.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    find_parser = subparsers.add_parser(
        "find-series",
        help="Search the catalog before choosing a dataset identifier.",
    )
    find_parser.add_argument("query")
    find_parser.add_argument("--family")
    find_parser.add_argument("--limit", type=int, default=8)

    get_parser = subparsers.add_parser("get-series", help="Fetch a curated FRED series.")
    get_parser.add_argument("dataset_id")
    get_parser.add_argument("--start")
    get_parser.add_argument("--end")
    get_parser.add_argument(
        "--vintage-as-of",
        help=(
            "Closed past ALFRED information date (YYYY-MM-DD). FRED series only; "
            "requires FRED_API_KEY."
        ),
    )
    get_parser.add_argument("--refresh", action="store_true")

    bea_parser = subparsers.add_parser(
        "bea-nipa-table",
        help="Fetch a complete BEA NIPA table for an explicit inclusive year range.",
    )
    bea_parser.add_argument("table_id", help="Canonical BEA TableName, such as T10106.")
    bea_parser.add_argument("frequency", choices=["A", "Q", "M", "a", "q", "m"])
    bea_parser.add_argument("start_year", type=int)
    bea_parser.add_argument("end_year", type=int)
    bea_parser.add_argument("--refresh", action="store_true")

    fiscal_parser = subparsers.add_parser(
        "fiscaldata-query",
        help="Query a curated Treasury FiscalData endpoint.",
    )
    fiscal_parser.add_argument("dataset_id")
    fiscal_parser.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help=(
            "Official FiscalData query keys, repeatable. Example: --param "
            "'filter=record_date:gte:2024-01-01,record_date:lte:2024-06-30' "
            "--param 'page[size]=1000'. Operators: eq, gte, gt, lte, lt, in."
        ),
    )
    fiscal_parser.add_argument("--max-pages", type=int, default=10)
    fiscal_parser.add_argument("--refresh", action="store_true")

    caveat_parser = subparsers.add_parser(
        "source-caveats",
        help="Show all caveats and whether they bind to the supplied context.",
    )
    caveat_parser.add_argument("dataset_id")
    caveat_parser.add_argument("--start")
    caveat_parser.add_argument("--end")
    caveat_parser.add_argument("--param", action="append", default=[], metavar="KEY=VALUE")
    caveat_parser.add_argument("--unit-class", action="append", default=[])

    file_parser = subparsers.add_parser(
        "get-file",
        help="Download a curated file artifact (workbook, text table, archive).",
    )
    file_parser.add_argument("dataset_id")
    file_parser.add_argument("--refresh", action="store_true")

    z1_parser = subparsers.add_parser(
        "z1-series",
        help="Fetch a Z.1 series with enforced official prefix semantics.",
    )
    z1_parser.add_argument(
        "code",
        help=(
            "Z.1 mnemonic (FL313020005.Q, BOGZ1FL313020005Q) or a z1.* catalog id from find-series."
        ),
    )
    z1_parser.add_argument(
        "kind",
        choices=[*Z1_KIND_CODES, *(kind.lower() for kind in Z1_KIND_CODES)],
        help=(
            "The official series-type prefix you intend to use. Must match the mnemonic — "
            "the deliberate redundancy prevents semantic relabeling."
        ),
    )
    z1_parser.add_argument("--start")
    z1_parser.add_argument("--end")
    z1_parser.add_argument("--refresh", action="store_true")

    soma_parser = subparsers.add_parser(
        "soma-holdings",
        help="Fetch CUSIP-level SOMA Treasury holdings for an as-of date.",
    )
    soma_parser.add_argument("--asof", help="ISO date; defaults to the latest published snapshot.")
    soma_parser.add_argument("--cusip", help="Restrict the result to one CUSIP.")
    soma_parser.add_argument("--refresh", action="store_true")

    provenance_parser = subparsers.add_parser(
        "provenance",
        help="Re-emit the manifest and artifact descriptor for a prior fetch.",
    )
    provenance_parser.add_argument("cache_key", help="Full cache key or a unique hex prefix.")

    catalog_parser = subparsers.add_parser("catalog", help="Catalog maintenance commands.")
    catalog_subparsers = catalog_parser.add_subparsers(dest="catalog_command", required=True)
    catalog_subparsers.add_parser("lint", help="Load and validate all catalog entries.")
    catalog_subparsers.add_parser("schema", help="Print the generated JSON Schema.")

    cache_parser = subparsers.add_parser("cache", help="Local cache maintenance commands.")
    cache_subparsers = cache_parser.add_subparsers(dest="cache_command", required=True)
    export_parser = cache_subparsers.add_parser(
        "export",
        help="Write a cached payload to a caller-owned destination file.",
    )
    export_parser.add_argument("cache_key", help="Full cache key or a unique hex prefix.")
    export_parser.add_argument("--out", required=True, help="Destination file path.")
    migrate_parser = cache_subparsers.add_parser(
        "migrate-v1",
        help="Read-only one-time migration of a legacy v1 cache into SQLite.",
    )
    migrate_parser.add_argument(
        "--from",
        dest="v1_root",
        default=None,
        help="Path to the old cache/v1 directory (default: the sibling of the v2 database).",
    )

    subparsers.add_parser("mcp", help="Run the stdio MCP server.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "catalog":
            if args.catalog_command == "schema":
                sys.stdout.write(catalog_schema_json())
                return 0
            _print_json(Catalog.load().lint_report())
            return 0
        if args.command == "mcp":
            from fiscus.mcp_server import main as mcp_main

            return mcp_main(data_dir=args.data_dir)

        with FiscusService(data_dir=args.data_dir) as service:
            if args.command == "find-series":
                result = service.find_series(
                    args.query,
                    family=args.family,
                    limit=args.limit,
                )
            elif args.command == "get-series":
                result = service.get_series(
                    args.dataset_id,
                    start=args.start,
                    end=args.end,
                    vintage_as_of=args.vintage_as_of,
                    refresh=args.refresh,
                )
            elif args.command == "bea-nipa-table":
                result = service.bea_nipa_table(
                    args.table_id,
                    args.frequency,
                    args.start_year,
                    args.end_year,
                    refresh=args.refresh,
                )
            elif args.command == "fiscaldata-query":
                result = service.fiscaldata_query(
                    args.dataset_id,
                    params=_parse_params(args.param),
                    max_pages=args.max_pages,
                    refresh=args.refresh,
                )
            elif args.command == "get-file":
                result = service.get_file(args.dataset_id, refresh=args.refresh)
            elif args.command == "z1-series":
                result = service.z1_series(
                    args.code,
                    args.kind,
                    start=args.start,
                    end=args.end,
                    refresh=args.refresh,
                )
            elif args.command == "soma-holdings":
                result = service.soma_holdings(
                    asof=args.asof,
                    cusip=args.cusip,
                    refresh=args.refresh,
                )
            elif args.command == "provenance":
                result = service.provenance(args.cache_key)
            elif args.command == "cache":
                if args.cache_command == "export":
                    result = service.export_artifact(args.cache_key, args.out)
                else:
                    result = service.migrate_v1_cache(args.v1_root)
            elif args.command == "source-caveats":
                result = service.source_caveats(
                    args.dataset_id,
                    start=args.start,
                    end=args.end,
                    params=_parse_params(args.param),
                    unit_classes=args.unit_class,
                )
            else:
                parser.error(f"unknown command: {args.command}")
                return 2
        _print_json(result)
        return 0
    except (FiscusError, ValueError) as exc:
        code = getattr(exc, "code", "invalid_request")
        print(f"fiscus [{code}]: {exc}", file=sys.stderr)
        return 2


def _parse_params(values: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"parameter must be KEY=VALUE: {value!r}")
        key, item = value.split("=", 1)
        if not key:
            raise ValueError(f"parameter key is empty: {value!r}")
        parsed[key] = item
    return parsed


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str))
