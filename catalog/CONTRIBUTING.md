# Adding catalog entries

The catalog is for **caveats, not coverage**. Executors already know how each source
family works, and uncataloged FRED/BLS/Z.1 series are served on demand with a generic
warning. Add an entry only when there is something worth encoding: a unit
trap, a history seam, an access quirk, or a series important enough to deserve verified
metadata and good aliases.

## Verify and add an entry

1. **Probe the endpoint live.** Fetch one small response. Confirm: status 200 for a
   non-browser client, response shape, the earliest available observation (query with
   an ascending sort or an early date filter), units and their scale.
2. **Pick the family file** (`catalog/<family>.yaml`), or create one with the standard
   header (`schema_version: 1`, `family: <name>`). One file per source family.
3. **Copy the nearest existing entry as a template** and edit every field. Aliases are
   what `find_series` matches — include the official mnemonic, common shorthand, and
   the phrases an economist would actually type.
4. **Write caveats as instructions, not descriptions.** A good caveat message tells the
   reader what to do or not do ("Use fred.wtregen for the weekly-average concept"), and
   uses a `when:` predicate if it only binds in some contexts (date ranges, parameter
   values, unit mixing). For SAAR flow levels, the divide-by-N interpretation is always
   applicable and must be an unconditional caveat; keep any cross-series unit-mixing
   warning as a separate conditional caveat.
5. **Validate:** `uv run fiscus catalog lint` must pass (strict schema, ID uniqueness,
   executor contracts), then `uv run pytest` (entries are data; the suite stays green).
6. **Set `verified_on`** to the date you checked the endpoint.

## Sources that block automated clients

A single HTTP 403 or 451 on a machine route is reported as `source_access_denied`.
Use `access: {executor: browser_download, mode: browser_download_only}` when you
have confirmed that the source requires a browser. Include a caveat explaining
how to download the file.

## Private and local entries: overlays

Entries that must not ship publicly — project-specific series, local artifact files,
private mirrors — go in an overlay, never in this directory. Point `FISCUS_EXTRA_CATALOG`
at a YAML file or directory outside the repo (or in a gitignored `local-catalog/`).
Overlay files use the same schema; entries merge over bundled ones by `id` (mappings
merge recursively, lists replace). The same verification rule applies.

To register a file already on disk (a project's frozen output, a browser-downloaded
workbook), use the `local_file` executor with `endpoint.local_path` instead of a URL:

```yaml
access:
  executor: local_file
endpoint:
  local_path: ~/path/to/frozen_output.csv
response:
  format: csv          # csv/tsv are previewed; binary is served as-is
```

Local files are read in place with a content hash and modification-time vintage —
never copied into the cache. Prefer `~`-relative paths so overlay entries survive a
move between machines with the same home layout.
