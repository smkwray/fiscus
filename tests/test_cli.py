from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fiscus.cache import CacheManager
from fiscus.cli import main


def test_catalog_lint_cli(capsys) -> None:
    assert main(["catalog", "lint"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["entries"] == 29


def test_find_series_cli(capsys, tmp_path: Path) -> None:
    assert main(["--data-dir", str(tmp_path), "find-series", "MSPD CUSIP"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["results"][0]["id"] == "fiscaldata.mspd_table_3_market"


def test_cache_export_cli_writes_caller_owned_file(capsys, tmp_path: Path) -> None:
    payload = b"date,value\n2026-01-01,1\n"
    with CacheManager(tmp_path / "data") as cache:
        artifact = cache.store(
            "fred.example",
            {},
            "release:1",
            payload,
            {"source_url": "https://example.test", "retrieved_at_utc": "2026-01-01Z"},
        )
    destination = tmp_path / "exports" / "example.csv"
    assert (
        main(
            [
                "--data-dir",
                str(tmp_path / "data"),
                "cache",
                "export",
                artifact.descriptor.cache_key[:20],
                "--out",
                str(destination),
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["exported_to"] == str(destination.resolve())
    assert output["byte_count"] == len(payload)
    assert destination.read_bytes() == payload


def test_cache_migrate_v1_cli_is_explicit_and_read_only(capsys, tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    artifact_dir = legacy / "datasets" / "fred.legacy" / ("a" * 24) / "release-1--deadbeef"
    artifact_dir.mkdir(parents=True)
    payload = b"date,value\n2026-01-01,1\n"
    (artifact_dir / "data.csv").write_bytes(payload)
    (artifact_dir / "manifest.json").write_text(
        json.dumps(
            {
                "dataset_id": "fred.legacy",
                "params": {},
                "vintage": "release:1",
                "data_file": "data.csv",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "byte_count": len(payload),
                "source_url": "https://example.test/legacy",
            }
        ),
        encoding="utf-8",
    )
    before = (artifact_dir / "manifest.json").read_bytes()

    assert (
        main(
            [
                "--data-dir",
                str(tmp_path / "data"),
                "cache",
                "migrate-v1",
                "--from",
                str(legacy),
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["migrated"] == 1
    assert output["skipped"] == 0
    assert output["latest_preserved"] == 0
    assert output["latest_fallbacks"] == 1
    assert (artifact_dir / "manifest.json").read_bytes() == before
