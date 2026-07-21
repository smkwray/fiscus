from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from fiscus.models import CatalogEntry, Caveat, CaveatPredicate, CaveatWhen


@dataclass(frozen=True)
class CaveatContext:
    start: date | None = None
    end: date | None = None
    params: Mapping[str, str] = field(default_factory=dict)
    unit_classes: frozenset[str] = field(default_factory=frozenset)
    kind: str | None = None


def parse_date(value: str | date | None) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(value)


def caveat_binding(caveat: Caveat, context: CaveatContext) -> bool | None:
    return _when_matches(caveat.when, context)


def binding_caveats(entry: CatalogEntry, context: CaveatContext) -> list[dict[str, Any]]:
    bound: list[dict[str, Any]] = []
    for caveat in entry.caveats:
        binding = caveat_binding(caveat, context)
        if binding is True:
            bound.append(_serialize(caveat, binding))
    return bound


def all_caveats(entry: CatalogEntry, context: CaveatContext | None = None) -> list[dict[str, Any]]:
    effective_context = context or CaveatContext()
    return [
        _serialize(caveat, caveat_binding(caveat, effective_context)) for caveat in entry.caveats
    ]


def _serialize(caveat: Caveat, binding: bool | None) -> dict[str, Any]:
    return {
        "id": caveat.id,
        "severity": caveat.severity.value,
        "message": caveat.message,
        "binds": binding,
        "when": caveat.when.model_dump(mode="json"),
        "source_note": caveat.source_note,
    }


def _when_matches(rule: CaveatWhen, context: CaveatContext) -> bool | None:
    values = [_predicate_matches(predicate, context) for predicate in rule.predicates]
    if rule.match == "all":
        if any(value is False for value in values):
            return False
        if all(value is True for value in values):
            return True
        return None
    if any(value is True for value in values):
        return True
    if all(value is False for value in values):
        return False
    return None


def _predicate_matches(predicate: CaveatPredicate, context: CaveatContext) -> bool | None:
    if predicate.type == "always":
        return True

    if predicate.type in {"date_range_intersects", "date_range_crosses"}:
        if context.start is None and context.end is None:
            return None
        request_start = context.start or date.min
        request_end = context.end or date.max
        if predicate.type == "date_range_crosses":
            return request_start < predicate.boundary <= request_end
        condition_start = predicate.start
        condition_end = predicate.end or date.max
        return request_start <= condition_end and request_end >= condition_start

    if predicate.type == "param_equals":
        if predicate.param not in context.params:
            return None
        return str(context.params[predicate.param]) == predicate.value

    if predicate.type == "param_in":
        if predicate.param not in context.params:
            return None
        return str(context.params[predicate.param]) in set(predicate.values)

    if predicate.type == "unit_mixing":
        if not context.unit_classes:
            return None
        return bool(context.unit_classes.intersection(predicate.with_unit_classes))

    if predicate.type == "kind_in":
        if context.kind is None:
            return None
        return context.kind in set(predicate.kinds)

    raise AssertionError(f"unhandled caveat predicate: {predicate}")
