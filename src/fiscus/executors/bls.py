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

BLS_POLICY = RetryPolicy(attempts=3, backoff_seconds=1.0, politeness_delay_seconds=0.25)

# BLS period codes: M01-M12 monthly (M13 = annual average), Q01-Q04 quarterly
# (Q05 = annual average), A01 annual, S01-S03 semiannual.
_PERIOD_MONTH_END = {
    "M01": "01-31",
    "M02": "02-28",
    "M03": "03-31",
    "M04": "04-30",
    "M05": "05-31",
    "M06": "06-30",
    "M07": "07-31",
    "M08": "08-31",
    "M09": "09-30",
    "M10": "10-31",
    "M11": "11-30",
    "M12": "12-31",
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

        params: dict[str, str] = dict(entry.endpoint.default_params)
        # The API rejects a lone year bound; default the missing side
        # (open-ended start -> that single year, open-ended end -> now).
        if start:
            params["startyear"] = start[:4]
            params["endyear"] = end[:4] if end else str(datetime.now(UTC).year)
        elif end:
            params["startyear"] = end[:4]
            params["endyear"] = end[:4]
        if api_key:
            params["registrationkey"] = api_key

        url = f"{entry.endpoint.url}{entry.endpoint.series_id}"
        response = self.requester.get(url, params=params, policy=BLS_POLICY)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceRequestError(f"BLS returned invalid JSON for {entry.id}") from exc
        status = payload.get("status")
        if status != "REQUEST_SUCCEEDED":
            messages = "; ".join(str(item) for item in payload.get("message", []))
            raise SourceRequestError(f"BLS request for {entry.id} failed: {status}: {messages}")
        series_list = payload.get("Results", {}).get("series", [])
        if not series_list or not isinstance(series_list, list):
            raise SourceRequestError(f"BLS response omitted series data for {entry.id}")
        observations = series_list[0].get("data", [])

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
                if not row["date"]
                or ((not start or row["date"] >= start) and (not end or row["date"] <= end))
            ]

        return FetchResult(
            rows=rows,
            source_url=redact_url(str(response.request.url)),
            retrieved_at_utc=retrieved_at,
            vintage=source_vintage_from_headers(response.headers, retrieved_at),
            source_metadata={
                "mode": "bls_api_v2",
                "keyed": bool(api_key),
                "http": selected_http_metadata(response.headers),
            },
        )


def _period_end_date(year: str, period: str) -> str:
    if period == "A01" and len(year) == 4:
        return f"{year}-12-31"
    suffix = _PERIOD_MONTH_END.get(period)
    if suffix and len(year) == 4:
        return f"{year}-{suffix}"
    return ""
