from __future__ import annotations

import csv
import hashlib
import io
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fiscus.errors import SourceRequestError
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
        data = path.read_bytes()
        stat = path.stat()
        modified = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
        rows: list[dict[str, str]] = []
        if entry.response.format in {ResponseFormat.CSV, ResponseFormat.TSV}:
            delimiter = "," if entry.response.format == ResponseFormat.CSV else "\t"
            reader = csv.DictReader(
                io.StringIO(data.decode("utf-8-sig", "replace")), delimiter=delimiter
            )
            if not reader.fieldnames:
                raise SourceRequestError(f"{entry.id} local file has no header row")
            rows = [
                {str(key): "" if value is None else str(value) for key, value in row.items()}
                for row in reader
            ]
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
