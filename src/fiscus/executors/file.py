from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from fiscus.errors import SourceRequestError
from fiscus.executors.base import FetchResult
from fiscus.http import (
    HttpRequester,
    RetryPolicy,
    redact_url,
    selected_http_metadata,
    source_vintage_from_headers,
)
from fiscus.models import CatalogEntry, ResponseFormat

FILE_POLICY = RetryPolicy(attempts=3, backoff_seconds=1.0, politeness_delay_seconds=0.3)


class FileExecutor:
    """Download a catalog-described file artifact.

    csv/tsv responses are parsed into canonical rows; binary formats
    (workbooks, archives) are preserved byte-for-byte as cache artifacts with
    provenance and no row preview.
    """

    def __init__(self, requester: HttpRequester) -> None:
        self.requester = requester

    def fetch(self, entry: CatalogEntry) -> tuple[FetchResult, bytes, str]:
        retrieved_at = datetime.now(UTC).isoformat()
        response = self.requester.get(
            str(entry.endpoint.url),
            params=entry.endpoint.default_params or None,
            policy=FILE_POLICY,
        )
        content = response.content
        if not content:
            raise SourceRequestError(f"{entry.id} returned an empty file")

        if entry.response.format in {ResponseFormat.CSV, ResponseFormat.TSV}:
            delimiter = "," if entry.response.format == ResponseFormat.CSV else "\t"
            text = content.decode("utf-8-sig", "replace")
            reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
            if not reader.fieldnames:
                raise SourceRequestError(f"{entry.id} file has no header row")
            rows = [
                {str(key): "" if value is None else str(value) for key, value in row.items()}
                for row in reader
            ]
            data_file = "data.csv"
        else:
            rows = []
            data_file = _source_filename(str(entry.endpoint.url))

        result = FetchResult(
            rows=rows,
            source_url=redact_url(str(response.request.url)),
            retrieved_at_utc=retrieved_at,
            vintage=source_vintage_from_headers(response.headers, retrieved_at),
            source_metadata={
                "mode": "file_download",
                "source_format": entry.response.format.value,
                "http": selected_http_metadata(response.headers),
            },
        )
        return result, content, data_file


def _source_filename(url: str) -> str:
    name = PurePosixPath(urlsplit(url).path).name or "download.bin"
    return name if "." in name else f"{name}.bin"
