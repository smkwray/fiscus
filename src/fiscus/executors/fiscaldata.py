from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fiscus.errors import SourceRequestError
from fiscus.executors.base import FetchResult
from fiscus.http import (
    HttpRequester,
    RetryPolicy,
    redact_url,
    selected_http_metadata,
    source_vintage_from_headers,
)
from fiscus.models import CatalogEntry

FISCALDATA_POLICY = RetryPolicy(
    attempts=4,
    backoff_seconds=0.75,
    politeness_delay_seconds=0.15,
)


class FiscalDataExecutor:
    def __init__(self, requester: HttpRequester) -> None:
        self.requester = requester

    def fetch(
        self,
        entry: CatalogEntry,
        *,
        params: dict[str, str] | None = None,
        max_pages: int = 10,
    ) -> FetchResult:
        requested = {str(key): str(value) for key, value in (params or {}).items()}
        disallowed = sorted(set(requested).difference(entry.endpoint.allowed_params))
        if disallowed:
            raise SourceRequestError(
                f"unsupported parameters for {entry.id}: {', '.join(disallowed)}"
            )
        hard_max = entry.response.pagination.hard_max_pages
        if max_pages < 1 or max_pages > hard_max:
            raise SourceRequestError(f"max_pages must be between 1 and {hard_max}")

        query = dict(entry.endpoint.default_params)
        query.update(requested)
        try:
            page_size = int(
                query.get("page[size]") or entry.response.pagination.default_page_size or 100
            )
        except (TypeError, ValueError) as exc:
            raise SourceRequestError("page[size] must be an integer") from exc
        if page_size < 1 or page_size > 10_000:
            raise SourceRequestError("page[size] must be between 1 and 10000")
        query["page[size]"] = str(page_size)

        rows: list[dict[str, Any]] = []
        page_urls: list[str] = []
        retrieved_at = datetime.now(UTC).isoformat()
        first_response = None
        total_pages: int | None = None
        next_link: Any = None

        for page_number in range(1, max_pages + 1):
            page_query = dict(query)
            page_query["page[number]"] = str(page_number)
            response = self.requester.get(
                str(entry.endpoint.url),
                params=page_query,
                policy=FISCALDATA_POLICY,
            )
            if first_response is None:
                first_response = response
            page_urls.append(redact_url(str(response.request.url)))
            try:
                payload = response.json()
            except ValueError as exc:
                raise SourceRequestError(
                    f"FiscalData returned invalid JSON for {entry.id}"
                ) from exc
            page_rows = payload.get(entry.response.data_path or "data")
            if not isinstance(page_rows, list):
                raise SourceRequestError(
                    "FiscalData response omitted "
                    f"{entry.response.data_path or 'data'} for {entry.id}"
                )
            rows.extend(row for row in page_rows if isinstance(row, dict))

            meta = payload.get("meta") or {}
            if total_pages is None and isinstance(meta, dict):
                raw_total_pages = meta.get("total-pages")
                try:
                    total_pages = int(raw_total_pages) if raw_total_pages is not None else None
                except (TypeError, ValueError):
                    total_pages = None

            links = payload.get("links") or {}
            next_link = links.get("next") if isinstance(links, dict) else None
            if total_pages is not None and page_number >= total_pages:
                break
            if total_pages is None and not next_link:
                break
        else:
            if total_pages is not None and total_pages > max_pages:
                raise SourceRequestError(
                    f"{entry.id} has {total_pages} pages at page[size]={page_size}; raise "
                    f"max_pages above {max_pages}, increase page[size] (max 10000), or "
                    "narrow the filter"
                )
            if total_pages is None and next_link:
                raise SourceRequestError(
                    f"{entry.id} has more pages than max_pages={max_pages}; "
                    "raise max_pages or narrow the filter"
                )

        if first_response is None:
            raise SourceRequestError(f"FiscalData returned no response for {entry.id}")
        return FetchResult(
            rows=rows,
            source_url=page_urls[0],
            retrieved_at_utc=retrieved_at,
            vintage=source_vintage_from_headers(first_response.headers, retrieved_at),
            source_metadata={
                "mode": "fiscaldata_json",
                "pages_fetched": len(page_urls),
                "page_urls": page_urls,
                "reported_total_pages": total_pages,
                "http": selected_http_metadata(first_response.headers),
            },
        )
