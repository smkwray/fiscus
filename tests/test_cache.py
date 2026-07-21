from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from fiscus.cache import CacheManager
from fiscus.errors import CacheCollisionError, CacheError

_MANIFEST = {
    "source_url": "https://example.test/data",
    "retrieved_at_utc": "2026-07-20T12:00:00+00:00",
    "source_metadata": {},
    "row_count": 1,
    "data_coverage": {"start": "2024-01-01", "end": "2024-01-01"},
}


def _csv(value: int) -> bytes:
    return f"date,value\n2024-01-01,{value}\n".encode()


def test_cache_bytes_are_immutable_within_vintage(tmp_path: Path) -> None:
    """Different bytes for one dataset/params/vintage identity are a collision."""
    with CacheManager(tmp_path) as cache:
        cache.store("fred.example", {}, "release:1", _csv(1), _MANIFEST)
        with pytest.raises(CacheCollisionError):
            cache.store("fred.example", {}, "release:1", _csv(2), _MANIFEST)


def test_at_rest_surface_is_a_single_database_file(tmp_path: Path) -> None:
    """Many datasets/vintages leave one database and no artifact directories."""
    with CacheManager(tmp_path) as cache:
        cache.store("fred.a", {"start": "2024"}, "release:1", _csv(1), _MANIFEST)
        cache.store("fred.a", {"start": "2024"}, "release:2", _csv(2), _MANIFEST)
        cache.store("bea.b", {"year": "2023"}, "retrieval:1", _csv(3), _MANIFEST)
        cache.store("bls.c", {}, "bls:1", _csv(4), _MANIFEST, data_file="series.csv")
        cache_dir = tmp_path / "cache" / "v2"
        assert sorted(path.name for path in cache_dir.iterdir()) == ["fiscus.sqlite3"]
        assert not any(path.is_dir() for path in cache_dir.iterdir())
    assert sorted(path.name for path in cache_dir.iterdir()) == ["fiscus.sqlite3"]


def test_connection_policy_and_immediate_write_transactions(tmp_path: Path) -> None:
    with CacheManager(tmp_path) as cache:
        conn = cache._connect()
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2

        traced: list[str] = []
        conn.set_trace_callback(traced.append)
        cache.store("fred.a", {}, "release:1", _csv(1), _MANIFEST)
        conn.set_trace_callback(None)
        assert any(statement.strip().upper() == "BEGIN IMMEDIATE" for statement in traced)


def test_latest_pointer_tracks_newest_store_and_cache_hit_flag(tmp_path: Path) -> None:
    with CacheManager(tmp_path) as cache:
        first = cache.store("fred.a", {}, "release:1", _csv(1), _MANIFEST)
        assert first.cache_hit is False
        cache.store("fred.a", {}, "release:2", _csv(2), _MANIFEST)

        latest = cache.load_latest("fred.a", {})
        assert latest is not None
        assert latest.cache_hit is True
        assert latest.manifest["vintage"] == "release:2"
        assert latest.rows == [{"date": "2024-01-01", "value": "2"}]
        assert latest.descriptor.as_dict() == {
            "cache_key": latest.manifest["cache_key"],
            "filename": "data.csv",
            "mime_type": "text/csv",
            "sha256": hashlib.sha256(_csv(2)).hexdigest(),
            "byte_count": len(_csv(2)),
        }
        assert cache.load_latest("fred.missing", {}) is None


def test_binary_payload_read_bytes_and_non_csv_guard(tmp_path: Path) -> None:
    blob = bytes(range(256)) * 4
    with CacheManager(tmp_path) as cache:
        stored = cache.store(
            "hqm.workbook", {}, "retrieval:1", blob, _MANIFEST, data_file="book.xls"
        )
        key = stored.descriptor.cache_key
        assert stored.descriptor.filename == "book.xls"
        assert stored.descriptor.mime_type == "application/vnd.ms-excel"
        assert stored.rows == []
        assert cache.read_bytes(key) == blob
        assert cache.read_bytes(key[:20]) == blob
        with pytest.raises(CacheError, match="not CSV"):
            cache.iter_csv_rows(key)


def test_iter_csv_rows_reads_verified_payload_without_a_file(tmp_path: Path) -> None:
    with CacheManager(tmp_path) as cache:
        artifact = cache.store("fred.a", {}, "release:1", _csv(7), _MANIFEST)
        rows = list(cache.iter_csv_rows(artifact.descriptor.cache_key[:20]))
        assert rows == [{"date": "2024-01-01", "value": "7"}]
        assert cache.read_bytes(artifact.descriptor.cache_key) == _csv(7)


def test_idempotent_store_returns_the_persisted_first_writer_artifact(tmp_path: Path) -> None:
    """Identical re-store returns the first writer's persisted manifest and filename."""
    with CacheManager(tmp_path) as cache:
        cache.store("fred.a", {}, "release:1", _csv(1), {"source_url": "X"}, data_file="a.csv")
        again = cache.store(
            "fred.a", {}, "release:1", _csv(1), {"source_url": "Y"}, data_file="b.csv"
        )
        assert again.manifest["source_url"] == "X"
        assert again.manifest["data_file"] == "a.csv"
        assert again.descriptor.filename == "a.csv"


def test_find_by_cache_key_full_prefix_and_invalid(tmp_path: Path) -> None:
    with CacheManager(tmp_path) as cache:
        artifact = cache.store("fred.a", {}, "release:1", _csv(1), _MANIFEST)
        key = artifact.descriptor.cache_key
        assert cache.find_by_cache_key(key).descriptor.cache_key == key
        assert cache.find_by_cache_key(key[:16]).descriptor.cache_key == key
        with pytest.raises(CacheError, match="at least 12 hex"):
            cache.find_by_cache_key("fred.a")
        with pytest.raises(CacheError, match="no cached artifact"):
            cache.find_by_cache_key("0" * 64)


def test_find_by_cache_key_ambiguous_prefix_is_rejected(tmp_path: Path) -> None:
    """Construct two keys sharing 12 hex characters instead of relying on chance."""
    with CacheManager(tmp_path) as cache:
        conn = cache._connect()
        shared = "abcdef123456"
        for suffix in ("0000", "1111"):
            key = shared + suffix + "0" * (64 - len(shared) - 4)
            conn.execute(
                "INSERT INTO artifacts (cache_key, dataset_id, params_json, vintage, sha256, "
                "byte_count, data_file, manifest_json, payload, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (key, "fred.x", "{}", f"v-{suffix}", "0" * 64, 0, "data.csv", "{}", b"", "t"),
            )
        with pytest.raises(CacheError, match="ambiguous"):
            cache.find_by_cache_key(shared)


def test_concurrent_writers_initialize_and_enforce_immutability(tmp_path: Path) -> None:
    """Concurrent writers against a fresh root initialize cleanly and serialize identity."""
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    collisions: list[bool] = []

    def writer(value: int) -> None:
        try:
            with CacheManager(tmp_path) as cache:
                barrier.wait(timeout=10)
                try:
                    cache.store("fred.race", {}, "release:1", _csv(value), _MANIFEST)
                    collisions.append(False)
                except CacheCollisionError:
                    collisions.append(True)
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=writer, args=(value,)) for value in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == [], errors
    assert collisions.count(True) == 1, collisions

    barrier2 = threading.Barrier(2)
    same_errors: list[BaseException] = []

    def same_writer() -> None:
        try:
            with CacheManager(tmp_path) as cache:
                barrier2.wait(timeout=10)
                cache.store("fred.same", {}, "release:1", _csv(7), _MANIFEST)
        except BaseException as error:
            same_errors.append(error)

    threads = [threading.Thread(target=same_writer) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert not any(thread.is_alive() for thread in threads)
    assert same_errors == []


def test_delete_journal_recovers_crashed_writer_and_returns_to_one_file(
    tmp_path: Path,
) -> None:
    """A clean reopen removes a rollback journal left by a dead writer."""
    with CacheManager(tmp_path) as cache:
        baseline = cache.store("fred.a", {}, "release:1", _csv(1), _MANIFEST)
    database = tmp_path / "cache" / "v2" / "fiscus.sqlite3"
    child = """
import os
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1], isolation_level=None)
conn.execute("PRAGMA journal_mode=DELETE")
conn.execute("PRAGMA synchronous=FULL")
conn.execute("BEGIN IMMEDIATE")
conn.execute("UPDATE artifacts SET created_at = 'uncommitted' WHERE cache_key = ?", (sys.argv[2],))
os._exit(23)
"""
    completed = subprocess.run(
        [sys.executable, "-c", child, str(database), baseline.descriptor.cache_key],
        check=False,
    )
    assert completed.returncode == 23

    with CacheManager(tmp_path) as cache:
        assert cache.read_bytes(baseline.descriptor.cache_key) == _csv(1)
    assert sorted(path.name for path in database.parent.iterdir()) == ["fiscus.sqlite3"]


def test_export_writes_a_caller_owned_file(tmp_path: Path) -> None:
    with CacheManager(tmp_path) as cache:
        artifact = cache.store("fred.a", {}, "release:1", _csv(7), _MANIFEST)
        destination = tmp_path / "out" / "series.csv"
        assert cache.export(artifact.descriptor.cache_key[:20], destination) == destination
        assert destination.read_bytes() == _csv(7)
        assert destination.parent != cache.root


def _write_v1_artifact(request_dir: Path, vintage_slug: str, vintage: str, data: bytes) -> None:
    artifact_dir = request_dir / (
        f"{vintage_slug}--{hashlib.sha256(vintage.encode()).hexdigest()[:8]}"
    )
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "data.csv").write_bytes(data)
    (artifact_dir / "manifest.json").write_text(
        json.dumps(
            {
                "dataset_id": "fred.legacy",
                "params": {},
                "vintage": vintage,
                "data_file": "data.csv",
                "sha256": hashlib.sha256(data).hexdigest(),
                "byte_count": len(data),
                "source_url": "https://example.test/legacy",
            }
        ),
        encoding="utf-8",
    )


def test_migrate_v1_preserves_pointer_diagnoses_corruption_and_writes_no_forest(
    tmp_path: Path,
) -> None:
    """Migration is read-only, insert-only, pointer-preserving, and diagnostic."""
    legacy = tmp_path / "cache" / "v1"
    request_dir = legacy / "datasets" / "fred.legacy" / ("a" * 24)
    _write_v1_artifact(request_dir, "z-release-2", "release:2", _csv(2))
    _write_v1_artifact(request_dir, "a-release-1", "release:1", _csv(1))
    (request_dir / "latest.json").write_text(
        json.dumps({"vintage": "release:1"}), encoding="utf-8"
    )

    corrupt = request_dir.parent.parent / "fred.bad" / ("b" * 24) / "release-1--dead"
    corrupt.mkdir(parents=True)
    (corrupt / "data.csv").write_bytes(_csv(9))
    (corrupt / "manifest.json").write_text(
        json.dumps(
            {
                "dataset_id": "fred.bad",
                "params": {},
                "vintage": "release:1",
                "data_file": "data.csv",
                "sha256": "0" * 64,
                "byte_count": len(_csv(9)),
            }
        ),
        encoding="utf-8",
    )
    legacy_snapshot = {
        path.relative_to(legacy): path.read_bytes() for path in legacy.rglob("*") if path.is_file()
    }

    with CacheManager(tmp_path) as cache:
        report = cache.migrate_v1()
        assert report["migrated"] == 2
        assert report["requests_scanned"] == 2
        assert report["skipped"] == 1
        assert report["latest_preserved"] == 1
        assert report["latest_fallbacks"] == 0
        assert report["diagnostics"] == [
            {
                "manifest_path": str(corrupt / "manifest.json"),
                "reason": "sha256 mismatch",
            }
        ]
        latest = cache.load_latest("fred.legacy", {})
        assert latest is not None
        assert latest.manifest["vintage"] == "release:1"
        assert cache.read_bytes(latest.descriptor.cache_key) == _csv(1)
        assert cache.load_latest("fred.bad", {}) is None
        assert sorted(path.name for path in cache.root.iterdir()) == ["fiscus.sqlite3"]

    assert legacy_snapshot == {
        path.relative_to(legacy): path.read_bytes() for path in legacy.rglob("*") if path.is_file()
    }
