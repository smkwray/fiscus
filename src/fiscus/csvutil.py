from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable, Mapping
from typing import Any


def rows_to_csv_bytes(
    rows: Iterable[Mapping[str, Any]],
    *,
    preferred_fields: Iterable[str] = (),
) -> bytes:
    materialized = [dict(row) for row in rows]
    fieldnames = _fieldnames(materialized, preferred_fields)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=fieldnames,
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    for row in materialized:
        writer.writerow({field: _cell(row.get(field)) for field in fieldnames})
    return buffer.getvalue().encode("utf-8")


def delimited_bytes_to_rows(
    data: bytes,
    *,
    delimiter: str = ",",
) -> list[dict[str, str]]:
    text = data.decode("utf-8-sig")
    return [dict(row) for row in csv.DictReader(io.StringIO(text), delimiter=delimiter)]


def csv_bytes_to_rows(data: bytes) -> list[dict[str, str]]:
    return delimited_bytes_to_rows(data)


def _fieldnames(
    rows: list[dict[str, Any]],
    preferred_fields: Iterable[str],
) -> list[str]:
    preferred = list(dict.fromkeys(str(field) for field in preferred_fields))
    if not rows:
        return preferred or ["value"]
    discovered = {str(key) for row in rows for key in row}
    ordered: list[str] = []
    for field in preferred:
        if field in discovered and field not in ordered:
            ordered.append(field)
    ordered.extend(sorted(discovered.difference(ordered)))
    return ordered or ["value"]


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
