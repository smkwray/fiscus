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
from typing import Any

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
    package_metadata: dict[str, Any]


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
            package_metadata={
                "package_sha256": manifest["sha256"],
                "package_byte_count": manifest["byte_count"],
                "release_date": manifest["release_date"],
                "http": dict(manifest["http"]),
            },
        )

    def search_series(self, query: str, limit: int) -> list[dict[str, str | bool | dict[str, str]]]:
        """Search the current package data dictionary without a curated-code registry."""
        # Keep exact mnemonics out of the description search: a near match is a
        # different economic concept, not an answer to the requested mnemonic.
        from fiscus.search import _exact_z1_mnemonic

        exact_code = _exact_z1_mnemonic(query)
        zip_path, manifest = self._ensure_package(refresh=False)
        index = self._series_index(zip_path)
        if exact_code is not None:
            meta = index.get(exact_code)
            return [] if meta is None else [_search_result(meta, manifest, exact=True)]

        query_tokens = _search_tokens(query)
        if not query_tokens:
            return []
        matches: list[tuple[int, Z1SeriesMeta]] = []
        for meta in index.values():
            frequency = "annual" if meta.code.endswith(".A") else "quarterly"
            text = " ".join((meta.code, meta.description, meta.table, meta.units, frequency))
            candidate_tokens = _search_tokens(text)
            if not query_tokens.issubset(candidate_tokens):
                continue
            score = len(query_tokens) * 10
            normalized_query = " ".join(sorted(query_tokens))
            normalized_text = " ".join(sorted(candidate_tokens))
            if normalized_query == normalized_text:
                score += 100
            matches.append((score, meta))
        matches.sort(key=lambda item: (-item[0], item[1].code))
        return [_search_result(meta, manifest, exact=False) for _, meta in matches[:limit]]

    def _ensure_package(self, *, refresh: bool) -> tuple[Path, dict]:
        release_date, _ = self._current_release_date()
        zip_path = self.package_dir / f"z1-release-{release_date}.zip"
        manifest_path = zip_path.with_suffix(".manifest.json")
        if not refresh and zip_path.is_file() and manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                _validate_package(zip_path, manifest, release_date)
            except (OSError, json.JSONDecodeError, SourceRequestError):
                pass
            else:
                return zip_path, manifest

        retrieved_at = datetime.now(UTC).isoformat()
        response = self.requester.get(Z1_PACKAGE_URL, policy=Z1_POLICY)
        content = response.content
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                corrupt_member = archive.testzip()
        except zipfile.BadZipFile:
            raise SourceRequestError("Z.1 package download is not a valid zip archive") from None
        if corrupt_member is not None:
            raise SourceRequestError(
                f"Z.1 package download has a corrupt ZIP member: {corrupt_member}"
            )
        digest = hashlib.sha256(content).hexdigest()
        vintage = f"z1-release:{release_date}|sha256:{digest}"
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


def _validate_package(zip_path: Path, manifest: Any, expected_release_date: str) -> None:
    if not isinstance(manifest, dict):
        raise SourceRequestError("Z.1 package manifest root is not an object")
    required_types = {
        "source_url": str,
        "retrieved_at_utc": str,
        "sha256": str,
        "byte_count": int,
        "vintage": str,
        "release_date": str,
        "http": dict,
    }
    for field, expected_type in required_types.items():
        if not isinstance(manifest.get(field), expected_type):
            raise SourceRequestError(f"Z.1 package manifest has invalid {field}")
    if manifest["source_url"] != Z1_PACKAGE_URL:
        raise SourceRequestError("Z.1 package manifest source URL is invalid")
    if manifest["release_date"] != expected_release_date:
        raise SourceRequestError("Z.1 package manifest release date is stale")
    try:
        content = zip_path.read_bytes()
    except OSError:
        raise SourceRequestError("Z.1 package cache is unreadable") from None
    digest = hashlib.sha256(content).hexdigest()
    if len(content) != manifest["byte_count"]:
        raise SourceRequestError("Z.1 package cache byte count does not match its manifest")
    if digest != manifest["sha256"]:
        raise SourceRequestError("Z.1 package cache digest does not match its manifest")
    expected_vintage = f"z1-release:{expected_release_date}|sha256:{digest}"
    if manifest["vintage"] != expected_vintage:
        raise SourceRequestError("Z.1 package manifest vintage does not match its bytes")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            corrupt_member = archive.testzip()
    except zipfile.BadZipFile:
        raise SourceRequestError("Z.1 package cache is not a valid zip archive") from None
    if corrupt_member is not None:
        raise SourceRequestError(f"Z.1 package cache has a corrupt ZIP member: {corrupt_member}")


def _search_result(
    meta: Z1SeriesMeta, manifest: dict[str, Any], *, exact: bool
) -> dict[str, str | bool | dict[str, str]]:
    result: dict[str, str | bool | dict[str, str]] = {
        "id": meta.code,
        "series_id": meta.code,
        "title": meta.description,
        "family": "z1",
        "frequency": "annual" if meta.code.endswith(".A") else "quarterly",
        "kind": meta.code[:2],
        "table": meta.table,
        "curated": False,
        "source": "z1_data_dictionary",
        "release_date": manifest["release_date"],
        "tools": {"z1_series": {"code": meta.code, "kind": meta.code[:2]}},
    }
    if not exact:
        result["units"] = meta.units
    return result


def _search_tokens(value: str) -> set[str]:
    normalized = value.casefold()
    normalized = re.sub(r"holding (?:gain|gains|loss|losses)", "revaluation", normalized)
    normalized = normalized.replace("valuation changes", "revaluation")
    tokens = re.findall(r"[a-z0-9]+", normalized)
    return {token[:-1] if token.endswith("s") and len(token) > 3 else token for token in tokens}
