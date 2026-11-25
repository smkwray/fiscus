# fiscus

`fiscus` is a Python command-line tool, library and Model Context Protocol (MCP) server for US macroeconomic, Treasury and financial data. It is for researchers and AI agents: you search a curated catalog, fetch a series, table or file, and get a preview with its units, the caveats that apply to your request, and a record of where the data came from. Every fetch is cached locally.

It is alpha software with one maintainer and no support or uptime commitment. Government endpoints change without notice; each catalog entry records the date it was last checked (`verified_on`).

## Sources

| Source | Examples | API key |
|---|---|---|
| Treasury FiscalData | Daily Treasury Statement (DTS), Monthly Statement of the Public Debt (MSPD), auctions | none |
| Federal Reserve | System Open Market Account (SOMA) holdings, Financial Accounts of the United States (Z.1) | none |
| FRED (Federal Reserve Economic Data) | any series; past vintages from ALFRED (archival FRED) | optional `FRED_API_KEY` |
| Bureau of Economic Analysis (BEA) | National Income and Product Accounts (NIPA) tables | required `BEA_API_KEY` |
| Bureau of Labor Statistics (BLS) | any series | optional `BLS_API_KEY` |
| Others | Treasury International Capital (TIC), TreasuryDirect, Office of Financial Research (OFR) money market funds, New York Fed reference rates, Congressional Budget Office (CBO) baselines, Census, San Francisco Fed | none |

Catalog entries carry reviewed units, history start dates and known pitfalls, such as a Wednesday level versus a weekly average. Data outside the catalog is still available and labeled as unreviewed: any FRED or BLS series as `fred.<SERIES_ID>` or `bls.<SERIES_ID>`, any Z.1 series by mnemonic, and any BEA NIPA table. A few bls.gov and cbo.gov files block automated downloads, so fetching one fails with a `browser_download_only` error; download those in a browser.

## Install

Requires Python 3.11 or later and [uv](https://docs.astral.sh/uv/).

```bash
uv tool install git+https://github.com/smkwray/fiscus.git
# or run once without installing:
uvx --from git+https://github.com/smkwray/fiscus.git fiscus find-series "Treasury General Account"
```

## Quick start

These commands need no API key:

```bash
fiscus find-series "TGA Wednesday level"
fiscus get-series fred.wdtgal --start 2024-01-01
fiscus fiscaldata-query fiscaldata.dts_operating_cash_balance \
  --param 'filter=record_date:gte:2024-01-01,record_date:lte:2024-03-31' \
  --param 'sort=record_date'
fiscus cache export CACHE_KEY --out wdtgal.csv
```

`find-series` returns the dataset ID and the command that fetches it. Each fetch prints JSON with a short preview, row count, date coverage, units, the caveats that apply, provenance, and an `artifact` whose `cache_key` identifies the full payload; `cache export` writes that payload to a file. A unique prefix of at least 12 characters can stand in for a cache key.

## Commands

```text
fiscus find-series QUERY [--family FAMILY] [--limit N]
fiscus get-series DATASET_ID [--start DATE] [--end DATE] [--vintage-as-of DATE]
fiscus bea-nipa-table TABLE_ID {A,Q,M} START_YEAR END_YEAR
fiscus fiscaldata-query DATASET_ID [--param KEY=VALUE ...] [--max-pages N]
fiscus get-file DATASET_ID
fiscus z1-series CODE KIND [--start DATE] [--end DATE]
fiscus soma-holdings [--asof DATE] [--cusip CUSIP]
fiscus source-caveats DATASET_ID [--start DATE] [--end DATE]
fiscus provenance CACHE_KEY
fiscus cache export CACHE_KEY --out PATH
fiscus catalog lint
```

Fetch commands accept `--refresh` to skip the cache; `fiscus COMMAND --help` lists every option. `--vintage-as-of` returns a FRED series as published on a past date and needs `FRED_API_KEY`. `z1-series` takes a mnemonic such as `FL313020005.Q` and its two-letter type prefix as `KIND` (`FL` for levels, `FU` for transactions), and refuses a mismatch.

## Configuration

| Variable | Effect |
|---|---|
| `FRED_API_KEY` | Uses the FRED API instead of its public CSV download; needed for vintages and FRED search |
| `BEA_API_KEY` | Required for BEA tables |
| `BLS_API_KEY` | Raises BLS limits from 10 to 20 years per request and from 25 to 500 requests a day |
| `FISCUS_DATA_DIR` | Cache location (default: the platform's user cache directory; `--data-dir` overrides it) |
| `FISCUS_EXTRA_CATALOG` | Your own catalog YAML files or directories |
| `FISCUS_USER_AGENT` | Replaces the default HTTP User-Agent |

Keys are read only from environment variables. The cache is one SQLite database; requests for current data are fetched again after 24 hours, while a FRED vintage, a dated SOMA snapshot or a fixed release file stays cached. Your operating system may clear it, so export anything you need to keep.

`FISCUS_EXTRA_CATALOG` entries add to or override bundled entries by `id`, and can point at a file already on disk. See [catalog/CONTRIBUTING.md](catalog/CONTRIBUTING.md) and [catalog/SCHEMA.md](catalog/SCHEMA.md).

## MCP server

```json
{
  "mcpServers": {
    "fiscus": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/smkwray/fiscus.git", "fiscus-mcp"]
    }
  }
}
```

From a checkout, run `uv run fiscus-mcp`, or `uv run --env-file PATH fiscus-mcp` to load keys from a file. The stdio server offers nine read-only tools that match the fetch and lookup commands. Results carry the artifact descriptor, not a file path; clients read the exact payload from the resource `fiscus-cache://artifact/{cache_key}`.

## Python

```python
from pathlib import Path
from fiscus import FiscusService

with FiscusService() as fiscus:
    result = fiscus.get_series("fred.wdtgal", start="2024-01-01")
    key = result["artifact"]["cache_key"]

    payload = fiscus.read_bytes(key)              # exact bytes
    rows = list(fiscus.iter_csv_rows(key))        # CSV artifacts only
    fiscus.export_artifact(key, Path("wdtgal.csv"))
```

Failures raise `fiscus.errors.FiscusError` subclasses with a stable `code`, such as `missing_key`, `source_removed` (HTTP 404 or 410) or `source_access_denied` (HTTP 403 or 451). The CLI prints the code and exits with status 2.

## Development

```bash
uv sync --frozen --group dev
uv run fiscus catalog lint
uv run pytest
uv run ruff check .
```

Tests run offline against recorded responses. A weekly GitHub Actions workflow makes one live request per source family and opens an issue when one fails.

## License

MIT. See [LICENSE](LICENSE).
