# Contributing

Bug reports and narrowly scoped pull requests are welcome. Please include a reproducible case, the catalog id involved, and the source URL or release date when an endpoint or semantic definition changed.

Before opening a pull request:

```bash
uv sync --group dev
uv run fiscus catalog lint
uv run ruff check .
uv run pytest
```

Catalog changes should include a current `verified_on` date, primary-source evidence, history-boundary checks, and a caveat whenever a reasonable user could choose the wrong unit, concept, or date regime.

This is a solo-maintained utility with no support, response-time, or uptime commitment.
