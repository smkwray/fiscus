from __future__ import annotations

import httpx
import pytest

from fiscus.errors import SourceRequestError
from fiscus.http import HttpRequester, RetryPolicy


def test_request_error_redacts_query_secret() -> None:
    secret = "sensitive-fred-key"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed to connect to {request.url}", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    requester = HttpRequester(client, sleep=lambda _: None)
    with pytest.raises(SourceRequestError) as captured:
        requester.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={"series_id": "WDTGAL", "api_key": secret},
            policy=RetryPolicy(attempts=1, politeness_delay_seconds=0),
        )
    message = str(captured.value)
    assert secret not in message
    assert "REDACTED" in message
    client.close()


def test_default_user_agent_carries_plus_url_comment() -> None:
    """The FRED edge silently stalls user agents without a parenthetical
    comment containing a ``+<url>`` token (``fiscus/0.1.0`` and even
    ``fiscus/0.1.0 (public data client)`` time out; adding ``+https://...``
    inside the parens is served). A regression here breaks every keyless
    FRED fetch."""
    from fiscus.service import FiscusService

    service = FiscusService()
    try:
        user_agent = service.http_client.headers["User-Agent"]
        assert user_agent.startswith("fiscus/")
        assert "(+http" in user_agent and user_agent.endswith(")")
    finally:
        service.close()


def test_user_agent_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deployments identify themselves via FISCUS_USER_AGENT instead of
    editing source; personal or contact details never belong in code."""
    from fiscus.service import default_user_agent

    monkeypatch.setenv("FISCUS_USER_AGENT", "myproject/1.0 (+https://example.org)")
    assert default_user_agent() == "myproject/1.0 (+https://example.org)"
