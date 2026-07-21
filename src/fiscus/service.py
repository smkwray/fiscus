from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from fiscus.cache import CacheManager, canonical_params
from fiscus.catalog import Catalog
from fiscus.caveats import CaveatContext, all_caveats, binding_caveats, parse_date
from fiscus.config import data_root
from fiscus.csvutil import rows_to_csv_bytes
from fiscus.errors import (
    BrowserDownloadOnlyError,
    DatasetNotFoundError,
    UnsupportedToolError,
)
from fiscus.executors import (
    BlsExecutor,
    FileExecutor,
    FiscalDataExecutor,
    FredExecutor,
    JsonApiExecutor,
    LocalFileExecutor,
    SomaExecutor,
    Z1Executor,
)
from fiscus.executors.base import FetchResult
from fiscus.executors.z1 import Z1_PACKAGE_URL, Z1Loaded
from fiscus.http import HttpRequester
from fiscus.models import (
    AccessMode,
    AccessSpec,
    AuthMode,
    AuthSpec,
    CatalogEntry,
    Caveat,
    EndpointSpec,
    ExecutorKind,
    HistorySpec,
    RefreshSpec,
    ResponseFormat,
    ResponseSpec,
    SemanticsSpec,
    Severity,
    ToolName,
    UnitSpec,
    VintageStrategy,
)
from fiscus.return_format import build_fetch_response, date_coverage, preview_rows
from fiscus.search import find_series as search_catalog
from fiscus.unit_inference import infer_passthrough_units

FILTER_DATE = re.compile(
    r"(?P<field>[A-Za-z0-9_]+):(?P<op>gte|gt|lte|lt|eq):(?P<date>\d{4}-\d{2}-\d{2})"
)

Z1_KIND_MEANINGS = {
    "FL": "a level of amounts outstanding at period end",
    "FU": "an unadjusted transaction flow",
    "LM": "a market-value level",
}

Z1_REJECTED_PREFIXES = {
    "FA": "FA is a seasonally adjusted annualized transaction flow; request the FU series instead",
    "FR": "FR is a revaluation series, outside the FL/FU/LM surface",
    "FV": "FV is an other-volume-changes series, outside the FL/FU/LM surface",
    "FC": "FC is a change series, outside the FL/FU/LM surface",
    "LA": "LA is a seasonally adjusted level, outside the FL/FU/LM surface",
    "FI": "FI is a growth-rate series, outside the FL/FU/LM surface",
}


def default_user_agent() -> str:
    """Build the outbound User-Agent, honoring the FISCUS_USER_AGENT override.

    The FRED edge stalls requests whose User-Agent lacks a parenthetical
    comment containing a ``+<url>`` token, so the default must keep that
    shape. Set FISCUS_USER_AGENT to identify your own deployment (e.g. to
    add a contact address).
    """
    override = os.environ.get("FISCUS_USER_AGENT")
    if override:
        return override
    try:
        from importlib.metadata import version

        pkg_version = version("fiscus")
    except Exception:
        pkg_version = "0"
    return f"fiscus/{pkg_version} (+https://pypi.org/project/fiscus/)"


class FiscusService:
    def __init__(
        self,
        *,
        catalog: Catalog | None = None,
        data_dir: str | Path | None = None,
        http_client: httpx.Client | None = None,
        sleep: Any | None = None,
    ) -> None:
        self.catalog = catalog or Catalog.load()
        self.cache = CacheManager(data_dir)
        self._owns_client = http_client is None
        self.http_client = http_client or httpx.Client(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            headers={
                "User-Agent": default_user_agent(),
                "Accept": "application/json,text/csv;q=0.9,*/*;q=0.5",
            },
        )
        requester_kwargs = {"sleep": sleep} if sleep is not None else {}
        requester = HttpRequester(self.http_client, **requester_kwargs)
        self.fred = FredExecutor(requester)
        self.fiscaldata = FiscalDataExecutor(requester)
        self.soma = SomaExecutor(requester)
        self.z1 = Z1Executor(requester, data_root(data_dir) / "packages" / "z1")
        self.json_api = JsonApiExecutor(requester)
        self.bls = BlsExecutor(requester)
        self.file = FileExecutor(requester)
        self.local_file = LocalFileExecutor()

    def close(self) -> None:
        self.cache.close()
        if self._owns_client:
            self.http_client.close()

    def read_bytes(self, cache_key: str) -> bytes:
        """Return the exact verified bytes for a cache artifact key or unique prefix."""
        return self.cache.read_bytes(cache_key)

    def iter_csv_rows(self, cache_key: str) -> Iterator[dict[str, str]]:
        """Iterate canonical CSV rows for a cache artifact key or unique prefix."""
        return self.cache.iter_csv_rows(cache_key)

    def export_artifact(self, cache_key: str, dest: str | Path) -> dict[str, Any]:
        """Write a cached payload to a caller-owned destination."""
        path = self.cache.export(cache_key, dest)
        return {
            "cache_key": cache_key,
            "exported_to": str(path.resolve()),
            "byte_count": path.stat().st_size,
        }

    def migrate_v1_cache(self, v1_root: str | Path | None = None) -> dict[str, Any]:
        """Read-only migration of a legacy v1 filesystem cache into SQLite."""
        return self.cache.migrate_v1(v1_root)

    def __enter__(self) -> FiscusService:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def find_series(
        self,
        query: str,
        *,
        family: str | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        if limit < 1 or limit > 50:
            raise ValueError("limit must be between 1 and 50")
        results = search_catalog(self.catalog.entries, query, family=family, limit=limit)
        response: dict[str, Any] = {"query": query, "count": len(results), "results": results}
        complete_match = any(
            "all query tokens matched" in result.get("matched_on", []) for result in results
        )
        if not complete_match:
            upstream, note = self._upstream_search(query, limit)
            if results:
                note = (
                    "Catalog matches above are partial (no entry matched every query term). " + note
                )
            response["uncurated_upstream"] = upstream
            response["note"] = note
        return response

    def get_series(
        self,
        dataset_id: str,
        *,
        start: str | date | None = None,
        end: str | date | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        try:
            entry = self.catalog.get(dataset_id)
        except DatasetNotFoundError:
            entry = self._passthrough_entry(dataset_id)
        self._require(entry.id, entry.tools, ToolName.GET_SERIES)
        self._require_machine_access(entry.id, entry.access.mode)
        series_executors = {ExecutorKind.FRED, ExecutorKind.JSON_API, ExecutorKind.BLS}
        if entry.access.executor not in series_executors:
            raise UnsupportedToolError(
                f"{entry.id} is not served by get_series; its catalog tools are "
                f"{[tool.value for tool in entry.tools]}"
            )

        start_date = parse_date(start)
        end_date = parse_date(end)
        if start_date and end_date and end_date < start_date:
            raise ValueError("end must not precede start")
        start_iso = start_date.isoformat() if start_date else None
        end_iso = end_date.isoformat() if end_date else None
        request_params: dict[str, Any] = {"start": start_iso, "end": end_iso}

        if entry.access.executor == ExecutorKind.FRED:
            auth_env = entry.access.auth.env_var
            request_params["source_mode"] = (
                "fred_api_json"
                if auth_env and os.environ.get(auth_env) and entry.endpoint.api_url
                else "fred_graph_csv"
            )
            executor = self.fred
        elif entry.access.executor == ExecutorKind.BLS:
            executor = self.bls
        else:
            executor = self.json_api

        def fetch() -> FetchResult:
            return executor.fetch(entry, start=start_iso, end=end_iso)

        context = CaveatContext(start=start_date, end=end_date)
        return self._cached_fetch(
            entry,
            canonical_params(request_params),
            refresh=refresh,
            caveat_context=context,
            fetch=fetch,
        )

    def fiscaldata_query(
        self,
        dataset_id: str,
        *,
        params: dict[str, str] | None = None,
        max_pages: int = 10,
        refresh: bool = False,
    ) -> dict[str, Any]:
        entry = self.catalog.get(dataset_id)
        self._require(entry.id, entry.tools, ToolName.FISCALDATA_QUERY)
        self._require_machine_access(entry.id, entry.access.mode)
        if entry.access.executor != ExecutorKind.FISCALDATA:
            raise UnsupportedToolError(f"{entry.id} is not implemented by the FiscalData executor")
        source_params = {str(key): str(value) for key, value in (params or {}).items()}
        request_params = canonical_params({"source_params": source_params, "max_pages": max_pages})
        start_date, end_date = infer_filter_date_range(
            source_params.get("filter", ""), entry.response.date_fields
        )
        context = CaveatContext(
            start=start_date,
            end=end_date,
            params=source_params,
        )
        return self._cached_fetch(
            entry,
            request_params,
            refresh=refresh,
            caveat_context=context,
            fetch=lambda: self.fiscaldata.fetch(
                entry,
                params=source_params,
                max_pages=max_pages,
            ),
        )

    def source_caveats(
        self,
        dataset_id: str,
        *,
        start: str | date | None = None,
        end: str | date | None = None,
        params: dict[str, str] | None = None,
        unit_classes: list[str] | None = None,
    ) -> dict[str, Any]:
        entry = self.catalog.get(dataset_id)
        start_date = parse_date(start)
        end_date = parse_date(end)
        if start_date and end_date and end_date < start_date:
            raise ValueError("end must not precede start")
        context = CaveatContext(
            start=start_date,
            end=end_date,
            params=params or {},
            unit_classes=frozenset(unit_classes or []),
            kind=entry.semantics.kind,
        )
        return {
            "dataset": {
                "id": entry.id,
                "title": entry.title,
                "family": entry.family,
                "verified_on": entry.verified_on.isoformat(),
                "history": entry.history.model_dump(mode="json"),
                "units": entry.units.model_dump(mode="json"),
                "semantics": entry.semantics.model_dump(mode="json"),
                "access": entry.access.model_dump(mode="json"),
            },
            "context": {
                "start": context.start.isoformat() if context.start else None,
                "end": context.end.isoformat() if context.end else None,
                "params": dict(context.params),
                "unit_classes": sorted(context.unit_classes),
                "kind": context.kind,
            },
            "caveats": all_caveats(entry, context),
        }

    def z1_series(
        self,
        code: str,
        kind: str,
        *,
        start: str | date | None = None,
        end: str | date | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        requested = code.strip()
        # Accept a catalog id (as returned by find_series) as well as a raw mnemonic.
        if requested.casefold() in {entry.id for entry in self.catalog.entries}:
            cataloged = self.catalog.get(requested.casefold())
            if cataloged.family == "z1" and cataloged.endpoint.series_id:
                requested = cataloged.endpoint.series_id
        canonical = normalize_z1_code(requested)
        kind_upper = str(kind).strip().upper()
        if kind_upper not in Z1_KIND_MEANINGS:
            raise ValueError(
                "kind must be FL (level), FU (transaction flow), or LM (market-value level)"
            )
        prefix = canonical[:2]
        if prefix in Z1_REJECTED_PREFIXES:
            raise ValueError(f"{canonical} is not served: {Z1_REJECTED_PREFIXES[prefix]}")
        if prefix not in Z1_KIND_MEANINGS:
            raise ValueError(f"{canonical} does not carry a supported Z.1 prefix (FL, FU, or LM)")
        if prefix != kind_upper:
            raise ValueError(
                f"refusing to present {canonical} as {kind_upper}: it is "
                f"{Z1_KIND_MEANINGS[prefix]} ({prefix}); request kind={prefix} or the "
                f"{kind_upper}-prefixed counterpart series"
            )
        start_date = parse_date(start)
        end_date = parse_date(end)
        if start_date and end_date and end_date < start_date:
            raise ValueError("end must not precede start")

        loaded = self.z1.load_series(canonical, refresh=refresh)
        entry = self._z1_entry(canonical, loaded)
        rows = [
            row
            for row in loaded.rows
            if (not start_date or row["date"] >= start_date.isoformat())
            and (not end_date or row["date"] <= end_date.isoformat())
        ]
        request_params = canonical_params(
            {
                "code": canonical,
                "start": start_date.isoformat() if start_date else None,
                "end": end_date.isoformat() if end_date else None,
            }
        )
        context = CaveatContext(start=start_date, end=end_date, kind=prefix)
        return self._cached_fetch(
            entry,
            request_params,
            refresh=refresh,
            caveat_context=context,
            fetch=lambda: FetchResult(
                rows=rows,
                source_url=loaded.package_source_url,
                retrieved_at_utc=loaded.package_retrieved_at_utc,
                vintage=loaded.vintage,
                source_metadata={
                    "mode": "z1_csv_package",
                    "release_date": loaded.release_date,
                    "series": {
                        "code": loaded.meta.code,
                        "description": loaded.meta.description,
                        "table": loaded.meta.table,
                        "units": loaded.meta.units,
                    },
                    "http": loaded.package_metadata,
                },
            ),
        )

    def soma_holdings(
        self,
        *,
        asof: str | None = None,
        cusip: str | None = None,
        refresh: bool = False,
        dataset_id: str = "soma.treasury_holdings",
    ) -> dict[str, Any]:
        entry = self.catalog.get(dataset_id)
        self._require(entry.id, entry.tools, ToolName.SOMA_HOLDINGS)
        self._require_machine_access(entry.id, entry.access.mode)
        if entry.access.executor != ExecutorKind.SOMA:
            raise UnsupportedToolError(f"{entry.id} is not implemented by the SOMA executor")
        # "Latest" is mutable, so resolve it to a concrete as-of date before
        # the cache is consulted; the snapshot date is the request identity.
        resolved_asof = asof or self.soma.latest_asof()
        asof_date = date.fromisoformat(resolved_asof)
        cusip_clean = cusip.strip().upper() if cusip else None
        request_params = canonical_params({"asof": resolved_asof, "cusip": cusip_clean})
        context = CaveatContext(start=asof_date, end=asof_date)
        return self._cached_fetch(
            entry,
            request_params,
            refresh=refresh,
            caveat_context=context,
            fetch=lambda: self.soma.fetch(entry, asof=resolved_asof, cusip=cusip_clean),
        )

    def get_file(
        self,
        dataset_id: str,
        *,
        refresh: bool = False,
    ) -> dict[str, Any]:
        entry = self.catalog.get(dataset_id)
        self._require(entry.id, entry.tools, ToolName.GET_FILE)
        self._require_machine_access(entry.id, entry.access.mode)
        if entry.access.executor == ExecutorKind.LOCAL_FILE:
            return self._local_file_response(entry)
        if entry.access.executor != ExecutorKind.FILE:
            raise UnsupportedToolError(f"{entry.id} is not implemented by the file executor")
        request_params = canonical_params({"url": str(entry.endpoint.url)})
        context = CaveatContext()
        if not refresh:
            cached = self.cache.load_latest(entry.id, request_params)
            if cached is not None:
                return build_fetch_response(entry, cached, binding_caveats(entry, context))
        result, content, data_file = self.file.fetch(entry)
        if data_file == "data.csv":
            data = rows_to_csv_bytes(result.rows, preferred_fields=entry.response.date_fields)
        else:
            data = content
        coverage = date_coverage(result.rows, entry.response.date_fields)
        manifest = {
            "source_url": result.source_url,
            "retrieved_at_utc": result.retrieved_at_utc,
            "source_metadata": result.source_metadata,
            "row_count": len(result.rows),
            "data_coverage": coverage,
            "catalog_verified_on": entry.verified_on.isoformat(),
        }
        cached = self.cache.store(
            entry.id,
            request_params,
            result.vintage,
            data,
            manifest,
            data_file=data_file,
        )
        return build_fetch_response(entry, cached, binding_caveats(entry, context))

    def _local_file_response(self, entry: CatalogEntry) -> dict[str, Any]:
        loaded = self.local_file.read(entry)
        rows = loaded["rows"]
        head, tail, truncated = preview_rows(rows, entry)
        coverage = date_coverage(rows, entry.response.date_fields)
        return {
            "dataset": {
                "id": entry.id,
                "title": entry.title,
                "family": entry.family,
                "frequency": entry.frequency,
                "units": entry.units.model_dump(mode="json"),
                "verified_on": entry.verified_on.isoformat(),
            },
            "preview": {
                "row_count": len(rows),
                "columns": list(rows[0]) if rows else [],
                "date_coverage": coverage,
                "head": head,
                "tail": tail,
                **({"truncated_columns": truncated} if truncated else {}),
            },
            "caveats": binding_caveats(entry, CaveatContext()),
            "provenance": {
                "source_url": f"file://{loaded['path']}",
                "retrieved_at_utc": loaded["retrieved_at_utc"],
                "sha256": loaded["sha256"],
                "byte_count": loaded["byte_count"],
                "vintage": loaded["vintage"],
                "cache_key": None,
                "row_count": len(rows),
                "data_coverage": coverage,
                "source_metadata": {
                    "mode": "local_file",
                    "modified_utc": loaded["modified_utc"],
                    "note": "Read in place; not copied into the cache. provenance() replay "
                    "by cache key is not available for local files.",
                },
            },
            "full_data_path": str(loaded["path"].resolve()),
            "manifest_path": None,
            "cache_hit": False,
        }

    def provenance(self, cache_key: str) -> dict[str, Any]:
        artifact = self.cache.find_by_cache_key(cache_key)
        return {
            "manifest": artifact.manifest,
            "artifact": artifact.descriptor.as_dict(),
            "row_count": artifact.manifest.get("row_count", len(artifact.rows)),
            "sha256_verified": True,
        }

    def _upstream_search(self, query: str, limit: int) -> tuple[list[dict[str, Any]], str]:
        """Source-side search when the curated catalog has no match."""
        api_key = os.environ.get("FRED_API_KEY")
        if not api_key:
            return [], (
                "No curated catalog match. Uncataloged FRED or BLS series can still be "
                "fetched as fred.<SERIES_ID> or bls.<SERIES_ID> (no curated caveat review). "
                "Set FRED_API_KEY to enable upstream FRED search from this tool."
            )
        found = self.fred.search(query, api_key, limit)
        upstream = [
            {
                "id": f"fred.{item.get('id', '').lower()}",
                "series_id": item.get("id", ""),
                "title": item.get("title", ""),
                "units": item.get("units", ""),
                "frequency": item.get("frequency", ""),
                "seasonal_adjustment": item.get("seasonal_adjustment_short", ""),
                "observation_start": item.get("observation_start", ""),
                "curated": False,
                "source": "fred_search",
            }
            for item in found
            if item.get("id")
        ]
        note = (
            "No curated catalog match; results below are from FRED's own search and carry "
            "no curated caveat review. Fetch with get_series using the id shown, and read "
            "units and seasonal adjustment from the source metadata in the response."
            if upstream
            else "No curated catalog match and FRED's upstream search returned nothing; "
            "uncataloged series can still be fetched as fred.<SERIES_ID> or bls.<SERIES_ID>."
        )
        return upstream, note

    def _passthrough_entry(self, dataset_id: str) -> CatalogEntry:
        """Synthesize an entry for an uncataloged fred.<ID> or bls.<ID> request."""
        family, _, raw_sid = dataset_id.partition(".")
        sid = raw_sid.upper().strip()
        if family not in {"fred", "bls"} or not re.fullmatch(r"[A-Z0-9._-]{2,64}", sid):
            raise DatasetNotFoundError(
                f"unknown dataset id {dataset_id!r}; call find_series first, or fetch an "
                "uncataloged series as fred.<SERIES_ID> or bls.<SERIES_ID>"
            )
        if family == "fred":
            return self._fred_passthrough_entry(sid)
        return self._bls_passthrough_entry(sid)

    def _fred_passthrough_entry(self, sid: str) -> CatalogEntry:
        api_key = os.environ.get("FRED_API_KEY")
        info = self.fred.series_info(sid, api_key) if api_key else None
        info = info or {}
        history_start = _safe_date(info.get("observation_start")) or date(1800, 1, 1)
        units, annualization_caveat = infer_passthrough_units(
            label=info.get("units"),
            seasonal_adjustment=info.get("seasonal_adjustment"),
            frequency=info.get("frequency"),
            unknown_label="unknown (FRED series metadata unavailable)",
        )
        caveats = [
            *([annualization_caveat] if annualization_caveat else []),
            _UNCATALOGED_CAVEAT,
            _FRED_DOT_CAVEAT,
        ]
        return CatalogEntry(
            id=f"fred.{sid.lower()}",
            family="fred",
            title=info.get("title") or f"FRED series {sid} (uncataloged)",
            description=(
                f"Uncataloged FRED series {sid}, synthesized at request time from source "
                "metadata. No curated caveat review has been performed."
            ),
            publisher="Distributed by FRED, Federal Reserve Bank of St. Louis",
            aliases=[sid],
            tags=["fred", "uncataloged"],
            verified_on=datetime.now(UTC).date(),
            history=HistorySpec(
                start=history_start,
                notes="Boundary from source metadata when keyed; otherwise not verified.",
            ),
            frequency=info.get("frequency") or "as reported by source",
            units=units,
            endpoint=EndpointSpec(
                url="https://fred.stlouisfed.org/graph/fredgraph.csv",
                api_url="https://api.stlouisfed.org/fred/series/observations",
                series_id=sid,
            ),
            access=AccessSpec(
                executor=ExecutorKind.FRED,
                auth=AuthSpec(
                    mode=AuthMode.OPTIONAL,
                    env_var="FRED_API_KEY",
                    note="Without a key, the executor uses the public FRED graph CSV path.",
                ),
            ),
            response=ResponseSpec(
                format=ResponseFormat.CSV,
                date_fields=["date"],
                value_fields=["value"],
                vintage_strategy=VintageStrategy.FRED_REALTIME_OR_HTTP,
            ),
            refresh=RefreshSpec(cadence="as published"),
            tools=[ToolName.GET_SERIES],
            caveats=caveats,
        )

    def _bls_passthrough_entry(self, sid: str) -> CatalogEntry:
        units, annualization_caveat = infer_passthrough_units(
            label=None,
            seasonal_adjustment=None,
            frequency=None,
            unknown_label="unknown (BLS data response omits series units)",
        )
        caveats = [
            *([annualization_caveat] if annualization_caveat else []),
            _UNCATALOGED_CAVEAT,
            _BLS_WINDOW_CAVEAT,
        ]
        return CatalogEntry(
            id=f"bls.{sid.lower()}",
            family="bls",
            title=f"BLS series {sid} (uncataloged)",
            description=(
                f"Uncataloged BLS series {sid}, synthesized at request time. No curated "
                "caveat review has been performed and no source metadata is attached."
            ),
            publisher="U.S. Bureau of Labor Statistics",
            aliases=[sid],
            tags=["bls", "uncataloged"],
            verified_on=datetime.now(UTC).date(),
            history=HistorySpec(
                start=date(1800, 1, 1),
                notes="History boundary not verified for uncataloged series.",
            ),
            frequency="as reported by source",
            units=units,
            endpoint=EndpointSpec(
                url="https://api.bls.gov/publicAPI/v2/timeseries/data/",
                series_id=sid,
            ),
            access=AccessSpec(
                executor=ExecutorKind.BLS,
                auth=AuthSpec(
                    mode=AuthMode.OPTIONAL,
                    env_var="BLS_API_KEY",
                    note="Keyless requests are limited to 10 years per query and 25 per day.",
                ),
            ),
            response=ResponseSpec(
                format=ResponseFormat.JSON,
                date_fields=["date"],
                value_fields=["value"],
            ),
            refresh=RefreshSpec(cadence="as published"),
            tools=[ToolName.GET_SERIES],
            caveats=caveats,
        )

    def _z1_entry(self, code: str, loaded: Z1Loaded) -> CatalogEntry:
        for entry in self.catalog.entries:
            if (
                entry.family == "z1"
                and entry.endpoint.series_id == code
                and ToolName.Z1_SERIES in entry.tools
            ):
                return entry
        return _synthetic_z1_entry(code, loaded)

    def _cached_fetch(
        self,
        entry: CatalogEntry,
        request_params: dict[str, Any],
        *,
        refresh: bool,
        caveat_context: CaveatContext,
        fetch: Callable[[], FetchResult],
    ) -> dict[str, Any]:
        if not refresh:
            cached = self.cache.load_latest(entry.id, request_params)
            if cached is not None:
                return build_fetch_response(
                    entry,
                    cached,
                    binding_caveats(entry, caveat_context),
                )

        result = fetch()
        preferred_fields = [
            *entry.response.date_fields,
            *entry.response.value_fields,
        ]
        data = rows_to_csv_bytes(result.rows, preferred_fields=preferred_fields)
        coverage = date_coverage(result.rows, entry.response.date_fields)
        manifest = {
            "source_url": result.source_url,
            "retrieved_at_utc": result.retrieved_at_utc,
            "source_metadata": result.source_metadata,
            "row_count": len(result.rows),
            "data_coverage": coverage,
            "catalog_verified_on": entry.verified_on.isoformat(),
        }
        cached = self.cache.store(
            entry.id,
            request_params,
            result.vintage,
            data,
            manifest,
        )
        return build_fetch_response(
            entry,
            cached,
            binding_caveats(entry, caveat_context),
        )

    @staticmethod
    def _require(dataset_id: str, tools: list[ToolName], expected: ToolName) -> None:
        if expected not in tools:
            raise UnsupportedToolError(f"{dataset_id} is not available through {expected.value}")

    @staticmethod
    def _require_machine_access(dataset_id: str, mode: AccessMode) -> None:
        if mode == AccessMode.BROWSER_DOWNLOAD_ONLY:
            raise BrowserDownloadOnlyError(f"{dataset_id} is cataloged as browser-download-only")


_UNCATALOGED_CAVEAT = Caveat(
    id="fiscus.uncataloged",
    severity=Severity.WARNING,
    message=(
        "This series is not in the curated catalog: units, history boundaries, and known "
        "footguns have not been reviewed. Use the returned unit and annualization signals "
        "when available; when they are unknown, verify the source before combining with "
        "anything. Consider adding a catalog entry once verified."
    ),
)

_FRED_DOT_CAVEAT = Caveat(
    id="fred.missing_value_dot",
    severity=Severity.INFO,
    message=(
        "FRED CSV observations mark missing values (holidays, unpublished periods) with a "
        "bare period character; filter these rows before numeric parsing."
    ),
)

_BLS_WINDOW_CAVEAT = Caveat(
    id="bls.year_window_limit",
    severity=Severity.WARNING,
    message=(
        "The API caps each request at 10 years without a key and 20 years with one. A long "
        "history requires multiple year-windowed calls; a single call silently returns only "
        "the requested window."
    ),
)


def _safe_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def normalize_z1_code(code: str) -> str:
    """Canonicalize Z.1 mnemonics: BOGZ1FL313020005Q and FL313020005 -> FL313020005.Q."""
    cleaned = code.strip().upper().replace(" ", "")
    if not cleaned:
        raise ValueError("Z.1 series code is empty")
    if cleaned.startswith("BOGZ1"):
        cleaned = cleaned[5:]
    if "." not in cleaned:
        if cleaned[-1] in {"Q", "A"} and len(cleaned) > 2 and cleaned[-2].isdigit():
            cleaned = f"{cleaned[:-1]}.{cleaned[-1]}"
        else:
            cleaned = f"{cleaned}.Q"
    return cleaned


_Z1_SYNTHETIC_CAVEATS = {
    "FL": Caveat(
        id="z1.fl.level_not_flow",
        severity=Severity.CRITICAL,
        message=(
            "FL is a level. Do not label a first difference of this series as the Z.1 "
            "transaction flow; use the corresponding FU series."
        ),
    ),
    "FU": Caveat(
        id="z1.fu.transaction_not_level",
        severity=Severity.CRITICAL,
        message=(
            "FU is the transactions concept, not the end-of-period asset level. Use the "
            "corresponding FL series for stocks."
        ),
    ),
    "LM": Caveat(
        id="z1.lm.valuation_effects",
        severity=Severity.CRITICAL,
        message=(
            "LM is a market-value level. Its change includes valuation and other adjustments; "
            "do not present the level change as purchases or transaction flow."
        ),
    ),
}

_Z1_REVISIONS_CAVEAT = Caveat(
    id="z1.revisions",
    severity=Severity.WARNING,
    message=(
        "Z.1 releases can make major historical and structural revisions. Pin and report the "
        "release vintage."
    ),
)


def _synthetic_z1_entry(code: str, loaded: Z1Loaded) -> CatalogEntry:
    """Describe an uncataloged Z.1 series from the package's own data dictionary."""
    release = loaded.release_date.removeprefix("retrieved-")
    try:
        verified = date.fromisoformat(release)
    except ValueError:
        verified = datetime.now(UTC).date()
    first_date = loaded.rows[0]["date"] if loaded.rows else None
    try:
        history_start = date.fromisoformat(first_date) if first_date else verified
    except ValueError:
        history_start = verified
    units_label = loaded.meta.units or "see Z.1 data dictionary"
    unit_class = (
        "usd_millions_nsa" if "millions of dollars" in units_label.casefold() else "not_saar"
    )
    frequency = "annual" if code.endswith(".A") else "quarterly, end of period"
    kind = code[:2]
    return CatalogEntry(
        id=f"z1.{code.casefold().replace('.', '_')}",
        family="z1",
        title=loaded.meta.description or code,
        description=(
            f"Uncataloged Z.1 series {code} served from the official CSV package; "
            f"described by the package data dictionary (table {loaded.meta.table})."
        ),
        publisher="Board of Governors of the Federal Reserve System",
        aliases=[code],
        tags=["z1", "uncataloged"],
        verified_on=verified,
        history=HistorySpec(
            start=history_start,
            notes="Boundary observed in the current package, not independently verified.",
        ),
        frequency=frequency,
        units=UnitSpec(label=units_label, unit_class=unit_class),
        endpoint=EndpointSpec(url=Z1_PACKAGE_URL, series_id=code),
        access=AccessSpec(executor=ExecutorKind.Z1, auth=AuthSpec()),
        response=ResponseSpec(
            format=ResponseFormat.ZIP_CSV,
            date_fields=["date"],
            value_fields=["value"],
            vintage_strategy=VintageStrategy.RELEASE_DATE,
        ),
        refresh=RefreshSpec(cadence="quarterly"),
        semantics=SemanticsSpec(kind=kind, notes=Z1_KIND_MEANINGS[kind]),
        tools=[ToolName.Z1_SERIES],
        caveats=[_Z1_SYNTHETIC_CAVEATS[kind], _Z1_REVISIONS_CAVEAT],
    )


def infer_filter_date_range(
    filter_value: str,
    date_fields: list[str],
) -> tuple[date | None, date | None]:
    permitted = set(date_fields)
    lower: list[date] = []
    upper: list[date] = []
    exact: list[date] = []
    for match in FILTER_DATE.finditer(filter_value):
        if permitted and match.group("field") not in permitted:
            continue
        value = date.fromisoformat(match.group("date"))
        operation = match.group("op")
        if operation in {"gte", "gt"}:
            lower.append(value)
        elif operation in {"lte", "lt"}:
            upper.append(value)
        else:
            exact.append(value)
    if exact:
        return min(exact), max(exact)
    start = max(lower) if lower else None
    end = min(upper) if upper else None
    if start and end and end < start:
        raise ValueError("FiscalData date filter upper bound precedes lower bound")
    return start, end
