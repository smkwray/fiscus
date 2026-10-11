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
    required_fields: Iterable[str] = (),
) -> list[dict[str, str]]:
    text = data.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text), delimiter=delimiter, strict=True)
    headers = next(reader, None)
    if not headers or any(not header.strip() for header in headers):
        raise ValueError("tabular data has an empty or missing header")
    if len(headers) != len(set(headers)):
        raise ValueError("tabular data has duplicate headers")
    missing = sorted(set(required_fields).difference(headers))
    if missing:
        raise ValueError(f"tabular data omitted required columns: {', '.join(missing)}")
    rows = []
    for values in reader:
        if not values:
            continue
        if len(values) != len(headers):
            raise ValueError(
                f"tabular row {reader.line_num} has {len(values)} fields; expected {len(headers)}"
            )
        rows.append(dict(zip(headers, values, strict=True)))
    return rows


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
