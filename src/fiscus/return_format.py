from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from fiscus.cache import CachedArtifact
from fiscus.models import CatalogEntry


def date_coverage(
    rows: Iterable[dict[str, Any]],
    date_fields: Iterable[str],
) -> dict[str, str | None]:
    fields = list(date_fields)
    values: list[str] = []
    for row in rows:
        for field in fields:
            value = row.get(field)
            if value:
                values.append(str(value))
                break
    return {
        "start": min(values) if values else None,
        "end": max(values) if values else None,
    }


PREVIEW_COLUMN_CAP = 24


def preview_rows(
    rows: list[dict[str, Any]],
    entry: CatalogEntry,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int] | None]:
    return preview_rows_for_fields(
        rows,
        [*entry.response.date_fields, *entry.response.value_fields],
    )


def preview_rows_for_fields(
    rows: list[dict[str, Any]],
    preferred_fields: Iterable[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int] | None]:
    head = rows[:3]
    tail = rows[-2:] if len(rows) > 3 else []
    columns = list(rows[0]) if rows else []
    if len(columns) <= PREVIEW_COLUMN_CAP:
        return head, tail, None
    keep = list(dict.fromkeys(field for field in preferred_fields if field in columns))[
        :PREVIEW_COLUMN_CAP
    ]
    for column in columns:
        if len(keep) >= PREVIEW_COLUMN_CAP:
            break
        if column not in keep:
            keep.append(column)
    trimmed_head = [{field: row.get(field, "") for field in keep} for row in head]
    trimmed_tail = [{field: row.get(field, "") for field in keep} for row in tail]
    return trimmed_head, trimmed_tail, {"shown": len(keep), "total": len(columns)}


def build_fetch_response(
    entry: CatalogEntry,
    artifact: CachedArtifact,
    caveats: list[dict[str, Any]],
) -> dict[str, Any]:
    rows = artifact.rows
    head, tail, truncated = preview_rows(rows, entry)
    manifest = artifact.manifest
    return {
        "dataset": {
            "id": entry.id,
            "title": entry.title,
            "family": entry.family,
            "frequency": entry.frequency,
            "units": entry.units.model_dump(mode="json"),
            "semantics": entry.semantics.model_dump(mode="json"),
            "verified_on": entry.verified_on.isoformat(),
        },
        "preview": {
            "row_count": len(rows),
            "columns": list(rows[0]) if rows else [],
            "date_coverage": manifest.get("data_coverage"),
            "head": head,
            "tail": tail,
            **({"truncated_columns": truncated} if truncated else {}),
        },
        "caveats": caveats,
        "provenance": {
            "source_url": manifest["source_url"],
            "retrieved_at_utc": manifest["retrieved_at_utc"],
            "sha256": manifest["sha256"],
            "byte_count": manifest["byte_count"],
            "vintage": manifest["vintage"],
            "cache_key": manifest["cache_key"],
            "row_count": manifest.get("row_count", len(rows)),
            "data_coverage": manifest.get("data_coverage"),
            "source_metadata": manifest.get("source_metadata", {}),
        },
        "artifact": artifact.descriptor.as_dict(),
        "cache_hit": artifact.cache_hit,
    }
