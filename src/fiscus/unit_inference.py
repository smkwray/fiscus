from __future__ import annotations

import re

from fiscus.models import Caveat, Severity, UnitSpec

_ANNUALIZATION_PATTERNS = (
    re.compile(r"\bannual[- ]rates?\b", re.IGNORECASE),
    re.compile(r"\bannualized\b", re.IGNORECASE),
    re.compile(r"\bannualised\b", re.IGNORECASE),
    re.compile(r"\bper annum\b", re.IGNORECASE),
    re.compile(r"\bper year\b", re.IGNORECASE),
    re.compile(r"\bsaar\b", re.IGNORECASE),
)

_RATE_LIKE_UNIT_MARKERS = (
    "percent",
    "percentage",
    "basis point",
    "yield",
    "rate of change",
    "growth rate",
    "index",
)

_FLOW_LEVEL_UNIT_MARKERS = (
    "dollar",
    "person",
    "people",
    "job",
    "employee",
    "worker",
    "unit",
    "vehicle",
    "barrel",
    "ton",
    "hour",
    "bushel",
    "kilowatt",
)


def infer_passthrough_units(
    *,
    label: str | None,
    seasonal_adjustment: str | None,
    frequency: str | None,
    unknown_label: str,
) -> tuple[UnitSpec, Caveat | None]:
    """Derive conservative unit labels for an uncataloged source series.

    FRED's keyed series endpoint supplies units and seasonal-adjustment metadata. The
    keyless graph CSV and the BLS data envelope do not, so those paths report an
    explicit unknown instead of treating missing metadata as not annualized.
    """
    clean_label = (label or "").strip()
    clean_seasonal = (seasonal_adjustment or "").strip()
    annualized = _metadata_annualization(clean_label, clean_seasonal)
    derived_seasonal = clean_seasonal or _seasonal_adjustment_from_label(clean_label)
    scale = _unit_scale(clean_label)
    flow_level = _is_flow_level_unit(clean_label)
    unit_class = _passthrough_unit_class(
        clean_label,
        seasonal_adjustment=derived_seasonal,
        annualized=annualized,
        flow_level=flow_level,
    )
    units = UnitSpec(
        label=clean_label or unknown_label,
        scale=scale,
        seasonal_adjustment=derived_seasonal or None,
        annualized=annualized,
        unit_class=unit_class,
    )
    if annualized is None:
        return units, Caveat(
            id="fiscus.annualization_unknown",
            severity=Severity.WARNING,
            message=(
                "This passthrough route supplied insufficient series-level unit metadata to "
                "determine annualization. Verify the source before treating an observation as "
                "a period flow. If it is an annualized flow level, divide by 4 for quarterly "
                "data or 12 for monthly data (or by the number of source periods per year); "
                "fiscus does not transform values."
            ),
            source_note=(
                "The keyless FRED graph CSV and BLS data response do not carry complete "
                "series-level unit metadata."
            ),
        )
    if annualized is False:
        return units, None
    return units, _detected_annualization_caveat(frequency, flow_level=flow_level)


def _metadata_annualization(label: str, seasonal_adjustment: str) -> bool | None:
    metadata_text = " ".join(part for part in (label, seasonal_adjustment) if part)
    normalized = metadata_text.casefold()
    if "not annualized" in normalized or "not annualised" in normalized:
        return False
    if any(pattern.search(metadata_text) for pattern in _ANNUALIZATION_PATTERNS):
        return True
    # FRED's units label is the authoritative statement of the measurement unit. When it is
    # present and contains no annual-rate marker, report not annualized. A seasonality label
    # alone cannot establish that fact, so keep the signal unknown instead of guessing.
    if label:
        return False
    return None


def _seasonal_adjustment_from_label(label: str) -> str:
    normalized = label.casefold()
    if "not seasonally adjusted" in normalized:
        return "not seasonally adjusted"
    if "seasonally adjusted annual rate" in normalized:
        return "seasonally adjusted annual rate"
    if "seasonally adjusted" in normalized:
        return "seasonally adjusted"
    return ""


def _unit_scale(label: str) -> str | None:
    normalized = label.casefold()
    for singular in ("trillion", "billion", "million", "thousand"):
        if re.search(rf"\b{singular}s?\b", normalized):
            return f"{singular}s"
    return None


def _is_flow_level_unit(label: str) -> bool:
    normalized = label.casefold()
    if any(marker in normalized for marker in _RATE_LIKE_UNIT_MARKERS):
        return False
    if (
        re.search(r"\bper\b", normalized)
        and "per annum" not in normalized
        and "per year" not in normalized
    ):
        return False
    return _unit_scale(label) is not None or any(
        marker in normalized for marker in _FLOW_LEVEL_UNIT_MARKERS
    )


def _passthrough_unit_class(
    label: str,
    *,
    seasonal_adjustment: str,
    annualized: bool | None,
    flow_level: bool,
) -> str:
    if annualized is None and not label:
        return "uncataloged_annualization_unknown"

    normalized = label.casefold()
    scale = _unit_scale(label)
    if any(marker in normalized for marker in ("percent", "percentage")):
        base = "percent"
    elif "basis point" in normalized:
        base = "basis_points"
    elif "u.s. dollar" in normalized or "us dollar" in normalized:
        base = "usd" + (f"_{scale}" if scale else "")
    elif "dollar" in normalized:
        base = "currency" + (f"_{scale}" if scale else "")
    elif "person" in normalized or "people" in normalized:
        base = "persons" + (f"_{scale}" if scale else "")
    elif "index" in normalized:
        base = "index"
    else:
        base = "source_units" + (f"_{scale}" if scale else "")

    seasonal = seasonal_adjustment.casefold()
    seasonally_adjusted = (
        "seasonally adjusted" in seasonal and "not seasonally adjusted" not in seasonal
    )
    if annualized is None:
        suffix = "annualization_unknown"
    elif annualized:
        suffix = "saar" if flow_level and seasonally_adjusted else "annualized"
        if not flow_level:
            suffix = "annual_rate"
    elif "not seasonally adjusted" in seasonal:
        suffix = "nsa"
    elif "seasonally adjusted" in seasonal:
        suffix = "sa"
    else:
        suffix = "not_annualized"
    return f"{base}_{suffix}"


def _detected_annualization_caveat(
    frequency: str | None,
    *,
    flow_level: bool,
) -> Caveat:
    period, factor = _period_divisor(frequency)
    if flow_level and factor:
        instruction = (
            f"Divide by {factor} to obtain the corresponding {period} flow before combining "
            "with nonannualized amounts."
        )
        caveat_id = "fiscus.annualized_flow"
    elif flow_level:
        instruction = (
            "Divide by the number of source periods per year (4 for quarterly data, 12 for "
            "monthly data) to obtain a period flow."
        )
        caveat_id = "fiscus.annualized_flow"
    else:
        instruction = (
            "For annualized flow levels, the period-flow conversion is division by the number "
            "of periods per year (4 quarterly, 12 monthly). This series appears rate- or "
            "index-like, so use the source-defined deannualization rather than blindly applying "
            "that flow conversion."
        )
        caveat_id = "fiscus.annualized_nonflow"
    return Caveat(
        id=caveat_id,
        severity=Severity.CRITICAL,
        message=(
            "Source metadata marks this uncataloged series as an annual rate. An observation "
            "is not automatically the amount or rate for one source period. "
            f"{instruction} fiscus returns the source values without transforming them."
        ),
        source_note=(
            "Derived at request time from source-supplied units, seasonal-adjustment, and "
            "frequency metadata."
        ),
    )


def _period_divisor(frequency: str | None) -> tuple[str, int | None]:
    normalized = (frequency or "").casefold()
    if "monthly" in normalized:
        return "monthly", 12
    if "quarterly" in normalized:
        return "quarterly", 4
    if "semiannual" in normalized or "semi-annual" in normalized:
        return "semiannual", 2
    return "period", None
