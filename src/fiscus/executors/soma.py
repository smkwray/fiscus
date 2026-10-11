from __future__ import annotations

from datetime import UTC, date, datetime

from fiscus.errors import SourceRequestError
from fiscus.executors.base import FetchResult
from fiscus.http import HttpRequester, RetryPolicy, redact_url, selected_http_metadata
from fiscus.models import CatalogEntry

SOMA_POLICY = RetryPolicy(attempts=3, backoff_seconds=0.75, politeness_delay_seconds=0.25)

ASOF_DATES_URL = "https://markets.newyorkfed.org/api/soma/asofdates/list.json"


class SomaExecutor:
    def __init__(self, requester: HttpRequester) -> None:
        self.requester = requester

    def latest_asof(self) -> str:
        response = self.requester.get(ASOF_DATES_URL, policy=SOMA_POLICY)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceRequestError("SOMA as-of date list returned invalid JSON") from exc
        soma = payload.get("soma") if isinstance(payload, dict) else None
        dates = soma.get("asOfDates") if isinstance(soma, dict) else None
        if not isinstance(dates, list) or not dates:
            raise SourceRequestError("SOMA as-of date list is empty or malformed")
        try:
            if any(not isinstance(value, str) for value in dates):
                raise ValueError("as-of date is not a string")
            parsed = [date.fromisoformat(value) for value in dates]
            if any(
                value != observed.isoformat()
                for value, observed in zip(dates, parsed, strict=True)
            ):
                raise ValueError("as-of date is not an ISO calendar date")
        except ValueError as exc:
            raise SourceRequestError("SOMA as-of date list contains a malformed date") from exc
        return max(parsed).isoformat()

    def fetch(
        self,
        entry: CatalogEntry,
        *,
        asof: str,
        cusip: str | None = None,
    ) -> FetchResult:
        retrieved_at = datetime.now(UTC).isoformat()
        date.fromisoformat(asof)
        url = f"{entry.endpoint.url}{asof}.json"
        response = self.requester.get(url, policy=SOMA_POLICY)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceRequestError(f"SOMA returned invalid JSON for as-of {asof}") from exc
        soma = payload.get("soma") if isinstance(payload, dict) else None
        holdings = soma.get("holdings") if isinstance(soma, dict) else None
        if not isinstance(holdings, list):
            raise SourceRequestError(f"SOMA response omitted holdings for as-of {asof}")
        if any(not isinstance(holding, dict) for holding in holdings):
            raise SourceRequestError(f"SOMA returned a malformed holding for as-of {asof}")
        if any(holding.get("asOfDate") != asof for holding in holdings):
            raise SourceRequestError(f"SOMA holding dates differ from requested as-of {asof}")
        rows = [
            {str(key): "" if value is None else str(value) for key, value in holding.items()}
            for holding in holdings
        ]
        if cusip:
            wanted = cusip.strip().casefold()
            rows = [row for row in rows if row.get("cusip", "").casefold() == wanted]
        return FetchResult(
            rows=rows,
            source_url=redact_url(str(response.request.url)),
            retrieved_at_utc=retrieved_at,
            # A published weekly snapshot is the release identity; holdings
            # for a given as-of date do not change after publication.
            vintage=f"soma-asof:{asof}",
            source_metadata={
                "mode": "soma_asof_snapshot",
                "asof": asof,
                "cusip_filter": cusip or "",
                "http": selected_http_metadata(response.headers),
            },
        )
