from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fiscus.errors import FiscusError
from fiscus.service import FiscusService


def _issue_reporting_instruction() -> str:
    for parent in Path(__file__).resolve().parents:
        state_path = parent / "do" / "state.md"
        if state_path.is_file():
            return (
                "If Fiscus has a reproducible defect, source drift, semantic hazard, or "
                f"missing capability, record a concise actionable item in {state_path} before "
                "ending the turn and put detailed evidence under its sibling do/history/ "
                "directory. Never put credentials or private data there."
            )
    return (
        "If Fiscus has a reproducible defect, source drift, semantic hazard, or missing "
        "capability, report it explicitly to the owner before ending the turn; when a Fiscus "
        "checkout is available, record it in that checkout's do/state.md with detailed "
        "evidence under do/history/. Never include credentials or private data."
    )


def create_server(
    service: FiscusService | None = None,
    *,
    data_dir: str | Path | None = None,
) -> Any:
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError
    from mcp.types import CallToolResult, TextContent

    class FiscusFastMCP(FastMCP):
        async def call_tool(
            self,
            name: str,
            arguments: dict[str, Any],
        ) -> Any:
            try:
                return await super().call_tool(name, arguments)
            except ToolError as exc:
                cause: BaseException | None = exc
                while cause is not None and not isinstance(cause, FiscusError):
                    cause = cause.__cause__
                if not isinstance(cause, FiscusError):
                    raise
                return CallToolResult(
                    content=[
                        TextContent(
                            type="text",
                            text=f"fiscus [{cause.code}]: {cause}",
                        )
                    ],
                    structuredContent={"code": cause.code, "message": str(cause)},
                    isError=True,
                )

    core = service or FiscusService(data_dir=data_dir)
    owns_service = service is None

    @asynccontextmanager
    async def lifespan(_: Any) -> Any:
        try:
            yield {}
        finally:
            core.close()

    app = FiscusFastMCP(
        "fiscus",
        instructions=(
            "Curated US economic data (Treasury plumbing, Fed, FRED, BEA, BLS, and more) "
            "with per-request caveats and provenance. Use these tools for any US economic "
            "data need instead of writing ad-hoc download code: call find_series first to "
            "get the right dataset id, units, and footguns, then the matching fetch tool. "
            "Cache-backed fetches return an artifact descriptor, never a server filesystem "
            "path; read exact full bytes from fiscus-cache://artifact/<cache_key>. "
            + _issue_reporting_instruction()
        ),
        json_response=True,
        log_level="WARNING",
        lifespan=lifespan if owns_service else None,
    )

    @app.resource(
        "fiscus-cache://artifact/{cache_key}",
        name="cache_artifact",
        description=(
            "Exact immutable bytes for a cache artifact key or unique prefix. Obtain the "
            "key and media type from a fetch response's artifact descriptor."
        ),
        mime_type="application/octet-stream",
    )
    def cache_artifact(cache_key: str) -> bytes:
        """Read exact cache bytes without exposing the server's local database path."""
        return core.read_bytes(cache_key)

    @app.tool()
    def find_series(query: str, family: str | None = None, limit: int = 8) -> dict[str, Any]:
        """Search the curated catalog. Call this before guessing any series or dataset id.

        Curated results include units, history start, access needs, supported fetch tool,
        and caveat count so the next call can select the right concept. When the catalog has
        no match, uncurated candidates from the source's own search are returned under
        uncurated_upstream. BEA inventory candidates intentionally omit reviewed history,
        units, and verified_on. For keyless discovery across the Z.1 package data dictionary,
        pass family="z1".
        """
        return core.find_series(query, family=family, limit=limit)

    @app.tool()
    def get_series(
        dataset_id: str,
        start: str | None = None,
        end: str | None = None,
        vintage_as_of: str | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """Fetch a cataloged series (FRED, BEA, BLS, OFR, NY Fed, TreasuryDirect families).

        Prefer ids from find_series. Uncataloged FRED and BLS series are also served as
        fred.<SERIES_ID> / bls.<SERIES_ID>. Their source unit metadata is labeled when
        available; otherwise annualization is explicitly unknown. These passthroughs carry
        interpretation warnings instead of curated review. Returns a compact preview,
        binding caveats, provenance, and an artifact descriptor. Read full bytes through
        fiscus-cache://artifact/<cache_key>. Set refresh only for a new source retrieval.
        For a FRED/ALFRED snapshot, set vintage_as_of to a closed past ISO date; start and
        end remain observation-date bounds, and FRED_API_KEY is required. Vintage failures
        distinguish an unset key, a rejected credential, no ALFRED history, a request before
        the first ALFRED vintage, and a source identifier that does not exist. A failed BOGZ1
        identifier is not auto-corrected because its two-letter prefix encodes the concept.
        """
        return core.get_series(
            dataset_id,
            start=start,
            end=end,
            vintage_as_of=vintage_as_of,
            refresh=refresh,
        )

    @app.tool()
    def bea_nipa_table(
        table_id: str,
        frequency: Literal["A", "Q", "M"],
        start_year: int,
        end_year: int,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """Fetch a complete BEA NIPA table for an explicit inclusive year range.

        table_id is BEA's canonical TableName (for example T10106). frequency must be
        A, Q, or M and must be advertised for that table in BEA's source inventory.
        The route is NIPA-only, sends an explicit Year list rather than Year=X, and
        returns source-observed metric labels plus per-line coverage. Uncurated tables
        omit reviewed units, history, and verified_on claims; exact curated pairs add
        a separate catalog overlay. BEA_API_KEY is required for a source retrieval.
        """
        return core.bea_nipa_table(
            table_id,
            frequency,
            start_year,
            end_year,
            refresh=refresh,
        )

    @app.tool()
    def fiscaldata_query(
        dataset_id: str,
        params: dict[str, str] | None = None,
        max_pages: int = 10,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """Query a curated Treasury FiscalData dataset selected from find_series.

        Use official FiscalData query keys such as fields, filter, sort, and page[size].
        Narrow large requests with a filter instead of raising max_pages indiscriminately.
        """
        return core.fiscaldata_query(
            dataset_id,
            params=params,
            max_pages=max_pages,
            refresh=refresh,
        )

    @app.tool()
    def get_file(dataset_id: str, refresh: bool = False) -> dict[str, Any]:
        """Download a curated file artifact selected from find_series.

        Serves sources published as direct files (workbooks, delimited text, archives).
        Parseable csv/tsv files return a row preview; binary formats return provenance and
        an artifact descriptor. Read exact bytes at fiscus-cache://artifact/<cache_key>.
        """
        return core.get_file(dataset_id, refresh=refresh)

    @app.tool()
    def z1_series(
        code: str,
        kind: str,
        start: str | None = None,
        end: str | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """Fetch a Z.1 Financial Accounts series with enforced official prefix semantics.

        kind must match the series prefix. This includes FR revaluations and the other
        official FA/FC/FG/FI/FL/FS/FU/FV/LA/LM/PC types. The call refuses to present one
        kind as another. Accepts bare mnemonics, .Q/.A suffixes, and FRED-style BOGZ1 aliases.
        If an official mnemonic is refused or absent, follow the server issue-reporting
        instruction before bypassing Fiscus.
        """
        return core.z1_series(code, kind, start=start, end=end, refresh=refresh)

    @app.tool()
    def soma_holdings(
        asof: str | None = None,
        cusip: str | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """Fetch CUSIP-level SOMA Treasury holdings for a weekly as-of snapshot date.

        Omit asof for the latest published snapshot; the resolved date becomes part of the
        immutable cache identity. Snapshots begin July 2003 and are weekly, not daily.
        """
        return core.soma_holdings(asof=asof, cusip=cusip, refresh=refresh)

    @app.tool()
    def provenance(cache_key: str) -> dict[str, Any]:
        """Re-emit the provenance manifest for any prior fetch by its cache key.

        The cache key appears in every fetch response's artifact and provenance blocks;
        a unique hex prefix of at least 12 characters is accepted. Resolves only immutable
        artifacts, never a mutable "latest" pointer.
        """
        return core.provenance(cache_key)

    @app.tool()
    def source_caveats(
        dataset_id: str,
        start: str | None = None,
        end: str | None = None,
        params: dict[str, str] | None = None,
        unit_classes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return every catalog caveat and whether it binds to the supplied context.

        Supply a date range to evaluate history seams and unit_classes when checking a planned
        combination of differently scaled, adjusted, or annualized series.
        """
        return core.source_caveats(
            dataset_id,
            start=start,
            end=end,
            params=params,
            unit_classes=unit_classes,
        )

    return app


def main(*, data_dir: str | Path | None = None) -> int:
    create_server(data_dir=data_dir).run(transport="stdio")
    return 0
