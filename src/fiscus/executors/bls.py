from __future__ import annotations

import os
from datetime import UTC, datetime

from fiscus.errors import MissingKeyError, SourceRequestError
from fiscus.executors.base import FetchResult
from fiscus.http import (
    HttpRequester,
    RetryPolicy,
    redact_url,
    selected_http_metadata,
    source_vintage_from_headers,
)
from fiscus.models import AuthMode, CatalogEntry
from fiscus.periods import month_end_iso

BLS_POLICY = RetryPolicy(attempts=3, backoff_seconds=1.0, politeness_delay_seconds=0.25)

# BLS period codes: M01-M12 monthly (M13 = annual average), Q01-Q04 quarterly
# (Q05 = annual average), A01 annual, S01-S03 semiannual.
_PERIOD_MONTH = {f"M{month:02d}": month for month in range(1, 13)}
_PERIOD_FIXED_END = {
    "Q01": "03-31",
    "Q02": "06-30",
    "Q03": "09-30",
    "Q04": "12-31",
    "S01": "06-30",
    "S02": "12-31",
}


class BlsExecutor:
    """Single-series fetch from the BLS Public Data API v2.

    The v2 envelope reports year and period codes rather than dates; this
    executor synthesizes period-end ISO dates and preserves the raw codes.
    Annual-average periods (M13, Q05) are excluded from the date axis but
    kept as rows with an empty date so they cannot be silently averaged in.
    """

    def __init__(self, requester: HttpRequester) -> None:
        self.requester = requester

    def fetch(
        self,
        entry: CatalogEntry,
        *,
        start: str | None = None,
        end: str | None = None,
    ) -> FetchResult:
        retrieved_at = datetime.now(UTC).isoformat()
        auth = entry.access.auth
        api_key = os.environ.get(auth.env_var) if auth.env_var else None
        if auth.mode == AuthMode.REQUIRED and not api_key:
            raise MissingKeyError(f"{entry.id} requires environment variable {auth.env_var}")

        url = f"{entry.endpoint.url}{entry.endpoint.series_id}"
        windows = _request_windows(entry, start, end, keyed=bool(api_key))
        observations = []
        responses = []
        for first_year, last_year in windows:
            params: dict[str, str] = dict(entry.endpoint.default_params)
            if first_year is not None and last_year is not None:
                params["startyear"] = str(first_year)
                params["endyear"] = str(last_year)
            if api_key:
                params["registrationkey"] = api_key
            response = self.requester.get(url, params=params, policy=BLS_POLICY)
            responses.append(response)
            try:
                payload = response.json()
            except ValueError:
                raise SourceRequestError(f"BLS returned invalid JSON for {entry.id}") from None
            status = payload.get("status")
            if status != "REQUEST_SUCCEEDED":
                messages = "; ".join(str(item) for item in payload.get("message", []))
                raise SourceRequestError(
                    f"BLS request for {entry.id} failed: {status}: {messages}"
                )
            series_list = payload.get("Results", {}).get("series", [])
            if not series_list or not isinstance(series_list, list):
                raise SourceRequestError(f"BLS response omitted series data for {entry.id}")
            page = series_list[0].get("data", [])
            if not isinstance(page, list):
                raise SourceRequestError(
                    f"BLS response contained malformed series data for {entry.id}"
                )
            observations.extend(page)

        rows = []
        for item in observations:
            if not isinstance(item, dict):
                continue
            year = str(item.get("year", ""))
            period = str(item.get("period", ""))
            rows.append(
                {
                    "date": _period_end_date(year, period),
                    "value": str(item.get("value", "")),
                    "year": year,
                    "period": period,
                    "period_name": str(item.get("periodName", "")),
                }
            )
        rows.sort(key=lambda row: (row["year"], row["period"]))
        if start or end:
            rows = [
                row
                for row in rows
                if row["date"]
                and (not start or row["date"] >= start)
                and (not end or row["date"] <= end)
            ]

        first_response = responses[0]
        last_response = responses[-1]
        return FetchResult(
            rows=rows,
            source_url=redact_url(str(first_response.request.url)),
            retrieved_at_utc=retrieved_at,
            vintage=source_vintage_from_headers(last_response.headers, retrieved_at),
            source_metadata={
                "mode": "bls_api_v2",
                "keyed": bool(api_key),
                "year_windows": [
                    {"start": first, "end": last}
                    for first, last in windows
                    if first is not None and last is not None
                ],
                "http": selected_http_metadata(last_response.headers),
            },
        )


def _period_end_date(year: str, period: str) -> str:
    if period == "A01" and len(year) == 4:
        return f"{year}-12-31"
    if period in _PERIOD_MONTH and len(year) == 4 and year.isdigit():
        return month_end_iso(int(year), _PERIOD_MONTH[period])
    suffix = _PERIOD_FIXED_END.get(period)
    if suffix and len(year) == 4:
        return f"{year}-{suffix}"
    return ""


def _request_windows(
    entry: CatalogEntry,
    start: str | None,
    end: str | None,
    *,
    keyed: bool,
) -> list[tuple[int | None, int | None]]:
    if not start and not end:
        return [(None, None)]
    last_year = int(end[:4]) if end else datetime.now(UTC).year
    first_year = int(start[:4]) if start else min(entry.history.start.year, last_year)
    max_years = 20 if keyed else 10
    windows = []
    cursor = first_year
    while cursor <= last_year:
        window_end = min(cursor + max_years - 1, last_year)
        windows.append((cursor, window_end))
        cursor = window_end + 1
    return windows
