# fiscus

**US economic data MCP server — curated catalog of macro, Treasury, and financial sources with the caveats attached**

fiscus leads with the plumbing that generic macro-data wrappers usually omit: Treasury FiscalData, MSPD, DTS, SOMA, Z.1 semantics, TIC, and OFR money-fund monitors — alongside BEA national accounts, BLS labor series, CBO baselines, NY Fed reference rates, TreasuryDirect auctions, and selected FRED series. Curated fetches are catalog-selected and labeled with reviewed units and known footguns; explicitly uncurated routes remain labeled as such. Fetches are cached with immutable provenance. Sources that reject automated clients (bls.gov tables, cbo.gov workbooks) are cataloged as browser-download-only so agents get an explicit diagnosis instead of a mysterious 403.

## Install

```bash
uv tool install fiscus
```

For a one-off invocation without installation:

```bash
uvx fiscus find-series "Treasury General Account"
```

## Zero-key quickstart

FRED public CSV and Treasury FiscalData work without credentials.

```bash
uvx fiscus find-series "TGA Wednesday level"
uvx fiscus get-series fred.wdtgal --start 2024-01-01
uvx fiscus fiscaldata-query fiscaldata.dts_operating_cash_balance \
  --param 'filter=record_date:gte:2024-01-01' \
  --param 'sort=record_date' \
  --max-pages 2
```

Set `FRED_API_KEY` to use the official FRED observations API instead of the public graph CSV path and to request historical ALFRED information snapshots, `BEA_API_KEY` for BEA national-accounts tables (required), and `BLS_API_KEY` to raise BLS query limits (optional). Keys are read from environment variables only; the MCP server can be launched with `uv run --env-file` pointing at a private env file.

For a FRED series, `--vintage-as-of YYYY-MM-DD` selects the information available
to ALFRED on one closed past date. `--start` and `--end` remain inclusive
observation-date bounds; they are never treated as vintage dates. Today and future
information dates are rejected because today's information set remains mutable.

## MCP server

The server has a dedicated executable so an interactive `fiscus` command never appears to hang on stdio:

```bash
uvx --from fiscus fiscus-mcp
```

Generic client configuration:

```json
{
  "mcpServers": {
    "fiscus": {
      "command": "uvx",
      "args": ["--from", "fiscus", "fiscus-mcp"]
    }
  }
}
```

From a checkout, use `uv run fiscus-mcp` instead.

Cache-backed tool results contain an artifact descriptor, not a server-local pathname. MCP clients read the exact payload through the read-only resource template
`fiscus-cache://artifact/{cache_key}`. The resource returns byte-exact content, including binary workbooks and archives; use the descriptor's `mime_type` and `filename` when saving or interpreting it. This keeps the nine read-only tools focused on economic-data operations rather than adding a server-side export tool whose path would still be inaccessible to a remote MCP client.

## Command surface

```text
fiscus find-series QUERY
fiscus get-series DATASET_ID [--start DATE] [--end DATE] [--vintage-as-of DATE]
fiscus bea-nipa-table TABLE_ID {A,Q,M} START_YEAR END_YEAR
fiscus fiscaldata-query DATASET_ID [--param KEY=VALUE] [--max-pages N]
fiscus get-file DATASET_ID
fiscus z1-series CODE KIND [--start DATE] [--end DATE]
fiscus soma-holdings [--asof DATE] [--cusip CUSIP]
fiscus source-caveats DATASET_ID
fiscus provenance CACHE_KEY
fiscus cache export CACHE_KEY --out PATH
fiscus cache migrate-v1 [--from PATH]
fiscus catalog lint
fiscus mcp
```

Call `find-series` before a fetch command. Dataset identifiers are curated catalog IDs — but coverage is not limited to the catalog:

- **Uncataloged passthrough:** any FRED or BLS series is fetchable as `fred.<SERIES_ID>` / `bls.<SERIES_ID>`. Keyed FRED requests derive units, seasonal adjustment, annualization, and a conservative `unit_class` from official series metadata. Detected annual-rate series receive an explicit interpretation caveat; the keyless FRED graph route and current BLS data route report annualization as unknown and warn the caller to verify before treating observations as period flows. Every passthrough remains marked "uncataloged — no curated caveat review." The catalog is for caveats, not coverage.
- **Upstream search fallback:** when `find-series` has no complete catalog match, it appends candidates from FRED's own search (requires `FRED_API_KEY`), clearly marked uncurated, so an empty result is a lead rather than a dead end.
- **Curated release files:** Census vintage files and archived BLS releases carry a
  logical release id and representation. Their immutable source identity appends the
  SHA-256 of exact downloaded bytes, so a correction at the same URL creates a new
  revision while ETag and Last-Modified remain transport metadata.
- **Z.1 package discovery:** `find-series QUERY --family z1` searches the current official package data dictionary without a key and returns the exact `z1-series CODE KIND` call for uncataloged matches.
- **BEA NIPA table access:** `find-series QUERY --family bea` falls back to BEA's source-reported NIPA table inventory. The inventory is cached in Fiscus's platform SQLite cache and refreshed once before an apparent table/frequency miss is rejected. `bea-nipa-table` fixes `datasetname=NIPA`, requires explicit inclusive years, and returns per-line observed coverage. Uncurated results and responses omit reviewed history, units, and `verified_on`; an exact cataloged table/frequency pair adds a separate curation overlay. `get-series bea.nipa_t10106` remains a deprecated one-release alias.
- **Local artifacts:** private overlay entries can register files already on disk via the `local_file` executor (`endpoint.local_path`) — served in place with a content-hash provenance block, never copied. See `catalog/CONTRIBUTING.md`.

`z1-series` additionally accepts any mnemonic from the official Z.1 CSV package (including FRED-style `BOGZ1` aliases) and enforces the mnemonic's official series-type prefix, including transactions, levels, changes, revaluations, other volume changes, seasonal factors, growth rates, and indexes. One package download per release serves every series, and uncataloged mnemonics are labeled from the package's own data dictionary. `soma-holdings` resolves the latest weekly snapshot date before consulting the cache, so "latest" never freezes to a stale snapshot. `provenance` re-emits the immutable manifest and artifact descriptor for a full cache key or unique prefix.

For `z1-series`, `KIND` must be the mnemonic's matching official prefix: `FA`, `FC`, `FG`, `FI`, `FL`, `FR`, `FS`, `FU`, `FV`, `LA`, `LM`, or `PC`. The deliberate redundancy prevents one economic concept from being relabeled as another.

## Return convention (v0.2)

Cache-backed fetches return a small head/tail preview, row count, date coverage, units, binding caveats, a provenance block, and an opaque `artifact` descriptor:

```json
{
  "artifact": {
    "cache_key": "<64 lowercase hex characters>",
    "filename": "data.csv",
    "mime_type": "text/csv",
    "sha256": "<payload digest>",
    "byte_count": 1234
  }
}
```

`full_data_path` and `manifest_path` are deliberately absent from cache-backed responses. The SQLite row is the cache artifact; fiscus does not create a per-request file forest or a temporary materialization tree. The descriptor is duplicated only where useful: its identity and integrity fields also remain in the provenance block.

Python callers can obtain full content without relying on a path:

```python
from pathlib import Path
from fiscus import FiscusService

with FiscusService() as fiscus:
    result = fiscus.get_series("fred.wdtgal", start="2024-01-01")
    key = result["artifact"]["cache_key"]

    payload = fiscus.read_bytes(key)                  # exact bytes
    rows = list(fiscus.iter_csv_rows(key))            # canonical CSV only
    receipt = fiscus.export_artifact(key, Path("wdtgal.csv"))
```

`read_bytes`, `iter_csv_rows`, `provenance`, and `cache export` accept either the full key or a unique hexadecimal prefix of at least 12 characters. `iter_csv_rows` rejects non-CSV artifacts instead of guessing how to parse a workbook or archive.

Expected failures have stable Python/CLI codes. In particular, HTTP 404/410 is
`source_removed`, HTTP 403/451 is `source_access_denied`, and a statically declared
nonmachine route is `browser_download_only`. A transient access denial is not
silently promoted into a permanent catalog fact.

### Cache lifetime versus caller-owned files

The cache defaults to the platform's device-local cache directory and can be relocated with `FISCUS_DATA_DIR`. Its quiescent `cache/v2` surface is one `fiscus.sqlite3` file; payloads, manifests, and latest pointers are rows in that database. SQLite uses rollback-journal `DELETE` mode, so a journal may exist during a write but no persistent WAL/SHM sidecars or artifact directories remain at rest.

The cache is durable across fiscus processes but is still a cache: operating-system cleanup, an explicit user deletion, or a replaced device can remove it. `cache export` / `export_artifact` creates a caller-owned file at the requested destination when archival or handoff durability matters. Conversely, a `local_file` catalog entry already points to caller-owned content, so its response keeps the real in-place `full_data_path` and `manifest_path: null` and never enters SQLite.

`--refresh` bypasses the latest pointer and re-reads the source. Current FRED responses use HTTP validators or retrieval UTC as immutable identity; their default real-time dates remain metadata. Historical FRED requests use `alfred-as-of:DATE`, and the date also enters canonical request parameters. Bytes already stored under one dataset/canonical-params/vintage identity are never mutated.

### Legacy v1 migration

A legacy filesystem cache is migrated only by the finite, explicit command:

```bash
fiscus cache migrate-v1
# or
fiscus cache migrate-v1 --from /path/to/cache/v1
```

Migration reads v1 without deleting or rewriting it, inserts valid artifacts directly into SQLite, preserves each usable authoritative `latest.json` pointer, and reports path-specific diagnostics for corrupt or incomplete legacy artifacts. It never materializes cache-owned files and does not enable dual-write.

## Catalog overlays

Set `FISCUS_EXTRA_CATALOG` to one or more YAML files or directories separated by the operating-system path separator. Overlay entries merge by `id`; mapping fields merge recursively and lists replace. New entries must be complete after merging.

```bash
FISCUS_EXTRA_CATALOG=./local-catalog uv run fiscus catalog lint
```

See `catalog/SCHEMA.md` and `catalog/schema.json`.

## Development

```bash
uv sync --frozen --group dev
uv run fiscus catalog lint
uv run pytest
uv run ruff check .
```

The unit suite is offline-first and uses tiny recorded response fixtures. A separate weekly workflow makes one cheap live request for each implemented family and opens or updates a single issue if a source fails.

## Status

This is an alpha, solo-maintained public-data utility. Source endpoints can drift; catalog entries carry `verified_on` dates, and the project makes no support or uptime promise.
