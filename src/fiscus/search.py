from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from typing import Any

from fiscus.models import CatalogEntry, ExecutorKind

TOKEN_RE = re.compile(r"[a-z0-9]+")

QUERY_ALIASES: dict[str, tuple[str, ...]] = {
    "tga": ("treasury general account", "operating cash balance"),
    "dts": ("daily treasury statement",),
    "mspd": ("monthly statement public debt",),
    "soma": ("system open market account",),
    "z1": ("financial accounts", "flow of funds"),
    "deposits": ("bank deposits", "commercial bank deposits"),
    "auction": ("treasury securities auction",),
}


def find_series(
    entries: tuple[CatalogEntry, ...],
    query: str,
    *,
    family: str | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    normalized_query = _normalize(query)
    if not normalized_query:
        return []
    query_tokens = set(_tokens(normalized_query))
    alias_tokens: set[str] = set()
    alias_phrases: set[str] = set()
    for token in query_tokens:
        for alias in QUERY_ALIASES.get(token, ()):
            alias_phrases.add(_normalize(alias))
            alias_tokens.update(_tokens(alias))
    alias_tokens.difference_update(query_tokens)

    candidates: list[tuple[float, CatalogEntry, list[str]]] = []
    for entry in entries:
        if family is not None and entry.family != family:
            continue
        score, reasons = _score_entry(
            entry,
            normalized_query,
            query_tokens,
            alias_tokens,
            alias_phrases,
        )
        if score > 0:
            candidates.append((score, entry, reasons))

    candidates.sort(key=lambda item: (-item[0], item[1].id))
    return [_result(score, entry, reasons) for score, entry, reasons in candidates[:limit]]


def _score_entry(
    entry: CatalogEntry,
    query: str,
    query_tokens: set[str],
    alias_tokens: set[str],
    alias_phrases: set[str],
) -> tuple[float, list[str]]:
    fields = {
        "id": _normalize(entry.id),
        "series_id": _normalize(entry.endpoint.series_id or ""),
        "title": _normalize(entry.title),
        "aliases": " | ".join(_normalize(alias) for alias in entry.aliases),
        "tags": " ".join(_normalize(tag) for tag in entry.tags),
        "description": _normalize(entry.description),
        "family": _normalize(entry.family),
    }
    score = 0.0
    reasons: list[str] = []

    exact_checks = [
        ("id", 120),
        ("series_id", 110),
        ("aliases", 95),
        ("title", 80),
    ]
    for field, weight in exact_checks:
        values = fields[field].split(" | ") if field == "aliases" else [fields[field]]
        if query in values:
            score += weight
            reasons.append(f"exact {field}")

    primary_phrase_weights = {
        "id": 55,
        "series_id": 55,
        "aliases": 50,
        "title": 45,
        "tags": 18,
        "description": 12,
    }
    for field, weight in primary_phrase_weights.items():
        if query in fields[field]:
            score += weight
            reasons.append(f"query phrase in {field}")

    alias_phrase_weights = {
        "id": 12,
        "series_id": 12,
        "aliases": 10,
        "title": 9,
        "tags": 4,
        "description": 3,
    }
    for field, weight in alias_phrase_weights.items():
        if any(phrase and phrase in fields[field] for phrase in alias_phrases):
            score += weight
            reasons.append(f"alias phrase in {field}")

    token_weights = {
        "id": 18,
        "series_id": 18,
        "aliases": 14,
        "title": 12,
        "tags": 8,
        "description": 4,
        "family": 3,
    }
    alias_weight_factor = 0.25
    matches_by_field: dict[str, set[str]] = defaultdict(set)
    all_entry_tokens: set[str] = set()
    for field, weight in token_weights.items():
        field_tokens = set(_tokens(fields[field]))
        all_entry_tokens.update(field_tokens)
        primary_matches = query_tokens.intersection(field_tokens)
        secondary_matches = alias_tokens.intersection(field_tokens)
        if primary_matches:
            score += weight * len(primary_matches)
            matches_by_field[field].update(primary_matches)
        if secondary_matches:
            score += weight * alias_weight_factor * len(secondary_matches)

    matched_primary = query_tokens.intersection(all_entry_tokens)
    missing_primary = query_tokens.difference(all_entry_tokens)
    if query_tokens:
        coverage = len(matched_primary) / len(query_tokens)
        score += 30 * coverage
        score -= 18 * len(missing_primary)
        if not missing_primary:
            score += 30
            reasons.append("all query tokens matched")
        elif matched_primary:
            reasons.append(f"missing query tokens: {', '.join(sorted(missing_primary))}")
    if entry.family in query_tokens:
        score += 8
        reasons.append("family boost")

    reasons.extend(
        f"{field} tokens: {', '.join(sorted(tokens))}"
        for field, tokens in sorted(matches_by_field.items())
    )
    return max(score, 0.0), reasons[:6]


def _result(score: float, entry: CatalogEntry, reasons: list[str]) -> dict[str, Any]:
    result = {
        "id": entry.id,
        "title": entry.title,
        "family": entry.family,
        "series_id": entry.endpoint.series_id,
        "score": round(score, 2),
        "units": entry.units.model_dump(mode="json"),
        "frequency": entry.frequency,
        "history_start": entry.history.start.isoformat(),
        "caveat_count": len(entry.caveats),
        "key_requirement": entry.access.auth.model_dump(mode="json"),
        "access_mode": entry.access.mode.value,
        "tools": [tool.value for tool in entry.tools],
        "verified_on": entry.verified_on.isoformat(),
        "vintage_support": vintage_support(entry.access.executor, entry.family),
        "matched_on": reasons,
    }
    if entry.release is not None:
        result["release"] = entry.release.model_dump(mode="json")
    return result


def vintage_support(executor: ExecutorKind, family: str) -> dict[str, Any]:
    if executor == ExecutorKind.FRED:
        return {
            "supported": True,
            "mode": "alfred_as_of",
            "parameter": "vintage_as_of",
            "requires_env": "FRED_API_KEY",
            "coverage": "series-specific; not preflighted",
            "semantics": "information available to ALFRED as of a closed past date",
        }
    return {
        "supported": False,
        "reason": (
            f"{family} does not expose a FRED/ALFRED historical information-date route"
        ),
    }


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return " ".join(TOKEN_RE.findall(decomposed))


def _tokens(value: str) -> list[str]:
    return TOKEN_RE.findall(_normalize(value))
