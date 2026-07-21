from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FetchResult:
    rows: list[dict[str, Any]]
    source_url: str
    retrieved_at_utc: str
    vintage: str
    source_metadata: dict[str, Any] = field(default_factory=dict)
