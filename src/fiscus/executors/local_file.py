from __future__ import annotations

import csv
import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fiscus.csvutil import delimited_bytes_to_rows
from fiscus.errors import SourceRequestError, SourceSchemaChangedError
from fiscus.models import CatalogEntry, ResponseFormat


class LocalFileExecutor:
    """Serve a catalog entry that points at a file already on disk.

    Intended for private overlay entries registering local artifacts
    (sibling-project outputs, browser-downloaded workbooks). The file is
    read in place — never copied into the cache — and the response carries
    a provenance block with the content hash and modification time.
    """

    def read(self, entry: CatalogEntry) -> dict[str, Any]:
        raw_path = entry.endpoint.local_path or ""
        path = Path(raw_path).expanduser()
        if not path.is_file():
            raise SourceRequestError(
                f"{entry.id} points at a local file that does not exist: {path}"
            )
        try:
            with path.open("rb") as source:
                before = os.fstat(source.fileno())
                data = source.read()
                stat = os.fstat(source.fileno())
        except OSError as exc:
            raise SourceRequestError(f"{entry.id} local file could not be read: {path}") from exc
        if (before.st_size, before.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
            raise SourceRequestError(f"{entry.id} local file changed while being read")
        modified = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
        rows: list[dict[str, str]] = []
        if entry.response.format in {ResponseFormat.CSV, ResponseFormat.TSV}:
            delimiter = "," if entry.response.format == ResponseFormat.CSV else "\t"
            try:
                rows = delimited_bytes_to_rows(
                    data,
                    delimiter=delimiter,
                    required_fields=[*entry.response.date_fields, *entry.response.value_fields],
                )
            except (ValueError, csv.Error) as exc:
                raise SourceSchemaChangedError(
                    f"{entry.id} local file is not valid UTF-8 delimited data"
                ) from exc
        return {
            "path": path,
            "rows": rows,
            "sha256": hashlib.sha256(data).hexdigest(),
            "byte_count": len(data),
            "modified_utc": modified.isoformat(),
            # The file's own modification time is the only vintage evidence
            # a local artifact carries.
            "vintage": f"file-mtime:{modified.isoformat()}",
            "retrieved_at_utc": datetime.now(UTC).isoformat(),
        }
