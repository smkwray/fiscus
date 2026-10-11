from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Literal

from fiscus.errors import (
    InvalidCredentialError,
    MissingKeyError,
    SourceQueryRejectedError,
    SourceRequestError,
    SourceSchemaChangedError,
)
from fiscus.executors.base import FetchResult
from fiscus.http import (
    SECRET_QUERY_PARAMS,
    HttpRequester,
    RetryPolicy,
    redact_url,
    selected_http_metadata,
    source_vintage_from_headers,
)
from fiscus.models import AuthMode, CatalogEntry
from fiscus.periods import month_end_iso

JSON_API_POLICY = RetryPolicy(attempts=3, backoff_seconds=0.75, politeness_delay_seconds=0.2)


@dataclass(frozen=True)
class JsonApiRequestSpec:
    """Bounded internal description of one JSON API request surface."""

    request_id: str
    url: str
    default_params: dict[str, str] = field(default_factory=dict)
    allowed_params: frozenset[str] = frozenset()
    auth_mode: AuthMode = AuthMode.NONE
    auth_env_var: str | None = None
    api_key_param: str | None = None
    data_path: str = "."
    date_fields: tuple[str, ...] = ()
    value_fields: tuple[str, ...] = ()
    date_normalizer: Literal["bea_period"] | None = None
    response_adapter: Literal["bea_api"] | None = None

    @classmethod
    def from_catalog_entry(cls, entry: CatalogEntry) -> JsonApiRequestSpec:
        return cls(
            request_id=entry.id,
            url=str(entry.endpoint.url),
            default_params=dict(entry.endpoint.default_params),
            allowed_params=frozenset(
                [
                    *entry.endpoint.allowed_params,
                    *(
                        [entry.endpoint.start_param]
                        if entry.endpoint.start_param
                        else []
                    ),
                    *([entry.endpoint.end_param] if entry.endpoint.end_param else []),
                ]
            ),
            auth_mode=entry.access.auth.mode,
            auth_env_var=entry.access.auth.env_var,
            api_key_param=entry.endpoint.api_key_param,
            data_path=entry.response.data_path or ".",
            date_fields=tuple(entry.response.date_fields),
            value_fields=tuple(entry.response.value_fields),
            date_normalizer=entry.response.date_normalizer,
        )


class JsonApiExecutor:
    """Generic executor for catalog entries and bounded internal JSON request specs."""

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
        spec = JsonApiRequestSpec.from_catalog_entry(entry)
        request_params = dict(params or {})
        if start and entry.endpoint.start_param:
            request_params[entry.endpoint.start_param] = start
        if end and entry.endpoint.end_param:
            request_params[entry.endpoint.end_param] = end
        return self.fetch_request(
            spec,
            params=request_params,
            start=start,
            end=end,
        )

    def fetch_request(
        self,
        spec: JsonApiRequestSpec,
        *,
        params: dict[str, str] | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> FetchResult:
        retrieved_at = datetime.now(UTC).isoformat()
        request_params: dict[str, str] = dict(spec.default_params)
        for key, value in (params or {}).items():
            if key not in spec.allowed_params:
                raise SourceRequestError(
                    f"{spec.request_id} does not accept parameter {key!r}; "
                    f"allowed: {sorted(spec.allowed_params)}"
                )
            request_params[str(key)] = str(value)

        api_key = os.environ.get(spec.auth_env_var) if spec.auth_env_var else None
        if spec.auth_mode == AuthMode.REQUIRED and not api_key:
            raise MissingKeyError(
                f"{spec.auth_env_var} is unset; {spec.request_id} requires this "
                "environment variable"
            )
        if api_key and spec.api_key_param:
            request_params[spec.api_key_param] = api_key

        response = self.requester.get(
            spec.url,
            params=request_params,
            policy=JSON_API_POLICY,
            secret_params=((spec.api_key_param,) if api_key and spec.api_key_param else ()),
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceRequestError(f"{spec.request_id} returned invalid JSON") from exc

        error = resolve_error_envelope(payload, spec.data_path)
        if error is not None:
            raise_embedded_api_error(spec, error, api_key)

        items = resolve_data_path(payload, spec.data_path)
        if not isinstance(items, list):
            if spec.response_adapter == "bea_api":
                raise SourceSchemaChangedError(
                    f"{spec.request_id} expected a list at payload path {spec.data_path!r}"
                )
            raise SourceRequestError(
                f"{spec.request_id} payload path {spec.data_path!r} is not a list"
            )

        secrets = tuple(secret for secret in (api_key,) if secret)
        rows = [normalize_item(item) for item in items]
        rows = [_redact_strings(row, secrets) for row in rows if row]
        if spec.date_normalizer == "bea_period":
            emitted, source_field = spec.date_fields[0], spec.date_fields[1]
            for row in rows:
                row[emitted] = bea_period_end(row.get(source_field, ""))
            rows.sort(key=lambda row: (row.get(emitted, ""), row.get("LineNumber", "")))

        if spec.date_fields and (start or end):
            field = spec.date_fields[0]
            lower = date.fromisoformat(start) if start else None
            upper = date.fromisoformat(end) if end else None
            rows = [
                row
                for row in rows
                if (observed := _source_calendar_date(row.get(field, ""))) is not None
                and (lower is None or observed >= lower)
                and (upper is None or observed <= upper)
            ]

        source_metadata: dict[str, Any] = {
            "mode": "json_api",
            "http": selected_http_metadata(response.headers),
        }
        if spec.response_adapter == "bea_api":
            source_metadata["bea_api"] = safe_bea_results_metadata(payload, secrets)

        return FetchResult(
            rows=rows,
            source_url=redact_url(
                str(response.request.url),
                SECRET_QUERY_PARAMS | frozenset(
                    (spec.api_key_param.casefold(),) if spec.api_key_param else ()
                ),
            ),
            retrieved_at_utc=retrieved_at,
            vintage=source_vintage_from_headers(response.headers, retrieved_at),
            source_metadata=_redact_strings(source_metadata, secrets),
        )


def resolve_error_envelope(payload: Any, data_path: str) -> dict[str, Any] | None:
    """Find an API error beside the configured data path or one of its parents."""
    parts = [segment for segment in data_path.split(".") if segment and segment != "."]
    for depth in range(len(parts) - 1, 0, -1):
        parent = resolve_data_path(payload, ".".join(parts[:depth]))
        if isinstance(parent, dict) and isinstance(parent.get("Error"), dict):
            return parent["Error"]
    return None


def raise_embedded_api_error(
    spec: JsonApiRequestSpec,
    error: dict[str, Any],
    api_key: str | None,
) -> None:
    code_value = error.get("APIErrorCode")
    code = str(code_value).strip() if code_value is not None else "unknown"
    detail_block = error.get("ErrorDetail")
    specific = detail_block.get("Description") if isinstance(detail_block, dict) else None
    general = error.get("APIErrorDescription")
    description = next(
        (
            str(value).strip()
            for value in (specific, general)
            if value is not None and str(value).strip()
        ),
        "Source API returned an error without a description.",
    )
    if api_key:
        description = description.replace(api_key, "REDACTED")

    credential_param = spec.api_key_param or ""
    normalized = description.casefold()
    credential_named = credential_param.casefold() in normalized if credential_param else False
    credential_rejected = credential_named and any(
        marker in normalized
        for marker in ("not active", "not registered", "invalid", "unauthorized", "rejected")
    )
    if credential_rejected:
        env_var = spec.auth_env_var or credential_param
        raise InvalidCredentialError(
            f"{env_var} was rejected by the source for {spec.request_id} "
            f"(APIErrorCode {code}): {description}"
        )
    raise SourceQueryRejectedError(
        f"{spec.request_id} source API rejected the query "
        f"(APIErrorCode {code}): {description}"
    )


def safe_bea_results_metadata(payload: Any, secrets: tuple[str, ...] = ()) -> dict[str, Any]:
    """Allowlist safe BEA result metadata while structurally omitting BEAAPI.Request."""
    results = resolve_data_path(payload, "BEAAPI.Results")
    if not isinstance(results, dict):
        return {}
    allowed = {
        key: results[key]
        for key in ("Statistic", "UTCProductionTime", "Dimensions", "Notes")
        if key in results
    }
    redacted = _redact_strings(allowed, secrets)
    return redacted if isinstance(redacted, dict) else {}


def _redact_strings(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        return {
            _redact_strings(str(key), secrets): _redact_strings(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_strings(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_strings(item, secrets) for item in value)
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "REDACTED")
    return value


def bea_period_end(period: str) -> str:
    """BEA TimePeriod (2025Q1, 2025M03, 2025) -> period-end ISO date."""
    token = period.strip().upper()
    quarter_end = {"1": "03-31", "2": "06-30", "3": "09-30", "4": "12-31"}
    if (
        len(token) == 6
        and token[:4].isdigit()
        and token[4] == "Q"
        and token[5] in quarter_end
    ):
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


def _source_calendar_date(value: str) -> date | None:
    """Compare calendar-date bounds while retaining the source's ISO timestamp."""
    try:
        return date.fromisoformat(value)
    except ValueError:
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            return None


def resolve_data_path(payload: Any, path: str) -> Any:
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
