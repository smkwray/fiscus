# Catalog schema v1

The catalog is the product knowledge base. Fetch code dispatches on `access.executor`; adding a dataset within an implemented family should require a catalog entry, not a new adapter.

## File layout

Use one YAML file per source family. Every file has this shape:

```yaml
schema_version: 1
family: fiscaldata
entries:
  - id: fiscaldata.example
    # complete entry
```

`schema_version` and `family` are required. `entries` must be a non-empty list. Every entry id must begin with `<family>.`, and ids are stable public API identifiers.

## Entry fields

Required fields:

| Field | Meaning |
|---|---|
| `id` | Stable catalog id, lowercase with dots, dashes, or underscores. |
| `family` | Source family used for search and file validation. |
| `title` | Human-readable concept name. |
| `description` | What the dataset measures, without analysis claims. |
| `publisher` | Primary publisher; a distributor may be named second. |
| `verified_on` | Date on which endpoint and semantics were checked. |
| `history` | `start`, optional `end`, and optional boundary notes. |
| `frequency` | Source frequency and timing convention. |
| `units` | Label, scale, seasonal treatment, annualization, and `unit_class`. `annualized` is `true`/`false` when known; synthesized passthrough entries may return `null` when their source route supplies no unit metadata. |
| `endpoint` | URL plus executor-specific details. |
| `access` | Executor, machine/browser mode, and authentication rule. |
| `response` | Source format, row path, date/value fields, pagination, and vintage strategy. |
| `refresh` | Expected source cadence and optional timing notes. |
| `tools` | Fetch tools allowed for the entry. |

Optional fields:

- `aliases` and `tags` improve `find_series` recall.
- `semantics` records an official Z.1 series-type prefix, granularity, accounting basis, and other concept notes.
- `release` records a curated file's logical release id, representation, publication
  and correction dates, and official discovery/layout/errata locators. It is valid
  only with `logical_release_plus_sha256`.
- `caveats` records unconditional or conditional warnings.
- `endpoint.api_url` and `endpoint.series_id` support FRED's keyed and keyless routes.
- `endpoint.path_template` supports date-addressed routes such as SOMA.
- `endpoint.default_params` supplies safe source defaults. `allowed_params` is an explicit pass-through allowlist.

Unknown fields fail validation. This is deliberate: misspellings must not silently become inert catalog data.

## Access and keys

```yaml
access:
  executor: fred
  mode: machine
  auth:
    mode: optional
    env_var: FRED_API_KEY
    note: Keyless fallback remains available.
```

`auth.mode` is `none`, `optional`, or `required`. Optional and required auth must name an environment variable. Secret values never belong in YAML, request parameters persisted to cache, or manifests.

Set `mode: browser_download_only` with `executor: browser_download` only for a
statically confirmed nonmachine route. A live HTTP 403 or 451 from a machine entry is
reported as `source_access_denied`; it does not by itself establish a permanent
browser-only fact. HTTP 404 or 410 is reported separately as `source_removed`.

Each catalog entry declares exactly one executable public fetch operation, selected
from the operations compatible with its transport executor:

| Executor | Compatible one-item `tools` values |
|---|---|
| `fred`, `bls` | `[get_series]` |
| `json_api` | `[get_series]` or `[bea_nipa_table]` |
| `fiscaldata` | `[fiscaldata_query]` |
| `z1` | `[z1_series]` |
| `soma` | `[soma_holdings]` |
| `file`, `local_file`, `browser_download` | `[get_file]` |

Duplicate, multi-operation, or incompatible declarations fail both Pydantic and
generated-JSON-Schema validation. `browser_download` and `browser_download_only` also
imply one another; a catalog entry cannot advertise a machine route through the
browser-only executor.

The parameterized `bea_nipa_table` route reaches source-advertised NIPA tables without
constructing synthetic catalog entries. Its source inventory is cached in the platform
Fiscus SQLite cache under the internal `bea_nipa_inventory` identity; an apparent table
or frequency miss forces one inventory refresh before rejection. Uncurated discovery
results omit `history_start`, `units`, and `verified_on`, and curation is applied only as
a response-time overlay for an exact table/frequency catalog pair.

## Conditional caveats

A caveat has an id, severity, message, optional source note, and a `when` rule. Predicates use three-valued evaluation: true, false, or unknown when the caller did not provide enough context. Fetch results attach only true caveats; `source_caveats` returns all three states.

```yaml
caveats:
  - id: fiscaldata.dts.schema_seam_2022_04
    severity: critical
    message: Check both schema eras when the request crosses April 2022.
    when:
      match: all
      predicates:
        - type: date_range_crosses
          boundary: 2022-04-01
```

Supported predicates:

| Predicate | Binds when |
|---|---|
| `always` | Unconditionally. |
| `date_range_intersects` | Requested range overlaps `start` through optional `end`. |
| `date_range_crosses` | Requested range spans a boundary date. |
| `param_equals` | A named request parameter equals one value. |
| `param_in` | A named request parameter is in an allowed value list. |
| `unit_mixing` | A planned comparison includes one of the named `unit_class` values. |
| `kind_in` | Z.1 semantic context is one of the official series-type prefixes. |

`when.match` is `all` or `any`. `always` cannot be combined with another predicate.

For an annualized flow level, keep two concepts separate: the interpretation caveat
(`this value is annualized; divide by the number of periods per year for a period
flow`) is unconditional, while a compatibility caveat about mixing SAAR and
not-annualized series may use `unit_mixing`. Do not gate the basic annualization
interpretation on comparison context.

## Vintage and coverage

`history` and the returned data date range describe **coverage**, not vintage. `response.vintage_strategy` selects source-version evidence:

- `fred_realtime_or_http`: HTTP validators or retrieval UTC identify current FRED
  bytes; FRED's default current real-time interval remains provenance metadata.
- `http_last_modified_or_retrieval`: HTTP `Last-Modified`, then retrieval UTC.
- `logical_release_plus_sha256`: a curated file's logical release id and
  representation plus the SHA-256 of exact downloaded bytes. ETag and Last-Modified
  remain transport metadata.
- `release_date`: a dated release identifier for package-style sources.
- `retrieval`: retrieval UTC only.

A fallback retrieval timestamp intentionally creates a new vintage on each refreshed pull. Bytes already stored under one `(dataset, parameters, vintage)` key are immutable; different bytes under the same key are a cache collision.

Remote CSV and TSV files retain their exact source bytes. Their descriptors preserve
the corresponding `.csv`/`.tsv` representation and MIME type, and cache-hit previews
parse with the matching delimiter.

```yaml
release:
  logical_id: bls.empsit.2020-01-10.usdl-20-0010
  representation: pdf
  published_on: 2020-01-10
  corrected_on: 2020-02-21
  discovery_url: https://www.bls.gov/bls/news-release/empsit.htm
response:
  format: binary
  vintage_strategy: logical_release_plus_sha256
```

## Overlays

`FISCUS_EXTRA_CATALOG` accepts one or more files or directories separated by the operating-system path separator. Overlay documents use the same top-level `schema_version`, `family`, and `entries` keys. Entries merge by id; mappings merge recursively and lists replace.

An overlay may patch an existing entry:

```yaml
schema_version: 1
family: fred
entries:
  - id: fred.wdtgal
    aliases:
      - local TGA point-in-time alias
```

A new overlay id must be complete after merging and pass the same schema.

## Validation

Pydantic models are the executable validation source. The generated JSON Schema is committed as `catalog/schema.json` for editors and external tooling.

```bash
fiscus catalog lint
fiscus catalog schema
```

`lint` loads bundled files, applies configured overlays, validates cross-field rules, rejects duplicate ids, and reports entry and caveat counts.
