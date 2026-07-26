from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

import httpx

from fiscus.cache import CacheManager, canonical_params
from fiscus.catalog import Catalog
from fiscus.caveats import CaveatContext, all_caveats, binding_caveats, parse_date
from fiscus.config import data_root
from fiscus.csvutil import rows_to_csv_bytes
from fiscus.errors import (
    BrowserDownloadOnlyError,
    DatasetNotFoundError,
    FiscusError,
    InvalidRequestError,
    InvalidVintageError,
    MissingKeyError,
    SourceQueryRejectedError,
    SourceSchemaChangedError,
    UnsupportedToolError,
    UnsupportedVintageError,
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
from fiscus.executors.json_api import JsonApiRequestSpec
from fiscus.executors.z1 import Z1_PACKAGE_URL, Z1Loaded
from fiscus.http import HttpRequester
from fiscus.models import (
    Z1_KIND_CODES,
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
from fiscus.return_format import (
    build_fetch_response,
    date_coverage,
    preview_rows,
    preview_rows_for_fields,
)
from fiscus.search import find_series as search_catalog
from fiscus.search import vintage_support
from fiscus.unit_inference import infer_passthrough_units

FILTER_DATE = re.compile(
    r"(?P<field>[A-Za-z0-9_]+):(?P<op>gte|gt|lte|lt|eq):(?P<date>\d{4}-\d{2}-\d{2})"
)
BEA_API_URL = "https://apps.bea.gov/api/data/"
BEA_NIPA_INVENTORY_DATASET_ID = "bea_nipa_inventory"
BEA_NIPA_TABLE_DATASET_ID = "bea_nipa_table"
BEA_TABLE_ID = re.compile(r"^T[A-Z0-9]{5,6}$")
BEA_FREQUENCIES = frozenset({"A", "Q", "M"})
BEA_INVENTORY_FREQUENCY = re.compile(r"\(([AQM])\)")


@dataclass(frozen=True)
class BeaNipaInventory:
    rows: list[dict[str, Any]]
    diagnostics: dict[str, Any] | None = None


Z1_KIND_MEANINGS = {
    "FA": "a seasonally adjusted annual-rate transaction flow",
    "FC": "the change in an unadjusted level",
    "FG": "a seasonally adjusted growth rate",
    "FI": "an index",
    "FL": "a level at period end, not seasonally adjusted",
    "FR": "a revaluation (holding gain or loss)",
    "FS": "a seasonal factor",
    "FU": "a transaction flow, not seasonally adjusted",
    "FV": "an other change in volume",
    "LA": "a seasonally adjusted level",
    "LM": "a market-value or current-cost level, not seasonally adjusted",
    "PC": "a percent change in an index",
}

if set(Z1_KIND_MEANINGS) != set(Z1_KIND_CODES):  # pragma: no cover - import-time invariant
    raise RuntimeError("Z.1 kind meanings must cover the official series-prefix taxonomy")

BEA_NIPA_INVENTORY_REQUEST = JsonApiRequestSpec(
    request_id=BEA_NIPA_INVENTORY_DATASET_ID,
    url=BEA_API_URL,
    default_params={
        "method": "GetParameterValues",
        "datasetname": "NIPA",
        "ParameterName": "TableName",
        "ResultFormat": "json",
    },
    auth_mode=AuthMode.REQUIRED,
    auth_env_var="BEA_API_KEY",
    api_key_param="UserID",
    data_path="BEAAPI.Results.ParamValue",
    response_adapter="bea_api",
)

BEA_NIPA_TABLE_REQUEST = JsonApiRequestSpec(
    request_id=BEA_NIPA_TABLE_DATASET_ID,
    url=BEA_API_URL,
    default_params={
        "method": "GetData",
        "datasetname": "NIPA",
        "ResultFormat": "json",
        "ShowMillions": "N",
    },
    allowed_params=frozenset({"TableName", "Frequency", "Year"}),
    auth_mode=AuthMode.REQUIRED,
    auth_env_var="BEA_API_KEY",
    api_key_param="UserID",
    data_path="BEAAPI.Results.Data",
    date_fields=("date", "TimePeriod"),
    value_fields=("DataValue",),
    date_normalizer="bea_period",
    response_adapter="bea_api",
)


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
            upstream, note, diagnostics = self._upstream_search(query, limit, family=family)
            if results:
                note = (
                    "Catalog matches above are partial (no entry matched every query term). " + note
                )
            response["uncurated_upstream"] = upstream
            response["note"] = note
            if diagnostics is not None:
                response["uncurated_upstream_diagnostics"] = diagnostics
        return response

    def get_series(
        self,
        dataset_id: str,
        *,
        start: str | date | None = None,
        end: str | date | None = None,
        vintage_as_of: str | date | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        if dataset_id == "bea.nipa_t10106":
            if vintage_as_of is not None:
                raise UnsupportedVintageError(
                    "bea.nipa_t10106 cannot serve vintage_as_of; use bea_nipa_table"
                )
            start_date = parse_date(start)
            end_date = parse_date(end)
            if start_date and end_date and end_date < start_date:
                raise InvalidRequestError("end must not precede start")
            result = self.bea_nipa_table(
                "T10106",
                "Q",
                start_date.year if start_date else 1947,
                end_date.year if end_date else datetime.now(UTC).year,
                refresh=refresh,
            )
            artifact = self.cache.find_by_cache_key(result["artifact"]["cache_key"])
            filtered = self._build_bea_nipa_response(
                "T10106",
                "Q",
                artifact,
                observation_start=start_date,
                observation_end=end_date,
            )
            filtered["cache_hit"] = result["cache_hit"]
            result = filtered
            result["deprecation"] = {
                "status": "deprecated",
                "remove_after": "one release",
                "replacement": "bea_nipa_table(T10106, Q, start_year, end_year)",
            }
            return result

        vintage_date = _closed_past_vintage(vintage_as_of)
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
        if vintage_date and entry.access.executor != ExecutorKind.FRED:
            raise UnsupportedVintageError(
                f"{entry.id} cannot serve vintage_as_of; historical information dates "
                "are supported only for FRED-backed series"
            )

        start_date = parse_date(start)
        end_date = parse_date(end)
        if start_date and end_date and end_date < start_date:
            raise ValueError("end must not precede start")
        start_iso = start_date.isoformat() if start_date else None
        end_iso = end_date.isoformat() if end_date else None
        vintage_iso = vintage_date.isoformat() if vintage_date else None
        request_params: dict[str, Any] = {"start": start_iso, "end": end_iso}

        if entry.access.executor == ExecutorKind.FRED:
            auth_env = entry.access.auth.env_var
            if vintage_iso:
                request_params.update(
                    {
                        "source_mode": "alfred_api_json",
                        "vintage_as_of": vintage_iso,
                    }
                )
            else:
                request_params["source_mode"] = (
                    "fred_api_json"
                    if auth_env and os.environ.get(auth_env) and entry.endpoint.api_url
                    else "fred_graph_csv"
                )
            def fetch() -> FetchResult:
                return self.fred.fetch(
                    entry,
                    start=start_iso,
                    end=end_iso,
                    vintage_as_of=vintage_iso,
                )

        elif entry.access.executor == ExecutorKind.BLS:
            def fetch() -> FetchResult:
                return self.bls.fetch(entry, start=start_iso, end=end_iso)

        else:
            def fetch() -> FetchResult:
                return self.json_api.fetch(entry, start=start_iso, end=end_iso)

        context = CaveatContext(
            start=start_date,
            end=end_date,
            params={"vintage_as_of": vintage_iso} if vintage_iso else {},
        )
        response_entry = (
            entry.model_copy(update={"caveats": [*entry.caveats, _ALFRED_SNAPSHOT_CAVEAT]})
            if vintage_iso
            else entry
        )
        return self._cached_fetch(
            response_entry,
            canonical_params(request_params),
            refresh=refresh,
            caveat_context=context,
            fetch=fetch,
        )

    def bea_nipa_table(
        self,
        table_id: str,
        frequency: Literal["A", "Q", "M"],
        start_year: int,
        end_year: int,
        *,
        refresh: bool = False,
    ) -> dict[str, Any]:
        table = str(table_id).strip().upper()
        freq = str(frequency).strip().upper()
        first_year, last_year = _validate_bea_nipa_request(
            table,
            freq,
            start_year,
            end_year,
        )
        request_params = canonical_params(
            {
                "table_id": table,
                "frequency": freq,
                "start_year": first_year,
                "end_year": last_year,
            }
        )
        cached = None if refresh else self.cache.load_latest(
            BEA_NIPA_TABLE_DATASET_ID,
            request_params,
        )
        if cached is None:
            if not os.environ.get("BEA_API_KEY"):
                raise MissingKeyError(
                    "BEA_API_KEY is unset; bea_nipa_table requires this environment variable"
                )
            inventory_item = self._validate_bea_nipa_inventory_pair(table, freq)
            year_param = ",".join(str(year) for year in range(first_year, last_year + 1))
            result = self.json_api.fetch_request(
                BEA_NIPA_TABLE_REQUEST,
                params={
                    "TableName": table,
                    "Frequency": freq,
                    "Year": year_param,
                },
            )
            coverage = bea_nipa_coverage(result.rows, table)
            source_reported = {
                "description": inventory_item["Description"],
                "advertised_frequencies": inventory_item["advertised_frequencies"],
            }
            if inventory_item.get("inventory_diagnostics") is not None:
                source_reported["inventory_diagnostics"] = inventory_item[
                    "inventory_diagnostics"
                ]
            data = rows_to_csv_bytes(
                result.rows,
                preferred_fields=[
                    "date",
                    "TimePeriod",
                    "TableName",
                    "LineNumber",
                    "SeriesCode",
                    "LineDescription",
                    "METRIC_NAME",
                    "CL_UNIT",
                    "UNIT_MULT",
                    "DataValue",
                ],
            )
            manifest = {
                "source_url": result.source_url,
                "retrieved_at_utc": result.retrieved_at_utc,
                "source_metadata": result.source_metadata,
                "source_request": {
                    "operation": BEA_NIPA_TABLE_DATASET_ID,
                    "method": "GetData",
                    "datasetname": "NIPA",
                    "TableName": table,
                    "Frequency": freq,
                    "Year": year_param,
                    "ResultFormat": "json",
                    "ShowMillions": "N",
                },
                "source_reported": source_reported,
                "row_count": len(result.rows),
                "data_coverage": date_coverage(result.rows, ("date", "TimePeriod")),
                "observed_coverage": coverage,
            }
            cached = self.cache.store(
                BEA_NIPA_TABLE_DATASET_ID,
                request_params,
                result.vintage,
                data,
                manifest,
            )
        return self._build_bea_nipa_response(table, freq, cached)

    def _validate_bea_nipa_inventory_pair(
        self,
        table_id: str,
        frequency: str,
    ) -> dict[str, Any]:
        match: dict[str, Any] | None = None
        for refresh in (False, True):
            try:
                inventory = self._bea_nipa_inventory(refresh=refresh)
            except SourceSchemaChangedError:
                if refresh:
                    raise
                continue
            match = next(
                (row for row in inventory.rows if row["TableName"] == table_id),
                None,
            )
            if match is not None and frequency in match["advertised_frequencies"]:
                if inventory.diagnostics is not None:
                    return {**match, "inventory_diagnostics": inventory.diagnostics}
                return match
        if match is None:
            raise SourceQueryRejectedError(
                f"BEA NIPA inventory does not advertise table {table_id} after refresh"
            )
        advertised = ", ".join(match["advertised_frequencies"]) or "none"
        raise SourceQueryRejectedError(
            f"BEA NIPA table {table_id} does not advertise frequency {frequency}; "
            f"advertised frequencies: {advertised}"
        )

    def _bea_nipa_inventory(self, *, refresh: bool = False) -> BeaNipaInventory:
        request_params = canonical_params(
            {"source_dataset": "NIPA", "parameter_name": "TableName"}
        )
        cached = None if refresh else self.cache.load_latest(
            BEA_NIPA_INVENTORY_DATASET_ID,
            request_params,
        )
        if cached is None:
            result = self.json_api.fetch_request(BEA_NIPA_INVENTORY_REQUEST)
            data = rows_to_csv_bytes(
                result.rows,
                preferred_fields=["TableName", "Description"],
            )
            cached = self.cache.store(
                BEA_NIPA_INVENTORY_DATASET_ID,
                request_params,
                result.vintage,
                data,
                {
                    "source_url": result.source_url,
                    "retrieved_at_utc": result.retrieved_at_utc,
                    "source_metadata": result.source_metadata,
                    "source_request": {
                        "operation": BEA_NIPA_INVENTORY_DATASET_ID,
                        "method": "GetParameterValues",
                        "datasetname": "NIPA",
                        "ParameterName": "TableName",
                        "ResultFormat": "json",
                    },
                    "row_count": len(result.rows),
                },
            )
        return normalize_bea_nipa_inventory(cached.rows)

    def _bea_curated_entry(self, table_id: str, frequency: str) -> CatalogEntry | None:
        for entry in self.catalog.entries:
            defaults = entry.endpoint.default_params
            if (
                entry.family == "bea"
                and entry.access.executor == ExecutorKind.JSON_API
                and entry.tools == [ToolName.BEA_NIPA_TABLE]
                and defaults.get("datasetname", "").upper() == "NIPA"
                and defaults.get("TableName", "").upper() == table_id
                and defaults.get("Frequency", "").upper() == frequency
            ):
                return entry
        return None

    def _build_bea_nipa_response(
        self,
        table_id: str,
        frequency: str,
        artifact: Any,
        *,
        observation_start: date | None = None,
        observation_end: date | None = None,
    ) -> dict[str, Any]:
        artifact_rows = artifact.rows
        manifest = artifact.manifest
        artifact_coverage = bea_nipa_coverage(artifact_rows, table_id)
        stored_coverage = manifest.get("observed_coverage")
        if isinstance(stored_coverage, dict) and stored_coverage != artifact_coverage:
            raise SourceSchemaChangedError(
                "cached BEA NIPA observed coverage disagrees with the artifact rows"
            )
        rows = _filter_rows_by_date(
            artifact_rows,
            start=observation_start,
            end=observation_end,
        )
        coverage = bea_nipa_coverage(rows, table_id)
        curated = self._bea_curated_entry(table_id, frequency)
        source_reported = manifest.get("source_reported", {})
        if not isinstance(source_reported, dict):
            source_reported = {}
        head, tail, truncated = preview_rows_for_fields(
            rows,
            [
                "date",
                "TimePeriod",
                "TableName",
                "LineNumber",
                "SeriesCode",
                "LineDescription",
                "METRIC_NAME",
                "CL_UNIT",
                "UNIT_MULT",
                "DataValue",
            ],
        )
        metric_names = _observed_values(rows, "METRIC_NAME")
        response_date_coverage = date_coverage(rows, ("date", "TimePeriod"))
        caveats = [_serialize_route_caveat(_BEA_NIPA_OBSERVED_ROWS_CAVEAT)]
        if any(
            "fisher quantity index" in metric.casefold()
            or "chained dollars" in metric.casefold()
            for metric in metric_names
        ):
            caveats.append(_serialize_route_caveat(_BEA_NIPA_CONCEPT_CAVEAT))
        if curated is not None:
            context = CaveatContext(
                start=observation_start
                or date(int(manifest["params"]["start_year"]), 1, 1),
                end=observation_end
                or date(int(manifest["params"]["end_year"]), 12, 31),
                params={"TableName": table_id, "Frequency": frequency},
            )
            for caveat in binding_caveats(curated, context):
                if caveat["id"] not in {item["id"] for item in caveats}:
                    caveats.append(caveat)

        curation = {
            "status": "curated" if curated is not None else "uncurated",
            "catalog_id": curated.id if curated is not None else None,
            "verified_on": curated.verified_on.isoformat() if curated is not None else None,
        }
        response: dict[str, Any] = {
            "dataset": {
                "route_id": f"bea.nipa:{table_id}:{frequency}",
                "source_dataset": "NIPA",
                "table_id": table_id,
                "frequency": frequency,
                "source_reported": source_reported,
                "curation": curation,
            },
            "observed": {
                "metric_names": metric_names,
                "unit_labels": _observed_values(rows, "CL_UNIT"),
                "unit_multipliers": _observed_values(rows, "UNIT_MULT"),
                "coverage": coverage,
                "empty_response": not rows,
            },
            "preview": {
                "row_count": len(rows),
                "columns": list(rows[0]) if rows else [],
                "date_coverage": response_date_coverage,
                "head": head,
                "tail": tail,
                **({"truncated_columns": truncated} if truncated else {}),
            },
            "caveats": caveats,
            "provenance": {
                "source_url": manifest["source_url"],
                "retrieved_at_utc": manifest["retrieved_at_utc"],
                "sha256": manifest["sha256"],
                "byte_count": manifest["byte_count"],
                "vintage": manifest["vintage"],
                "cache_key": manifest["cache_key"],
                "row_count": len(rows),
                "data_coverage": response_date_coverage,
                "source_request": manifest.get("source_request", {}),
                "source_metadata": manifest.get("source_metadata", {}),
            },
            "artifact": artifact.descriptor.as_dict(),
            "cache_hit": artifact.cache_hit,
        }
        if curated is not None:
            response["catalog"] = {
                "id": curated.id,
                "title": curated.title,
                "publisher": curated.publisher,
                "frequency": curated.frequency,
                "units": curated.units.model_dump(mode="json"),
                "history": curated.history.model_dump(mode="json"),
                "semantics": curated.semantics.model_dump(mode="json"),
                "verified_on": curated.verified_on.isoformat(),
            }
        return response

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
                "kind must be an official Z.1 series prefix: " + ", ".join(Z1_KIND_CODES)
            )
        prefix = canonical[:2]
        if prefix not in Z1_KIND_MEANINGS:
            raise ValueError(f"{canonical} does not carry an official supported Z.1 series prefix")
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
                    **loaded.package_metadata,
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
        # Remote file artifacts retain the exact downloaded representation. CSV
        # parsing is for preview and validation only; it must not rewrite source bytes.
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

    def _upstream_search(
        self,
        query: str,
        limit: int,
        *,
        family: str | None,
    ) -> tuple[list[dict[str, Any]], str, dict[str, Any] | None]:
        """Source-side search when the curated catalog has no match."""
        if family == "bea" or _looks_like_bea_nipa_query(query):
            try:
                inventory = self._bea_nipa_inventory()
            except SourceSchemaChangedError:
                try:
                    inventory = self._bea_nipa_inventory(refresh=True)
                except MissingKeyError:
                    return [], (
                        "The cached BEA NIPA inventory is unusable and BEA_API_KEY is unset, "
                        "so Fiscus cannot refresh source-reported table discovery."
                    ), None
                except FiscusError as exc:
                    return [], f"BEA NIPA inventory search failed after refresh: {exc}", None
            except MissingKeyError:
                return [], (
                    "No complete curated BEA match and no cached NIPA inventory is available. "
                    "Set BEA_API_KEY once to cache BEA's source-reported table inventory."
                ), None
            except FiscusError as exc:
                return [], f"BEA NIPA inventory search failed: {exc}", None
            upstream = search_bea_nipa_inventory(inventory.rows, query, limit)
            note = (
                "No complete curated catalog match; results below are from BEA's "
                "source-reported NIPA TableName inventory, cached in the platform Fiscus "
                "SQLite cache. They omit reviewed units, history, and verified_on claims. "
                "Fetch with bea_nipa_table using one advertised frequency."
                if upstream
                else "No complete curated match and BEA's NIPA table inventory returned nothing."
            )
            if inventory.diagnostics is not None:
                note += (
                    " Inventory normalization skipped "
                    f"{inventory.diagnostics['skipped_row_count']} malformed source row(s); "
                    "the valid rows remain searchable."
                )
            return upstream, note, inventory.diagnostics
        if family == "z1":
            try:
                upstream = self.z1.search_series(query, limit)
            except FiscusError as exc:
                return [], f"Z.1 package data-dictionary search failed: {exc}", None
            note = (
                "No complete curated catalog match; results below are from the current "
                "official Z.1 package data dictionary and carry no curated caveat review. "
                "Fetch with z1_series using the code and matching kind shown."
                if upstream
                else (
                    "No complete curated match and the Z.1 package data dictionary "
                    "returned nothing."
                )
            )
            return upstream, note, None
        if family not in {None, "fred"}:
            return (
                [],
                f"No curated catalog match or source-side search for family {family!r}.",
                None,
            )
        api_key = os.environ.get("FRED_API_KEY")
        if not api_key:
            return [], (
                "No curated catalog match. Uncataloged FRED or BLS series can still be "
                "fetched as fred.<SERIES_ID> or bls.<SERIES_ID> (no curated caveat review). "
                "Set FRED_API_KEY to enable upstream FRED search from this tool."
            ), None
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
                "vintage_support": vintage_support(ExecutorKind.FRED, "fred"),
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
        return upstream, note, None

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

_ALFRED_SNAPSHOT_CAVEAT = Caveat(
    id="fred.alfred_information_snapshot",
    severity=Severity.WARNING,
    message=(
        "This artifact is an ALFRED information snapshot as of the requested closed past "
        "date. It is not proof of the exact source-origin file or publication instant; "
        "ALFRED availability can follow the source release and may use fallback evidence."
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

_BEA_NIPA_OBSERVED_ROWS_CAVEAT = Caveat(
    id="bea.nipa_observed_rows",
    severity=Severity.CRITICAL,
    message=(
        "BEA returns only the observations present for each table line. Earlier periods may be "
        "absent with no blank placeholder, and different lines in one table may begin in "
        "different periods. Do not infer a line's history from the table-wide date range; "
        "inspect `coverage.by_line` for the selected `LineNumber`/`SeriesCode`."
    ),
)

_BEA_NIPA_CONCEPT_CAVEAT = Caveat(
    id="bea.nipa_quantity_chained_concepts",
    severity=Severity.CRITICAL,
    message=(
        "Quantity indexes and chained-dollar values produce the same percent-change measures, "
        "apart from rounding, but they are not interchangeable level concepts. Chained-dollar "
        "components are generally nonadditive away from the reference year; do not substitute "
        "one metric for the other when a dollar level, share, aggregation, or contribution is "
        "required."
    ),
)


def _serialize_route_caveat(caveat: Caveat) -> dict[str, Any]:
    return {
        "id": caveat.id,
        "severity": caveat.severity.value,
        "message": caveat.message,
        "binds": True,
        "when": caveat.when.model_dump(mode="json"),
        "source_note": caveat.source_note,
    }


def _validate_bea_nipa_request(
    table_id: str,
    frequency: str,
    start_year: int,
    end_year: int,
) -> tuple[int, int]:
    if not BEA_TABLE_ID.fullmatch(table_id):
        raise InvalidRequestError("table_id must be a canonical BEA NIPA TableName such as T10106")
    if frequency not in BEA_FREQUENCIES:
        raise InvalidRequestError("frequency must be one of A, Q, or M")
    first_year = _coerce_bea_year(start_year)
    last_year = _coerce_bea_year(end_year)
    if first_year > last_year:
        raise InvalidRequestError("start_year must not exceed end_year")
    if last_year - first_year > 500:
        raise InvalidRequestError("year range must not exceed 501 inclusive years")
    return first_year, last_year


def _coerce_bea_year(value: Any) -> int:
    message = "start_year and end_year must be four-digit integers"
    if isinstance(value, bool):
        raise InvalidRequestError(message)
    try:
        year = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise InvalidRequestError(message) from exc
    if value != year and str(value).strip() != str(year):
        raise InvalidRequestError(message)
    if not 1000 <= year <= 9999:
        raise InvalidRequestError(message)
    return year


def normalize_bea_nipa_inventory(rows: list[dict[str, str]]) -> BeaNipaInventory:
    normalized: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        table_id = (row.get("TableName") or row.get("Key") or "").strip().upper()
        description = (row.get("Description") or row.get("Desc") or "").strip()
        problems: list[str] = []
        if not BEA_TABLE_ID.fullmatch(table_id):
            problems.append("TableName/Key is absent or noncanonical")
        if not description:
            problems.append("Description/Desc is absent")
        if table_id and table_id in seen:
            problems.append(f"TableName {table_id} is duplicated")
        frequencies = sorted(set(BEA_INVENTORY_FREQUENCY.findall(description)))
        if description and not frequencies:
            problems.append("Description/Desc advertises no A/Q/M frequency")
        if problems:
            issues.append(
                {
                    "row_index": index,
                    "fields": {
                        "TableName": row.get("TableName"),
                        "Key": row.get("Key"),
                        "Description": row.get("Description"),
                        "Desc": row.get("Desc"),
                    },
                    "problems": problems,
                }
            )
            continue
        seen.add(table_id)
        normalized.append(
            {
                "TableName": table_id,
                "Description": description,
                "advertised_frequencies": frequencies,
            }
        )
    if not normalized:
        sample = issues[:3]
        raise SourceSchemaChangedError(
            "BEA NIPA inventory has no usable table rows; "
            f"received_rows={len(rows)}, skipped_rows={len(issues)}, "
            f"field_context={sample!r}"
        )
    diagnostics = None
    if issues:
        diagnostics = {
            "skipped_row_count": len(issues),
            "received_row_count": len(rows),
            "sample_issues": issues[:3],
            "sample_truncated": len(issues) > 3,
        }
    return BeaNipaInventory(rows=normalized, diagnostics=diagnostics)


def bea_nipa_coverage(
    rows: list[dict[str, str]],
    requested_table_id: str,
) -> dict[str, Any]:
    by_line: dict[tuple[str, str], dict[str, Any]] = {}
    seen_observations: set[tuple[str, str, str]] = set()
    for row in rows:
        table_id = row.get("TableName", "").strip().upper()
        line_number = row.get("LineNumber", "").strip()
        period = row.get("TimePeriod", "").strip()
        if table_id != requested_table_id or not line_number or not period:
            raise SourceSchemaChangedError(
                "BEA NIPA data row lacks the requested TableName, LineNumber, or TimePeriod"
            )
        observation_key = (table_id, line_number, period)
        if observation_key in seen_observations:
            raise SourceSchemaChangedError(
                f"BEA NIPA response repeats line {line_number} period {period}"
            )
        seen_observations.add(observation_key)
        line_key = (table_id, line_number)
        item = by_line.setdefault(
            line_key,
            {
                "periods": [],
                "series_codes": set(),
                "line_descriptions": set(),
                "cl_units": set(),
                "unit_mults": set(),
                "metric_names": set(),
            },
        )
        item["periods"].append(period)
        for field, key in (
            ("SeriesCode", "series_codes"),
            ("LineDescription", "line_descriptions"),
            ("CL_UNIT", "cl_units"),
            ("UNIT_MULT", "unit_mults"),
            ("METRIC_NAME", "metric_names"),
        ):
            value = row.get(field, "").strip()
            if value:
                item[key].add(value)

    lines: list[dict[str, Any]] = []
    for (_, line_number), item in sorted(by_line.items(), key=lambda pair: _line_sort(pair[0][1])):
        series_codes = sorted(item["series_codes"])
        if len(series_codes) > 1:
            raise SourceSchemaChangedError(
                f"BEA NIPA line {line_number} carries conflicting SeriesCode values: "
                + ", ".join(series_codes)
            )
        descriptions = sorted(item["line_descriptions"])
        periods = item["periods"]
        line = {
            "line_number": line_number,
            "series_code": series_codes[0] if series_codes else None,
            "line_description": descriptions[0] if len(descriptions) == 1 else None,
            "first_observed_period": min(periods),
            "last_observed_period": max(periods),
            "observation_count": len(periods),
            "observed_cl_unit": sorted(item["cl_units"]),
            "observed_unit_mult": sorted(item["unit_mults"]),
            "observed_metric_name": sorted(item["metric_names"]),
        }
        if len(descriptions) > 1:
            line["observed_line_descriptions"] = descriptions
        lines.append(line)

    periods = [row.get("TimePeriod", "") for row in rows if row.get("TimePeriod")]
    return {
        "basis": "observed_rows_in_this_artifact",
        "continuity_assessed": False,
        "table": {
            "first_observed_period_any_line": min(periods) if periods else None,
            "last_observed_period_any_line": max(periods) if periods else None,
            "row_count": len(rows),
            "line_count": len(lines),
        },
        "by_line": lines,
    }


def _line_sort(line_number: str) -> tuple[int, int | str]:
    return (0, int(line_number)) if line_number.isdigit() else (1, line_number)


def _observed_values(rows: list[dict[str, str]], field: str) -> list[str]:
    return sorted({row[field].strip() for row in rows if row.get(field, "").strip()})


def _looks_like_bea_nipa_query(query: str) -> bool:
    normalized = query.strip().upper()
    return bool(
        re.search(r"\bNIPA\b", normalized)
        or re.search(r"\bT[A-Z0-9]{5,6}\b", normalized)
        or re.search(r"\b\d{1,2}\.\d{1,2}(?:\.\d{1,2})?\b", normalized)
    )


def _query_bea_table_id(query: str) -> str | None:
    direct = re.search(r"\b(T[A-Z0-9]{5,6})\b", query.upper())
    if direct:
        return direct.group(1)
    numbered = re.search(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{1,2}))?\b", query)
    if not numbered:
        return None
    section, table, subtable = numbered.groups()
    canonical = f"T{int(section)}{int(table):02d}{int(subtable or 0):02d}"
    return canonical if BEA_TABLE_ID.fullmatch(canonical) else None


def search_bea_nipa_inventory(
    inventory: list[dict[str, Any]],
    query: str,
    limit: int,
) -> list[dict[str, Any]]:
    exact_table = _query_bea_table_id(query)
    tokens = set(re.findall(r"[a-z0-9]+", query.casefold()))
    ranked: list[tuple[int, str, dict[str, Any]]] = []
    for item in inventory:
        table_id = item["TableName"]
        if exact_table is not None and table_id != exact_table:
            continue
        description = item["Description"]
        haystack = f"{table_id} {description}".casefold()
        score = 1000 if exact_table == table_id else sum(1 for token in tokens if token in haystack)
        if score <= 0:
            continue
        ranked.append((score, table_id, item))
    ranked.sort(key=lambda value: (-value[0], value[1]))
    return [
        {
            "source_dataset": "NIPA",
            "table_id": item["TableName"],
            "source_description": item["Description"],
            "advertised_frequencies": item["advertised_frequencies"],
            "curation": {"status": "uncurated", "catalog_id": None},
            "tools": [ToolName.BEA_NIPA_TABLE.value],
            "source": "bea_nipa_inventory",
        }
        for _, _, item in ranked[:limit]
    ]


def _filter_rows_by_date(
    rows: list[dict[str, str]],
    *,
    start: date | None,
    end: date | None,
) -> list[dict[str, str]]:
    if start is None and end is None:
        return rows
    filtered: list[dict[str, str]] = []
    for row in rows:
        observed = _safe_date(row.get("date"))
        if observed is None:
            continue
        if start is not None and observed < start:
            continue
        if end is not None and observed > end:
            continue
        filtered.append(row)
    return filtered


def _safe_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _closed_past_vintage(value: str | date | None) -> date | None:
    if value is None:
        return None
    try:
        parsed = parse_date(value)
    except (TypeError, ValueError) as exc:
        raise InvalidVintageError(
            "vintage_as_of must be an ISO date in YYYY-MM-DD form"
        ) from exc
    if parsed is None:  # pragma: no cover - guarded by value is not None
        return None
    today_utc = datetime.now(UTC).date()
    if parsed >= today_utc:
        raise InvalidVintageError(
            f"vintage_as_of must be strictly earlier than the current UTC date "
            f"{today_utc.isoformat()}; omit it for current data"
        )
    return parsed


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
    "FA": Caveat(
        id="z1.fa.saar_transaction",
        severity=Severity.CRITICAL,
        message=(
            "FA is a seasonally adjusted annual-rate transaction flow. Divide the published "
            "value by 4 exactly once to estimate the transaction during the quarter; do not "
            "sum four published quarterly SAAR observations."
        ),
    ),
    "FC": Caveat(
        id="z1.fc.change_not_transaction",
        severity=Severity.CRITICAL,
        message=(
            "FC is the change in an unadjusted level. It can include transactions, "
            "revaluations, and other changes in volume; do not label it as transactions."
        ),
    ),
    "FG": Caveat(
        id="z1.fg.growth_rate",
        severity=Severity.CRITICAL,
        message="FG is a seasonally adjusted growth rate, not a dollar level or flow.",
    ),
    "FI": Caveat(
        id="z1.fi.index",
        severity=Severity.CRITICAL,
        message="FI is an index. Do not interpret its observations as dollar levels or flows.",
    ),
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
    "FR": Caveat(
        id="z1.fr.revaluation_not_transaction",
        severity=Severity.CRITICAL,
        message=(
            "FR is a revaluation (holding gain or loss), not a transaction flow or the "
            "total change in the corresponding level."
        ),
    ),
    "FS": Caveat(
        id="z1.fs.seasonal_factor",
        severity=Severity.CRITICAL,
        message="FS is a seasonal factor, not a transaction flow or a level.",
    ),
    "FV": Caveat(
        id="z1.fv.other_volume_change",
        severity=Severity.CRITICAL,
        message=(
            "FV records other changes in volume, such as discontinuities or breaks. Do not "
            "label it as transactions or holding gains."
        ),
    ),
    "LA": Caveat(
        id="z1.la.level_not_flow",
        severity=Severity.CRITICAL,
        message="LA is a seasonally adjusted level. Do not present its change as transactions.",
    ),
    "LM": Caveat(
        id="z1.lm.valuation_effects",
        severity=Severity.CRITICAL,
        message=(
            "LM is a market-value or current-cost level, not seasonally adjusted. Its change "
            "includes valuation and other adjustments; do not present the level change as "
            "purchases or transaction flow."
        ),
    ),
    "PC": Caveat(
        id="z1.pc.index_percent_change",
        severity=Severity.CRITICAL,
        message="PC is a percent change in an index, not an index level or a dollar flow.",
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
    units_lower = units_label.casefold()
    annualized = "annual rate" in units_lower
    if "millions of dollars" in units_lower:
        unit_class = "usd_millions_saar" if annualized else "usd_millions"
    else:
        unit_class = "not_saar"
    frequency = "annual" if code.endswith(".A") else "quarterly"
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
        units=UnitSpec(label=units_label, annualized=annualized, unit_class=unit_class),
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
