from __future__ import annotations

import csv
import io
import os
from datetime import UTC, date, datetime
from typing import Any

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
ALFRED_PAGE_LIMIT = 100_000


class FredExecutor:
    def __init__(self, requester: HttpRequester) -> None:
        self.requester = requester

    def fetch(
        self,
        entry: CatalogEntry,
        *,
        start: str | None = None,
        end: str | None = None,
        vintage_as_of: str | None = None,
    ) -> FetchResult:
        retrieved_at = datetime.now(UTC).isoformat()
        auth = entry.access.auth
        api_key = os.environ.get(auth.env_var) if auth.env_var else None
        if auth.mode == AuthMode.REQUIRED and not api_key:
            raise MissingKeyError(f"{entry.id} requires environment variable {auth.env_var}")
        if vintage_as_of:
            if not api_key:
                raise MissingKeyError(
                    f"{entry.id} requires environment variable FRED_API_KEY for vintage_as_of"
                )
            if not entry.endpoint.api_url:
                raise SourceRequestError(
                    f"{entry.id} has no keyed FRED endpoint for vintage_as_of"
                )
            return self._fetch_alfred(
                entry,
                api_key,
                start,
                end,
                vintage_as_of,
                retrieved_at,
            )
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
        response_metadata = {
            name: payload[name]
            for name in (
                "realtime_start",
                "realtime_end",
                "observation_start",
                "observation_end",
                "units",
                "output_type",
                "count",
                "offset",
                "limit",
            )
            if payload.get(name) not in {None, ""}
        }
        return FetchResult(
            rows=rows,
            source_url=redact_url(str(response.request.url)),
            retrieved_at_utc=retrieved_at,
            # FRED defaults realtime_start/realtime_end to today. Those fields
            # describe the current information set, not an immutable intraday
            # source revision, so they remain metadata rather than cache identity.
            vintage=source_vintage_from_headers(response.headers, retrieved_at),
            source_metadata={
                "mode": "fred_api_json",
                "response": response_metadata,
                "http": selected_http_metadata(response.headers),
            },
        )

    def _fetch_alfred(
        self,
        entry: CatalogEntry,
        api_key: str,
        start: str | None,
        end: str | None,
        vintage_as_of: str,
        retrieved_at: str,
    ) -> FetchResult:
        request_params = {
            "series_id": str(entry.endpoint.series_id),
            "file_type": "json",
            "units": "lin",
            "realtime_start": vintage_as_of,
            "realtime_end": vintage_as_of,
            "output_type": "1",
            "sort_order": "asc",
            "limit": str(ALFRED_PAGE_LIMIT),
        }
        if start:
            request_params["observation_start"] = start
        if end:
            request_params["observation_end"] = end

        rows: list[dict[str, str]] = []
        offsets_fetched: list[int] = []
        http_pages: list[dict[str, Any]] = []
        response_metadata: dict[str, Any] | None = None
        total_count: int | None = None
        expected_offset = 0
        first_source_url: str | None = None
        previous_date: date | None = None

        while True:
            params = {**request_params, "api_key": api_key, "offset": str(expected_offset)}
            response = self.requester.get(
                str(entry.endpoint.api_url), params=params, policy=FRED_POLICY
            )
            if first_source_url is None:
                first_source_url = redact_url(str(response.request.url))
            try:
                payload = response.json()
            except ValueError as exc:
                raise SourceRequestError(
                    f"FRED/ALFRED returned invalid JSON for {entry.id}"
                ) from exc
            if not isinstance(payload, dict):
                raise SourceRequestError(
                    f"FRED/ALFRED returned a non-object response for {entry.id}"
                )

            page_count, page_offset, page_limit = _validate_alfred_page_metadata(
                payload,
                entry.id,
                vintage_as_of,
                expected_offset,
                start,
                end,
            )
            if total_count is None:
                total_count = page_count
                response_metadata = _alfred_response_metadata(payload)
            elif page_count != total_count:
                raise SourceRequestError(
                    f"FRED/ALFRED total count changed during pagination for {entry.id}"
                )

            observations = payload.get("observations")
            if not isinstance(observations, list):
                raise SourceRequestError(
                    f"FRED/ALFRED response omitted observations for {entry.id}"
                )
            remaining = page_count - page_offset
            expected_page_rows = min(page_limit, remaining)
            if len(observations) != expected_page_rows:
                raise SourceRequestError(
                    f"FRED/ALFRED returned {len(observations)} rows at offset {page_offset}; "
                    f"expected {expected_page_rows} for {entry.id}"
                )

            offsets_fetched.append(page_offset)
            http_pages.append(
                {
                    "offset": page_offset,
                    **selected_http_metadata(response.headers),
                }
            )
            for observation in observations:
                row, observation_date = _validate_alfred_observation(
                    observation,
                    entry.id,
                    vintage_as_of,
                    start,
                    end,
                )
                if previous_date is not None and observation_date <= previous_date:
                    detail = "duplicate" if observation_date == previous_date else "unsorted"
                    raise SourceRequestError(
                        f"FRED/ALFRED returned {detail} observation date "
                        f"{observation_date.isoformat()} for {entry.id}"
                    )
                rows.append(row)
                previous_date = observation_date

            next_offset = page_offset + len(observations)
            if next_offset >= page_count:
                if next_offset != page_count:
                    raise SourceRequestError(
                        f"FRED/ALFRED pagination exceeded total count for {entry.id}"
                    )
                break
            if next_offset <= page_offset:
                raise SourceRequestError(
                    f"FRED/ALFRED pagination made no progress for {entry.id}"
                )
            expected_offset = next_offset

        if total_count is None or response_metadata is None or len(rows) != total_count:
            raise SourceRequestError(f"FRED/ALFRED pagination was incomplete for {entry.id}")

        return FetchResult(
            rows=rows,
            source_url=first_source_url or str(entry.endpoint.api_url),
            retrieved_at_utc=retrieved_at,
            vintage=f"alfred-as-of:{vintage_as_of}",
            source_metadata={
                "provider": "FRED/ALFRED",
                "mode": "alfred_as_of",
                "requested_observation_bounds": {"start": start, "end": end},
                "requested_vintage_as_of": vintage_as_of,
                "request": {
                    **request_params,
                    "offset": "paginated from 0",
                },
                "response": response_metadata,
                "pagination": {
                    "count": total_count,
                    "page_count": len(offsets_fetched),
                    "offsets_fetched": offsets_fetched,
                },
                "http_pages": http_pages,
                "retrieved_at_utc": retrieved_at,
                "revision_date_status": "not checked; arbitrary closed past dates are valid",
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


def _validate_alfred_page_metadata(
    payload: dict[str, Any],
    dataset_id: str,
    vintage_as_of: str,
    expected_offset: int,
    start: str | None,
    end: str | None,
) -> tuple[int, int, int]:
    if payload.get("realtime_start") != vintage_as_of or payload.get(
        "realtime_end"
    ) != vintage_as_of:
        raise SourceRequestError(
            f"FRED/ALFRED response real-time bounds differ from {vintage_as_of} "
            f"for {dataset_id}"
        )
    if _response_integer(payload, "output_type", dataset_id) != 1:
        raise SourceRequestError(f"FRED/ALFRED response output_type is not 1 for {dataset_id}")
    if payload.get("units") != "lin":
        raise SourceRequestError(f"FRED/ALFRED response units are not lin for {dataset_id}")
    if payload.get("sort_order") != "asc":
        raise SourceRequestError(
            f"FRED/ALFRED response sort_order is not asc for {dataset_id}"
        )
    for name, requested in (("observation_start", start), ("observation_end", end)):
        if requested and payload.get(name) != requested:
            raise SourceRequestError(
                f"FRED/ALFRED response {name} differs from requested bound "
                f"{requested} for {dataset_id}"
            )
    count = _response_integer(payload, "count", dataset_id)
    offset = _response_integer(payload, "offset", dataset_id)
    limit = _response_integer(payload, "limit", dataset_id)
    if count < 0 or offset < 0 or limit < 1:
        raise SourceRequestError(
            f"FRED/ALFRED returned invalid pagination metadata for {dataset_id}"
        )
    if offset != expected_offset:
        raise SourceRequestError(
            f"FRED/ALFRED returned offset {offset}; expected {expected_offset} for {dataset_id}"
        )
    if limit != ALFRED_PAGE_LIMIT:
        raise SourceRequestError(
            f"FRED/ALFRED returned limit {limit}; expected {ALFRED_PAGE_LIMIT} "
            f"for {dataset_id}"
        )
    if offset > count:
        raise SourceRequestError(
            f"FRED/ALFRED returned offset beyond total count for {dataset_id}"
        )
    return count, offset, limit


def _validate_alfred_observation(
    observation: Any,
    dataset_id: str,
    vintage_as_of: str,
    start: str | None,
    end: str | None,
) -> tuple[dict[str, str], date]:
    if not isinstance(observation, dict):
        raise SourceRequestError(f"FRED/ALFRED returned a malformed row for {dataset_id}")
    if (
        observation.get("realtime_start") != vintage_as_of
        or observation.get("realtime_end") != vintage_as_of
    ):
        raise SourceRequestError(
            f"FRED/ALFRED row real-time bounds differ from {vintage_as_of} for {dataset_id}"
        )
    date_text = observation.get("date")
    value = observation.get("value")
    if not isinstance(date_text, str) or not isinstance(value, str):
        raise SourceRequestError(
            f"FRED/ALFRED row omitted a string date or value for {dataset_id}"
        )
    try:
        observation_date = date.fromisoformat(date_text)
    except ValueError as exc:
        raise SourceRequestError(
            f"FRED/ALFRED row has invalid date {date_text!r} for {dataset_id}"
        ) from exc
    if start and observation_date < date.fromisoformat(start):
        raise SourceRequestError(
            f"FRED/ALFRED returned {date_text} before requested observation start "
            f"for {dataset_id}"
        )
    if end and observation_date > date.fromisoformat(end):
        raise SourceRequestError(
            f"FRED/ALFRED returned {date_text} after requested observation end "
            f"for {dataset_id}"
        )
    return (
        {
            "date": date_text,
            "value": value,
            "realtime_start": vintage_as_of,
            "realtime_end": vintage_as_of,
        },
        observation_date,
    )


def _response_integer(payload: dict[str, Any], name: str, dataset_id: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool):
        raise SourceRequestError(
            f"FRED/ALFRED response {name} is not an integer for {dataset_id}"
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SourceRequestError(
            f"FRED/ALFRED response {name} is not an integer for {dataset_id}"
        ) from exc
    if isinstance(value, float) and not value.is_integer():
        raise SourceRequestError(
            f"FRED/ALFRED response {name} is not an integer for {dataset_id}"
        )
    if isinstance(value, str) and str(parsed) != value:
        raise SourceRequestError(
            f"FRED/ALFRED response {name} is not a canonical integer for {dataset_id}"
        )
    return parsed


def _alfred_response_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        name: payload[name]
        for name in (
            "realtime_start",
            "realtime_end",
            "observation_start",
            "observation_end",
            "units",
            "output_type",
            "sort_order",
            "order_by",
            "count",
            "offset",
            "limit",
        )
        if payload.get(name) not in {None, ""}
    }
