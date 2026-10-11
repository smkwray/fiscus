from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fiscus.catalog import Catalog
from fiscus.errors import SourceRequestError
from fiscus.service import FiscusService


def _overlay(tmp_path: Path, artifact: Path) -> Path:
    overlay = tmp_path / "local-catalog" / "local.yaml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text(
        textwrap.dedent(
            f"""
            schema_version: 1
            family: local
            entries:
              - id: local.tdc_estimates
                family: local
                title: Canonical quarterly TDC estimates (sibling artifact)
                description: Frozen processed output registered as a private local series.
                publisher: local project
                aliases: [TDC estimates, tdc quarterly anchor]
                tags: [local, tdc]
                verified_on: 2026-07-13
                history:
                  start: 1946-12-31
                frequency: quarterly
                units:
                  label: Millions of U.S. dollars
                  unit_class: usd_millions_nsa
                endpoint:
                  local_path: {artifact}
                access:
                  executor: local_file
                  mode: machine
                  auth:
                    mode: none
                response:
                  format: csv
                  date_fields: [date]
                  value_fields: [tdc]
                refresh:
                  cadence: static snapshot
                tools: [get_file]
                caveats:
                  - id: local.frozen_snapshot
                    severity: warning
                    message: Frozen sibling output; rebuild upstream before extending coverage.
            """
        ),
        encoding="utf-8",
    )
    return overlay


def test_local_file_overlay_is_discoverable_and_served_in_place(tmp_path: Path) -> None:
    """The private overlay is what turns fiscus into the front door for a
    project's own artifacts: a local CSV must be findable, previewed, and
    served from its original path — never copied into the cache."""
    artifact = tmp_path / "tdc_estimates.csv"
    artifact.write_text("date,tdc\n2025-12-31,-103400\n2026-03-31,58200\n", encoding="utf-8")
    overlay = _overlay(tmp_path, artifact)

    catalog = Catalog.load(extra_paths=[overlay])
    with FiscusService(catalog=catalog, data_dir=tmp_path / "cache") as service:
        found = service.find_series("TDC estimates")
        assert found["results"][0]["id"] == "local.tdc_estimates"

        result = service.get_file("local.tdc_estimates")

    assert result["preview"]["row_count"] == 2
    assert result["full_data_path"] == str(artifact.resolve())
    assert result["provenance"]["vintage"].startswith("file-mtime:")
    assert result["provenance"]["sha256"]
    assert result["manifest_path"] is None
    assert "artifact" not in result
    assert {item["id"] for item in result["caveats"]} == {"local.frozen_snapshot"}
    assert not list((tmp_path / "cache").rglob("data.csv"))


def test_local_file_missing_path_is_explicit(tmp_path: Path) -> None:
    artifact = tmp_path / "gone.csv"
    artifact.write_text("date,tdc\n2025-12-31,1\n", encoding="utf-8")
    overlay = _overlay(tmp_path, artifact)
    artifact.unlink()

    catalog = Catalog.load(extra_paths=[overlay])
    with (
        FiscusService(catalog=catalog, data_dir=tmp_path / "cache") as service,
        pytest.raises(SourceRequestError, match="does not exist"),
    ):
        service.get_file("local.tdc_estimates")


@pytest.mark.parametrize(
    "content",
    [
        b"date,tdc\n2025-12-31,\xff\n",
        b"date,tdc,tdc\n2025-12-31,1,2\n",
        b"date,\n2025-12-31,1\n",
        b"date,wrong_value\n2025-12-31,1\n",
        b"date,tdc\n2025-12-31,1,2\n",
        b"date,tdc\n2025-12-31\n",
        b'date,tdc\n2025-12-31,"unterminated\n',
    ],
)
def test_local_file_rejects_lossy_or_malformed_tabular_data(
    tmp_path: Path, content: bytes,
) -> None:
    artifact = tmp_path / "estimates.csv"
    artifact.write_bytes(content)
    catalog = Catalog.load(extra_paths=[_overlay(tmp_path, artifact)])
    with (
        FiscusService(catalog=catalog, data_dir=tmp_path / "cache") as service,
        pytest.raises(SourceRequestError),
    ):
        service.get_file("local.tdc_estimates")
