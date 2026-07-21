from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from fiscus.errors import SourceRequestError

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3
    backoff_seconds: float = 0.5
    politeness_delay_seconds: float = 0.1


DEFAULT_RETRY_POLICY = RetryPolicy()


class HttpRequester:
    def __init__(
        self,
        client: httpx.Client,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.sleep = sleep

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    ) -> httpx.Response:
        return self._request("GET", url, params=params, policy=policy)

    def head(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    ) -> httpx.Response:
        return self._request("HEAD", url, params=params, policy=policy)

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    ) -> httpx.Response:
        safe_target = redact_url(str(self.client.build_request(method, url, params=params).url))
        last_error: Exception | None = None
        for attempt in range(1, policy.attempts + 1):
            if policy.politeness_delay_seconds > 0:
                self.sleep(policy.politeness_delay_seconds)
            try:
                response = self.client.request(method, url, params=params)
            except httpx.RequestError as exc:
                last_error = SourceRequestError(
                    f"{type(exc).__name__} while requesting {safe_target}"
                )
                if attempt == policy.attempts:
                    break
                self.sleep(policy.backoff_seconds * (2 ** (attempt - 1)))
                continue

            if response.status_code not in RETRYABLE_STATUS:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    safe_url = redact_url(str(exc.request.url))
                    detail = _response_detail(response)
                    raise SourceRequestError(
                        f"source returned HTTP {response.status_code} for {safe_url}{detail}"
                    ) from exc
                return response

            last_error = SourceRequestError(
                f"source returned retryable HTTP {response.status_code}"
            )
            if attempt < policy.attempts:
                self.sleep(_retry_delay(response, policy, attempt))

        safe_url = safe_target
        reason = str(last_error) if last_error else "request failed"
        raise SourceRequestError(
            f"source request failed after {policy.attempts} attempts for {safe_url}: {reason}"
        ) from last_error


SECRET_QUERY_PARAMS = frozenset({"api_key", "apikey", "userid", "registrationkey", "key", "token"})


def redact_url(url: str, secret_params: frozenset[str] = SECRET_QUERY_PARAMS) -> str:
    parts = urlsplit(url)
    query = [
        (key, "REDACTED" if key.casefold() in secret_params else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def source_vintage_from_headers(
    headers: Mapping[str, str],
    retrieved_at_utc: str,
) -> str:
    last_modified = headers.get("last-modified")
    etag = headers.get("etag")
    etag_token = _etag_token(etag) if etag else None
    if last_modified:
        try:
            parsed = parsedate_to_datetime(last_modified)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            vintage = f"http-last-modified:{parsed.astimezone(UTC).isoformat()}"
            return f"{vintage}|etag:{etag_token}" if etag_token else vintage
        except (TypeError, ValueError, OverflowError):
            pass
    if etag_token:
        return f"http-etag:{etag_token}"
    return f"retrieval:{retrieved_at_utc}"


def selected_http_metadata(headers: Mapping[str, str]) -> dict[str, str]:
    selected = {}
    for name in ("etag", "last-modified", "content-type", "content-length"):
        value = headers.get(name)
        if value:
            selected[name] = value
    return selected


def _retry_delay(response: httpx.Response, policy: RetryPolicy, attempt: int) -> float:
    retry_after = response.headers.get("retry-after")
    if retry_after:
        try:
            return max(float(retry_after), 0.0)
        except ValueError:
            pass
    return policy.backoff_seconds * (2 ** (attempt - 1))


def _response_detail(response: httpx.Response) -> str:
    content_type = response.headers.get("content-type", "")
    if "json" not in content_type and "text" not in content_type:
        return ""
    text = response.text.strip().replace("\n", " ")
    return f": {text[:240]}" if text else ""


def _etag_token(etag: str) -> str:
    return hashlib.sha256(etag.encode("utf-8")).hexdigest()[:20]
