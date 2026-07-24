from __future__ import annotations

import csv
import hashlib
import io
import json
import mimetypes
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fiscus.config import data_root
from fiscus.csvutil import delimited_bytes_to_rows
from fiscus.errors import CacheCollisionError, CacheError

_HEX = frozenset("0123456789abcdef")
_INIT_ATTEMPTS = 6
_BUSY_TIMEOUT_MS = 5_000
_SCHEMA_VERSION = 2

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS artifacts (
        cache_key     TEXT PRIMARY KEY,
        dataset_id    TEXT NOT NULL,
        params_json   TEXT NOT NULL,
        vintage       TEXT NOT NULL,
        sha256        TEXT NOT NULL,
        byte_count    INTEGER NOT NULL,
        data_file     TEXT NOT NULL,
        manifest_json TEXT NOT NULL,
        payload       BLOB NOT NULL,
        created_at    TEXT NOT NULL,
        UNIQUE (dataset_id, params_json, vintage)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS latest (
        request_key TEXT PRIMARY KEY,
        dataset_id  TEXT NOT NULL,
        cache_key   TEXT NOT NULL,
        vintage     TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        FOREIGN KEY (cache_key) REFERENCES artifacts(cache_key) ON DELETE RESTRICT
    )
    """,
)


@dataclass(frozen=True)
class ArtifactDescriptor:
    """Opaque identity and content metadata for one immutable cache artifact."""

    cache_key: str
    filename: str
    mime_type: str
    sha256: str
    byte_count: int

    def as_dict(self) -> dict[str, str | int]:
        return {
            "cache_key": self.cache_key,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "sha256": self.sha256,
            "byte_count": self.byte_count,
        }


@dataclass(frozen=True)
class CachedArtifact:
    descriptor: ArtifactDescriptor
    manifest: dict[str, Any]
    rows: list[dict[str, str]]
    cache_hit: bool


class CacheManager:
    """Device-local generic cache backed by one SQLite database.

    The durable cache source of truth is ``cache/v2/fiscus.sqlite3``. Every cached
    payload, immutable manifest, and per-request latest pointer is stored as a row;
    fiscus does not materialize cache-owned artifact files or manifest sidecars.

    The connection uses rollback-journal ``DELETE`` mode, ``synchronous=FULL``, foreign
    keys, and a bounded busy timeout. Writes use short ``BEGIN IMMEDIATE`` transactions,
    so concurrent writers serialize before checking immutable identity. Callers obtain
    full content with :meth:`read_bytes`, :meth:`iter_csv_rows`, or an explicit
    caller-owned :meth:`export`.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = data_root(root) / "cache" / "v2"
        self.db_path = self.root / "fiscus.sqlite3"
        self._conn: sqlite3.Connection | None = None

    # -- connection / lifecycle ------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self.root.mkdir(parents=True, exist_ok=True)
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(_INIT_ATTEMPTS):
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=_BUSY_TIMEOUT_MS / 1_000,
                isolation_level=None,
            )
            try:
                conn.row_factory = sqlite3.Row
                conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA synchronous=FULL")
                # DELETE mode has a transient rollback journal during a write, but no
                # persistent -wal/-shm files and one database file at quiescent rest.
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in _SCHEMA:
                        conn.execute(statement)
                    schema_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                    if schema_version > _SCHEMA_VERSION:
                        raise CacheError(
                            f"cache database schema {schema_version} is newer than supported "
                            f"schema {_SCHEMA_VERSION}: {self.db_path}"
                        )
                    stale_journal = self.db_path.with_name(
                        f"{self.db_path.name}-journal"
                    ).exists()
                    if schema_version < _SCHEMA_VERSION or stale_journal:
                        # Writing user_version marks the schema and also lets SQLite remove a
                        # non-hot rollback journal left by a process that died before commit.
                        conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
                self._conn = conn
                return conn
            except sqlite3.OperationalError as error:
                conn.close()
                last_error = error
                time.sleep(0.05 * (attempt + 1))
            except BaseException:
                conn.close()
                raise
        raise CacheError(f"could not initialize cache database at {self.db_path}: {last_error}")

    def sqlite_runtime(self) -> str:
        return sqlite3.sqlite_version

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> CacheManager:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    @contextmanager
    def _write_transaction(conn: sqlite3.Connection) -> Iterator[None]:
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()

    # -- writes ----------------------------------------------------------------

    def store(
        self,
        dataset_id: str,
        params: dict[str, Any],
        vintage: str,
        data: bytes,
        manifest: dict[str, Any],
        data_file: str = "data.csv",
    ) -> CachedArtifact:
        full_key = self._write(
            dataset_id, params, vintage, data, manifest, data_file, set_latest=True
        )
        # Return the persisted artifact, not the caller's inputs: on an idempotent
        # re-store the first writer's manifest/filename is authoritative.
        return self._load_by_key(full_key, cache_hit=False)

    def _write(
        self,
        dataset_id: str,
        params: dict[str, Any],
        vintage: str,
        data: bytes,
        manifest: dict[str, Any],
        data_file: str,
        *,
        set_latest: bool,
    ) -> str:
        params_json = _params_json(params)
        full_key = _digest({"dataset": dataset_id, "params": params, "vintage": vintage})
        request_key = _digest({"dataset": dataset_id, "params": params})
        digest = hashlib.sha256(data).hexdigest()
        safe_data_file = _safe_component(data_file)

        payload = dict(manifest)
        payload.update(
            {
                "cache_key": full_key,
                "dataset_id": dataset_id,
                "params": params,
                "vintage": vintage,
                "sha256": digest,
                "byte_count": len(data),
                "data_file": safe_data_file,
                "mime_type": _mime_type(safe_data_file, manifest),
            }
        )
        manifest_json = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

        conn = self._connect()
        with self._write_transaction(conn):
            existing = conn.execute(
                "SELECT sha256, payload FROM artifacts WHERE cache_key = ?", (full_key,)
            ).fetchone()
            if existing is not None:
                if existing["sha256"] != digest or bytes(existing["payload"]) != data:
                    raise CacheCollisionError(
                        f"cache key collision for {dataset_id!r}, vintage {vintage!r}"
                    )
                # Identical bytes already stored: idempotent, keep the first writer's row.
            else:
                try:
                    conn.execute(
                        "INSERT INTO artifacts (cache_key, dataset_id, params_json, vintage, "
                        "sha256, byte_count, data_file, manifest_json, payload, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            full_key,
                            dataset_id,
                            params_json,
                            vintage,
                            digest,
                            len(data),
                            safe_data_file,
                            manifest_json,
                            data,
                            datetime.now(UTC).isoformat(),
                        ),
                    )
                except sqlite3.IntegrityError:
                    raced = conn.execute(
                        "SELECT sha256, payload FROM artifacts WHERE cache_key = ?", (full_key,)
                    ).fetchone()
                    if (
                        raced is None
                        or raced["sha256"] != digest
                        or bytes(raced["payload"]) != data
                    ):
                        raise CacheCollisionError(
                            f"cache key collision for {dataset_id!r}, vintage {vintage!r}"
                        ) from None
            if set_latest:
                self._set_latest(conn, request_key, dataset_id, full_key, vintage)
        return full_key

    @staticmethod
    def _set_latest(
        conn: sqlite3.Connection,
        request_key: str,
        dataset_id: str,
        cache_key: str,
        vintage: str,
    ) -> None:
        conn.execute(
            "INSERT INTO latest (request_key, dataset_id, cache_key, vintage, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(request_key) DO UPDATE SET "
            "cache_key = excluded.cache_key, vintage = excluded.vintage, "
            "updated_at = excluded.updated_at",
            (request_key, dataset_id, cache_key, vintage, datetime.now(UTC).isoformat()),
        )

    # -- reads -----------------------------------------------------------------

    def load_latest(
        self,
        dataset_id: str,
        params: dict[str, Any],
    ) -> CachedArtifact | None:
        request_key = _digest({"dataset": dataset_id, "params": params})
        conn = self._connect()
        pointer = conn.execute(
            "SELECT cache_key FROM latest WHERE request_key = ?", (request_key,)
        ).fetchone()
        if pointer is None:
            return None
        return self._load_by_key(str(pointer["cache_key"]), cache_hit=True)

    def find_by_cache_key(self, cache_key: str) -> CachedArtifact:
        """Resolve an immutable artifact by its full cache key or a unique prefix."""
        return self._load_by_key(self._resolve_key(cache_key), cache_hit=True)

    def read_bytes(self, cache_key: str) -> bytes:
        """Return the exact verified payload for a full cache key or unique prefix."""
        resolved = self._resolve_key(cache_key)
        _, _, data = self._read_record(resolved)
        return data

    def iter_csv_rows(self, cache_key: str) -> Iterator[dict[str, str]]:
        """Iterate canonical CSV/TSV rows for a full cache key or unique prefix.

        The database BLOB is verified and read before this method returns the iterator, so
        iteration does not depend on a live cursor or a cache-owned materialized file.
        """
        resolved = self._resolve_key(cache_key)
        descriptor, _, data = self._read_record(resolved)
        delimiter = _tabular_delimiter(descriptor)
        if delimiter is None:
            raise CacheError(f"cache artifact is not CSV or TSV: {resolved}")
        text = io.StringIO(data.decode("utf-8-sig"), newline="")
        reader = csv.DictReader(text, delimiter=delimiter)
        return (dict(row) for row in reader)

    def _resolve_key(self, cache_key: str) -> str:
        needle = _validate_key(cache_key)
        conn = self._connect()
        matches = conn.execute(
            "SELECT cache_key FROM artifacts WHERE cache_key GLOB ? ORDER BY cache_key",
            (needle + "*",),
        ).fetchall()
        if not matches:
            raise CacheError(f"no cached artifact matches cache_key {needle!r}")
        if len(matches) > 1:
            raise CacheError(f"cache_key prefix {needle!r} is ambiguous ({len(matches)} artifacts)")
        return str(matches[0]["cache_key"])

    def _load_by_key(self, cache_key: str, *, cache_hit: bool) -> CachedArtifact:
        descriptor, manifest, data = self._read_record(cache_key)
        delimiter = _tabular_delimiter(descriptor)
        rows = (
            delimited_bytes_to_rows(data, delimiter=delimiter)
            if delimiter is not None
            else []
        )
        return CachedArtifact(
            descriptor=descriptor,
            manifest=manifest,
            rows=rows,
            cache_hit=cache_hit,
        )

    def _read_record(
        self, cache_key: str
    ) -> tuple[ArtifactDescriptor, dict[str, Any], bytes]:
        conn = self._connect()
        row = conn.execute(
            "SELECT cache_key, sha256, byte_count, data_file, manifest_json, payload "
            "FROM artifacts WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if row is None:
            raise CacheError(f"cache artifact is incomplete: {cache_key}")
        try:
            manifest = json.loads(str(row["manifest_json"]))
        except json.JSONDecodeError as error:
            raise CacheError(f"cache manifest is invalid: {cache_key}") from error
        if not isinstance(manifest, dict):
            raise CacheError(f"cache manifest is invalid: {cache_key}")

        data = bytes(row["payload"])
        expected_sha = str(row["sha256"])
        expected_count = int(row["byte_count"])
        actual_sha = hashlib.sha256(data).hexdigest()
        if actual_sha != expected_sha or len(data) != expected_count:
            raise CacheError(f"cache hash/length mismatch: {cache_key}")
        consistency = {
            "cache_key": str(row["cache_key"]),
            "sha256": expected_sha,
            "byte_count": expected_count,
            "data_file": str(row["data_file"]),
        }
        for field, expected in consistency.items():
            if field in manifest and manifest[field] != expected:
                raise CacheError(f"cache manifest disagrees with stored {field}: {cache_key}")

        filename = str(row["data_file"])
        mime_type = _mime_type(filename, manifest)
        descriptor = ArtifactDescriptor(
            cache_key=str(row["cache_key"]),
            filename=filename,
            mime_type=mime_type,
            sha256=expected_sha,
            byte_count=expected_count,
        )
        return descriptor, manifest, data

    # -- explicit export / migration ------------------------------------------

    def export(self, cache_key: str, dest: str | Path) -> Path:
        """Write a verified cached payload to a caller-owned destination.

        Accepts a full cache key or a unique prefix. The destination is explicitly outside
        the cache contract: fiscus creates it at the caller's request and never treats it as
        a database backing file.
        """
        data = self.read_bytes(cache_key)
        destination = Path(dest).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return destination

    def migrate_v1(self, v1_root: str | Path | None = None) -> dict[str, Any]:
        """Read-only, finite migration of a legacy filesystem (v1) cache.

        The old ``cache/v1`` forest is never modified or deleted. Valid artifacts are
        inserted directly into SQLite without materializing cache-owned files. An
        authoritative usable ``latest.json`` pointer is preserved; malformed, missing, or
        content-mismatched legacy artifacts are skipped with path-specific diagnostics.
        """
        legacy_root = Path(v1_root).expanduser() if v1_root else (self.root.parent / "v1")
        report: dict[str, Any] = {
            "legacy_root": str(legacy_root.resolve()),
            "requests_scanned": 0,
            "migrated": 0,
            "skipped": 0,
            "latest_preserved": 0,
            "latest_fallbacks": 0,
            "diagnostics": [],
        }
        if not legacy_root.is_dir():
            return report
        for request_dir in sorted(legacy_root.glob("datasets/*/*")):
            if not request_dir.is_dir():
                continue
            report["requests_scanned"] += 1
            vintage_keys: dict[str, tuple[str, str, dict[str, Any]]] = {}
            for manifest_path in sorted(request_dir.glob("*/manifest.json")):
                loaded, reason = _read_v1_artifact(manifest_path)
                if loaded is None:
                    report["skipped"] += 1
                    report["diagnostics"].append(
                        {"manifest_path": str(manifest_path), "reason": reason}
                    )
                    continue
                dataset_id, params, vintage, data_file, data, base = loaded
                try:
                    full_key = self._write(
                        dataset_id, params, vintage, data, base, data_file, set_latest=False
                    )
                except CacheCollisionError as error:
                    report["skipped"] += 1
                    report["diagnostics"].append(
                        {"manifest_path": str(manifest_path), "reason": str(error)}
                    )
                    continue
                vintage_keys[vintage] = (full_key, dataset_id, params)
                report["migrated"] += 1
            if vintage_keys:
                latest_status = self._preserve_latest(request_dir, vintage_keys, report)
                if latest_status == "preserved":
                    report["latest_preserved"] += 1
                elif latest_status == "fallback":
                    report["latest_fallbacks"] += 1
        return report

    def _preserve_latest(
        self,
        request_dir: Path,
        vintage_keys: dict[str, tuple[str, str, dict[str, Any]]],
        report: dict[str, Any],
    ) -> str | None:
        pointer_path = request_dir / "latest.json"
        chosen_vintage: str | None = None
        pointer_preserved = False
        if pointer_path.is_file():
            try:
                pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                report["diagnostics"].append(
                    {
                        "manifest_path": str(pointer_path),
                        "reason": f"invalid latest pointer: {error}",
                    }
                )
            else:
                candidate = pointer.get("vintage") if isinstance(pointer, dict) else None
                if isinstance(candidate, str) and candidate in vintage_keys:
                    chosen_vintage = candidate
                    pointer_preserved = True
                elif isinstance(candidate, str):
                    report["diagnostics"].append(
                        {
                            "manifest_path": str(pointer_path),
                            "reason": f"latest vintage was not migrated: {candidate}",
                        }
                    )
                    # A syntactically valid v1 pointer is authoritative. Do not silently
                    # substitute another vintage when its target was absent or corrupt.
                    return None
                else:
                    report["diagnostics"].append(
                        {
                            "manifest_path": str(pointer_path),
                            "reason": "latest pointer has no string vintage",
                        }
                    )
        if chosen_vintage is None:
            # No usable legacy pointer: choose the last artifact in deterministic scan order.
            chosen_vintage = next(reversed(vintage_keys))
        full_key, dataset_id, params = vintage_keys[chosen_vintage]
        request_key = _digest({"dataset": dataset_id, "params": params})
        conn = self._connect()
        with self._write_transaction(conn):
            self._set_latest(conn, request_key, dataset_id, full_key, chosen_vintage)
        return "preserved" if pointer_preserved else "fallback"


def canonical_params(params: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key in sorted(params):
        value = params[key]
        if value is None:
            continue
        if isinstance(value, dict):
            normalized[key] = canonical_params(value)
        elif isinstance(value, (set, frozenset)):
            normalized[key] = sorted(str(item) for item in value)
        elif isinstance(value, (list, tuple)):
            normalized[key] = [str(item) for item in value]
        elif isinstance(value, bool):
            normalized[key] = value
        else:
            normalized[key] = str(value)
    return normalized


def _read_v1_artifact(
    manifest_path: Path,
) -> tuple[
    tuple[str, dict[str, Any], str, str, bytes, dict[str, Any]] | None,
    str,
]:
    """Read and verify one legacy artifact, returning a diagnostic on failure."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as error:
        return None, f"manifest is unreadable: {error}"
    except json.JSONDecodeError as error:
        return None, f"manifest is invalid JSON: {error}"
    if not isinstance(manifest, dict):
        return None, "manifest root is not an object"
    dataset_id = manifest.get("dataset_id")
    vintage = manifest.get("vintage")
    params = manifest.get("params", {})
    data_file = str(manifest.get("data_file", "data.csv"))
    if (
        not isinstance(dataset_id, str)
        or not isinstance(vintage, str)
        or not isinstance(params, dict)
    ):
        return None, "manifest lacks a valid dataset_id, params object, or vintage"
    data_path = manifest_path.parent / _safe_component(data_file)
    if not data_path.is_file():
        return None, f"payload is missing: {data_path.name}"
    try:
        data = data_path.read_bytes()
    except OSError as error:
        return None, f"payload is unreadable: {error}"
    recorded_sha = manifest.get("sha256")
    recorded_len = manifest.get("byte_count")
    if isinstance(recorded_sha, str) and hashlib.sha256(data).hexdigest() != recorded_sha:
        return None, "sha256 mismatch"
    if isinstance(recorded_len, int) and recorded_len != len(data):
        return None, "byte_count mismatch"
    base = {
        key: value
        for key, value in manifest.items()
        if key
        not in {
            "cache_key",
            "dataset_id",
            "params",
            "vintage",
            "sha256",
            "byte_count",
            "data_file",
        }
    }
    return (dataset_id, params, vintage, data_file, data, base), ""


def _mime_type(filename: str, manifest: dict[str, Any]) -> str:
    declared = manifest.get("mime_type")
    if isinstance(declared, str) and declared.strip():
        return declared.split(";", 1)[0].strip().lower()
    if filename.lower().endswith(".csv"):
        return "text/csv"
    if filename.lower().endswith(".tsv"):
        return "text/tab-separated-values"
    source_metadata = manifest.get("source_metadata")
    if isinstance(source_metadata, dict):
        http_metadata = source_metadata.get("http")
        if isinstance(http_metadata, dict):
            content_type = http_metadata.get("content-type")
            if isinstance(content_type, str) and content_type.strip():
                return content_type.split(";", 1)[0].strip().lower()
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def _tabular_delimiter(descriptor: ArtifactDescriptor) -> str | None:
    if (
        descriptor.mime_type == "text/tab-separated-values"
        or descriptor.filename.lower().endswith(".tsv")
    ):
        return "\t"
    if descriptor.mime_type == "text/csv" or descriptor.filename.lower().endswith(".csv"):
        return ","
    return None


def _params_json(params: dict[str, Any]) -> str:
    return json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_key(cache_key: str) -> str:
    needle = cache_key.strip().lower()
    if len(needle) < 12 or any(ch not in _HEX for ch in needle):
        raise CacheError("cache_key must be at least 12 hex characters")
    return needle


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-.")
    return cleaned or "item"
