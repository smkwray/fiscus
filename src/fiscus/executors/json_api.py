from __future__ import annotations

import os
from datetime import UTC, datetime
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
from fiscus.periods import month_end_iso

JSON_API_POLICY = RetryPolicy(attempts=3, backoff_seconds=0.75, politeness_delay_seconds=0.2)


class JsonApiExecutor:
    """Generic executor for catalog-described JSON endpoints.

    The catalog entry supplies everything: URL, default parameters, the
    key parameter name for keyed sources, the dot-path to the payload's row
    list, and optional start/end parameter names. Row items may be objects
    or positional [date, value] pairs.
    """

    def __init__(self, requester: HttpRequester) -> None:
        self.requester = requester

    def fetch(
        self,
        entry: CatalogEntry,
        *,
        start: str | None = None,
        end: str | None = None,
        params: dict[str, str] | None = None,
    ) -> FetchResult:
        retrieved_at = datetime.now(UTC).isoformat()
        request_params: dict[str, str] = dict(entry.endpoint.default_params)
        allowed = set(entry.endpoint.allowed_params)
        for key, value in (params or {}).items():
            if key not in allowed:
                raise SourceRequestError(
                    f"{entry.id} does not accept parameter {key!r}; allowed: {sorted(allowed)}"
                )
            request_params[str(key)] = str(value)
        if start and entry.endpoint.start_param:
            request_params[entry.endpoint.start_param] = start
        if end and entry.endpoint.end_param:
            request_params[entry.endpoint.end_param] = end

        auth = entry.access.auth
        api_key = os.environ.get(auth.env_var) if auth.env_var else None
        if auth.mode == AuthMode.REQUIRED and not api_key:
            raise MissingKeyError(f"{entry.id} requires environment variable {auth.env_var}")
        if api_key and entry.endpoint.api_key_param:
            request_params[entry.endpoint.api_key_param] = api_key

        response = self.requester.get(
            str(entry.endpoint.url),
            params=request_params,
            policy=JSON_API_POLICY,
            secret_params=(
                (entry.endpoint.api_key_param,)
                if api_key and entry.endpoint.api_key_param
                else ()
            ),
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceRequestError(f"{entry.id} returned invalid JSON") from exc

        items = resolve_data_path(payload, entry.response.data_path or "")
        if not isinstance(items, list):
            raise SourceRequestError(
                f"{entry.id} payload path {entry.response.data_path!r} is not a list"
            )
        rows = [normalize_item(item) for item in items]
        rows = [row for row in rows if row]
        if entry.response.date_normalizer == "bea_period":
            emitted, source_field = entry.response.date_fields[0], entry.response.date_fields[1]
            for row in rows:
                row[emitted] = bea_period_end(row.get(source_field, ""))
            rows.sort(key=lambda row: row.get(emitted, ""))

        date_fields = entry.response.date_fields
        if date_fields and (start or end):
            # Client-side filtering when the endpoint has no range parameters;
            # ISO-formatted date strings compare correctly as text.
            field = date_fields[0]
            rows = [
                row
                for row in rows
                if (not start or row.get(field, "") >= start)
                and (not end or row.get(field, "") <= end)
            ]

        return FetchResult(
            rows=rows,
            source_url=redact_url(str(response.request.url)),
            retrieved_at_utc=retrieved_at,
            vintage=source_vintage_from_headers(response.headers, retrieved_at),
            source_metadata={
                "mode": "json_api",
                "http": selected_http_metadata(response.headers),
            },
        )


def bea_period_end(period: str) -> str:
    """BEA TimePeriod (2025Q1, 2025M03, 2025) -> period-end ISO date."""
    token = period.strip().upper()
    quarter_end = {"1": "03-31", "2": "06-30", "3": "09-30", "4": "12-31"}
    if len(token) == 6 and token[4] == "Q" and token[5] in quarter_end:
        return f"{token[:4]}-{quarter_end[token[5]]}"
    if (
        len(token) == 7
        and token[4] == "M"
        and token[:4].isdigit()
        and token[5:].isdigit()
        and 1 <= int(token[5:]) <= 12
    ):
        return month_end_iso(int(token[:4]), int(token[5:]))
    if len(token) == 4 and token.isdigit():
        return f"{token}-12-31"
    return ""


def resolve_data_path(payload: Any, path: str) -> Any:
    # A path of "." addresses the payload root (a bare top-level list).
    current = payload
    for part in [segment for segment in path.split(".") if segment and segment != "."]:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        else:
            return None
    return current


def normalize_item(item: Any) -> dict[str, str]:
    if isinstance(item, dict):
        return {str(key): "" if value is None else str(value) for key, value in item.items()}
    if isinstance(item, list) and len(item) >= 2:
        return {
            "date": "" if item[0] is None else str(item[0]),
            "value": "" if item[1] is None else str(item[1]),
        }
    return {}
