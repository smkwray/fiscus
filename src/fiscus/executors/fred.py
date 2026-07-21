from __future__ import annotations

import csv
import io
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

FRED_POLICY = RetryPolicy(attempts=3, backoff_seconds=0.5, politeness_delay_seconds=0.1)

FRED_SERIES_INFO_URL = "https://api.stlouisfed.org/fred/series"
FRED_SEARCH_URL = "https://api.stlouisfed.org/fred/series/search"


class FredExecutor:
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
        if api_key and entry.endpoint.api_url:
            return self._fetch_api(entry, api_key, start, end, retrieved_at)
        return self._fetch_graph_csv(entry, start, end, retrieved_at)

    def series_info(self, series_id: str, api_key: str) -> dict[str, str] | None:
        """Fetch series metadata from the keyed FRED API; None on any failure."""
        try:
            response = self.requester.get(
                FRED_SERIES_INFO_URL,
                params={"series_id": series_id, "api_key": api_key, "file_type": "json"},
                policy=FRED_POLICY,
            )
            payload = response.json()
        except (SourceRequestError, ValueError):
            return None
        seriess = payload.get("seriess")
        if not isinstance(seriess, list) or not seriess or not isinstance(seriess[0], dict):
            return None
        return {str(key): str(value) for key, value in seriess[0].items()}

    def search(self, query: str, api_key: str, limit: int) -> list[dict[str, str]]:
        """Keyed FRED full-text series search; empty list on failure."""
        try:
            response = self.requester.get(
                FRED_SEARCH_URL,
                params={
                    "search_text": query,
                    "api_key": api_key,
                    "file_type": "json",
                    "limit": str(limit),
                    "order_by": "popularity",
                    "sort_order": "desc",
                },
                policy=FRED_POLICY,
            )
            payload = response.json()
        except (SourceRequestError, ValueError):
            return []
        seriess = payload.get("seriess")
        if not isinstance(seriess, list):
            return []
        return [
            {str(key): str(value) for key, value in item.items()}
            for item in seriess
            if isinstance(item, dict)
        ]

    def _fetch_api(
        self,
        entry: CatalogEntry,
        api_key: str,
        start: str | None,
        end: str | None,
        retrieved_at: str,
    ) -> FetchResult:
        params = {
            "series_id": entry.endpoint.series_id,
            "api_key": api_key,
            "file_type": "json",
            "units": "lin",
        }
        if start:
            params["observation_start"] = start
        if end:
            params["observation_end"] = end
        response = self.requester.get(
            str(entry.endpoint.api_url), params=params, policy=FRED_POLICY
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceRequestError(f"FRED returned invalid JSON for {entry.id}") from exc
        observations = payload.get("observations")
        if not isinstance(observations, list):
            raise SourceRequestError(f"FRED response omitted observations for {entry.id}")
        rows = []
        realtime_dates: list[str] = []
        for observation in observations:
            if not isinstance(observation, dict):
                continue
            rows.append(
                {
                    "date": observation.get("date", ""),
                    "value": observation.get("value", ""),
                    "realtime_start": observation.get("realtime_start", ""),
                    "realtime_end": observation.get("realtime_end", ""),
                }
            )
            if observation.get("realtime_end"):
                realtime_dates.append(str(observation["realtime_end"]))
        response_realtime_end = payload.get("realtime_end")
        if response_realtime_end:
            vintage = f"fred-realtime:{response_realtime_end}"
        elif realtime_dates:
            vintage = f"fred-realtime:{max(realtime_dates)}"
        else:
            vintage = source_vintage_from_headers(response.headers, retrieved_at)
        return FetchResult(
            rows=rows,
            source_url=redact_url(str(response.request.url)),
            retrieved_at_utc=retrieved_at,
            vintage=vintage,
            source_metadata={
                "mode": "fred_api_json",
                "http": selected_http_metadata(response.headers),
            },
        )

    def _fetch_graph_csv(
        self,
        entry: CatalogEntry,
        start: str | None,
        end: str | None,
        retrieved_at: str,
    ) -> FetchResult:
        params: dict[str, str] = {"id": str(entry.endpoint.series_id)}
        if start:
            params["cosd"] = start
        if end:
            params["coed"] = end
        response = self.requester.get(str(entry.endpoint.url), params=params, policy=FRED_POLICY)
        text = response.content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise SourceRequestError(f"FRED returned an empty CSV for {entry.id}")
        date_field = next(
            (
                field
                for field in reader.fieldnames
                if field.casefold() in {"date", "observation_date"}
            ),
            reader.fieldnames[0],
        )
        value_field = next(
            (field for field in reader.fieldnames if field != date_field),
            None,
        )
        if value_field is None:
            raise SourceRequestError(f"FRED CSV omitted a value column for {entry.id}")
        rows = [
            {"date": row.get(date_field, ""), "value": row.get(value_field, "")} for row in reader
        ]
        return FetchResult(
            rows=rows,
            source_url=redact_url(str(response.request.url)),
            retrieved_at_utc=retrieved_at,
            vintage=source_vintage_from_headers(response.headers, retrieved_at),
            source_metadata={
                "mode": "fred_graph_csv",
                "http": selected_http_metadata(response.headers),
            },
        )
