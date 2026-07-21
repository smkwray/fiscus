from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

from fiscus.errors import DatasetNotFoundError, SourceRequestError
from fiscus.http import HttpRequester, RetryPolicy, redact_url, selected_http_metadata

Z1_POLICY = RetryPolicy(attempts=3, backoff_seconds=1.0, politeness_delay_seconds=0.25)

Z1_PACKAGE_URL = "https://www.federalreserve.gov/releases/z1/current/z1_csv_files.zip"

QUARTER_DATE = re.compile(r"^(?P<year>\d{4}):Q(?P<quarter>[1-4])$")
QUARTER_END = {"1": "03-31", "2": "06-30", "3": "09-30", "4": "12-31"}


@dataclass(frozen=True)
class Z1SeriesMeta:
    code: str
    description: str
    table: str
    units: str
    csv_member: str


@dataclass(frozen=True)
class Z1Loaded:
    rows: list[dict[str, str]]
    meta: Z1SeriesMeta
    vintage: str
    release_date: str
    package_source_url: str
    package_retrieved_at_utc: str
    package_metadata: dict[str, str]


class Z1Executor:
    """Serve individual Z.1 series out of the official quarterly CSV package.

    One package download per release vintage serves every series; the package
    and its provenance sidecar live under the fiscus data root.
    """

    def __init__(self, requester: HttpRequester, package_dir: Path) -> None:
        self.requester = requester
        self.package_dir = package_dir
        self._index_cache: dict[Path, dict[str, Z1SeriesMeta]] = {}

    def load_series(self, code: str, *, refresh: bool = False) -> Z1Loaded:
        zip_path, manifest = self._ensure_package(refresh=refresh)
        index = self._series_index(zip_path)
        meta = index.get(code)
        if meta is None:
            raise DatasetNotFoundError(
                f"Z.1 series {code} is not present in release {manifest['release_date']}; "
                "check the mnemonic (including the .Q or .A suffix)"
            )
        rows = self._read_series(zip_path, meta)
        return Z1Loaded(
            rows=rows,
            meta=meta,
            vintage=manifest["vintage"],
            release_date=manifest["release_date"],
            package_source_url=manifest["source_url"],
            package_retrieved_at_utc=manifest["retrieved_at_utc"],
            package_metadata=dict(manifest.get("http", {})),
        )

    def _ensure_package(self, *, refresh: bool) -> tuple[Path, dict]:
        release_date, _ = self._current_release_date()
        vintage = f"z1-release:{release_date}"
        zip_path = self.package_dir / f"z1-release-{release_date}.zip"
        manifest_path = zip_path.with_suffix(".manifest.json")
        if not refresh and zip_path.is_file() and manifest_path.is_file():
            return zip_path, json.loads(manifest_path.read_text(encoding="utf-8"))

        retrieved_at = datetime.now(UTC).isoformat()
        response = self.requester.get(Z1_PACKAGE_URL, policy=Z1_POLICY)
        content = response.content
        try:
            zipfile.ZipFile(io.BytesIO(content)).testzip()
        except zipfile.BadZipFile as exc:
            raise SourceRequestError("Z.1 package download is not a valid zip archive") from exc
        self.package_dir.mkdir(parents=True, exist_ok=True)
        temporary = zip_path.with_name(f".{zip_path.name}.{os.getpid()}.tmp")
        temporary.write_bytes(content)
        os.replace(temporary, zip_path)
        manifest = {
            "source_url": redact_url(str(response.request.url)),
            "retrieved_at_utc": retrieved_at,
            "sha256": hashlib.sha256(content).hexdigest(),
            "byte_count": len(content),
            "vintage": vintage,
            "release_date": release_date,
            "http": selected_http_metadata(response.headers),
        }
        manifest_temporary = manifest_path.with_name(f".{manifest_path.name}.{os.getpid()}.tmp")
        manifest_temporary.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(manifest_temporary, manifest_path)
        self._index_cache.pop(zip_path, None)
        return zip_path, manifest

    def _current_release_date(self) -> tuple[str, dict[str, str]]:
        response = self.requester.head(Z1_PACKAGE_URL, policy=Z1_POLICY)
        metadata = selected_http_metadata(response.headers)
        last_modified = response.headers.get("last-modified")
        if last_modified:
            try:
                parsed = parsedate_to_datetime(last_modified)
                return parsed.date().isoformat(), metadata
            except (TypeError, ValueError, OverflowError):
                pass
        # Explicitly labeled fallback: without Last-Modified the release
        # identity degrades to the retrieval day.
        return f"retrieved-{datetime.now(UTC).date().isoformat()}", metadata

    def _series_index(self, zip_path: Path) -> dict[str, Z1SeriesMeta]:
        cached = self._index_cache.get(zip_path)
        if cached is not None:
            return cached
        index: dict[str, Z1SeriesMeta] = {}
        with zipfile.ZipFile(zip_path) as archive:
            members = set(archive.namelist())
            for name in sorted(members):
                if not name.startswith("data_dictionary/") or not name.endswith(".txt"):
                    continue
                csv_member = "csv/" + Path(name).stem + ".csv"
                if csv_member not in members:
                    continue
                text = archive.read(name).decode("utf-8", "replace")
                for line in text.splitlines():
                    fields = line.split("\t")
                    if len(fields) < 5 or not fields[0]:
                        continue
                    code = fields[0].strip()
                    if code not in index:
                        index[code] = Z1SeriesMeta(
                            code=code,
                            description=fields[1].strip(),
                            table=fields[3].strip(),
                            units=fields[4].strip(),
                            csv_member=csv_member,
                        )
        if not index:
            raise SourceRequestError("Z.1 package contains no data-dictionary series index")
        self._index_cache[zip_path] = index
        return index

    def _read_series(self, zip_path: Path, meta: Z1SeriesMeta) -> list[dict[str, str]]:
        with zipfile.ZipFile(zip_path) as archive:
            text = archive.read(meta.csv_member).decode("utf-8-sig", "replace")
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames or meta.code not in reader.fieldnames:
            raise SourceRequestError(
                f"Z.1 table {meta.csv_member} does not contain column {meta.code}"
            )
        date_field = reader.fieldnames[0]
        rows = []
        for row in reader:
            raw_date = (row.get(date_field) or "").strip()
            if not raw_date:
                continue
            rows.append(
                {
                    "date": _normalize_z1_date(raw_date),
                    "value": (row.get(meta.code) or "").strip(),
                }
            )
        return rows


def _normalize_z1_date(raw: str) -> str:
    match = QUARTER_DATE.match(raw)
    if match:
        return f"{match.group('year')}-{QUARTER_END[match.group('quarter')]}"
    if re.fullmatch(r"\d{4}", raw):
        return f"{raw}-12-31"
    return raw
