from __future__ import annotations

import traceback

import httpx
import pytest

from fiscus.errors import (
    SourceAccessDeniedError,
    SourceRemovedError,
    SourceRequestError,
)
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


@pytest.mark.parametrize(
    ("status", "expected_type", "code"),
    [
        (404, SourceRemovedError, "source_removed"),
        (410, SourceRemovedError, "source_removed"),
        (403, SourceAccessDeniedError, "source_access_denied"),
        (451, SourceAccessDeniedError, "source_access_denied"),
    ],
)
def test_source_statuses_distinguish_removal_from_access_denial(
    status: int,
    expected_type: type[SourceRequestError],
    code: str,
) -> None:
    secret = "private-token"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="source response", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    requester = HttpRequester(client, sleep=lambda _: None)
    with pytest.raises(expected_type) as captured:
        requester.get(
            "https://example.test/archive.csv",
            params={"token": secret},
            policy=RetryPolicy(attempts=1, politeness_delay_seconds=0),
        )

    assert isinstance(captured.value, SourceRequestError)
    assert captured.value.code == code
    assert f"HTTP {status}" in str(captured.value)
    assert secret not in str(captured.value)
    assert "REDACTED" in str(captured.value)
    client.close()


@pytest.mark.parametrize(
    ("status", "expected_type"),
    [
        (400, SourceRequestError),
        (403, SourceAccessDeniedError),
        (404, SourceRemovedError),
        (410, SourceRemovedError),
        (451, SourceAccessDeniedError),
    ],
)
@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("text/plain", "reflected {url}"),
        ("application/json", '{"error": "reflected {url}"}'),
    ],
)
def test_credentialed_http_error_omits_reflected_body_and_cause(
    status: int,
    expected_type: type[SourceRequestError],
    content_type: str,
    body: str,
) -> None:
    secret = "sensitive custom value/with spaces"

    def handler(request: httpx.Request) -> httpx.Response:
        reflected = body.replace("{url}", str(request.url))
        return httpx.Response(
            status,
            text=reflected,
            headers={"content-type": content_type},
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    requester = HttpRequester(client, sleep=lambda _: None)
    with pytest.raises(expected_type) as captured:
        requester.get(
            "https://example.test/data",
            params={"customCredential": secret},
            secret_params={"customCredential"},
            policy=RetryPolicy(attempts=1, politeness_delay_seconds=0),
        )

    rendered = "".join(
        traceback.format_exception(
            type(captured.value),
            captured.value,
            captured.value.__traceback__,
        )
    )
    assert secret not in str(captured.value)
    assert secret not in rendered
    assert "sensitive%20custom" not in rendered
    assert "reflected" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert "customCredential=REDACTED" in str(captured.value)
    client.close()


def test_noncredentialed_http_error_keeps_bounded_body_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            text="public diagnostic",
            headers={"content-type": "text/plain"},
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    requester = HttpRequester(client, sleep=lambda _: None)
    with pytest.raises(SourceRequestError, match="public diagnostic"):
        requester.get(
            "https://example.test/data",
            policy=RetryPolicy(attempts=1, politeness_delay_seconds=0),
        )
    client.close()


@pytest.mark.parametrize("credential_location", ["query", "userinfo", "header", "client-auth"])
def test_credentials_outside_params_do_not_leak_in_http_errors(
    credential_location: str,
) -> None:
    secret = "sensitive-secret-value"
    url = "https://example.test/data"
    client_options: dict = {}
    if credential_location == "query":
        url += f"?api_key={secret}"
    elif credential_location == "userinfo":
        url = f"https://user:{secret}@example.test/data"
    elif credential_location == "header":
        client_options["headers"] = {"Authorization": f"Bearer {secret}"}
    else:
        client_options["auth"] = ("user", secret)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            text=f"reflected {secret} {request.url} {request.headers.get('authorization', '')}",
            headers={"content-type": "text/plain"},
            request=request,
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler), **client_options) as client,
        pytest.raises(SourceRequestError) as captured,
    ):
        HttpRequester(client, sleep=lambda _: None).get(url)
    rendered = "".join(traceback.format_exception(captured.value))
    assert secret not in rendered
    assert "reflected" not in rendered
    assert captured.value.__cause__ is None


def test_url_userinfo_is_redacted_on_transport_failure() -> None:
    secret = "sensitive-secret-value"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed for {request.url}", request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(SourceRequestError) as captured,
    ):
        HttpRequester(client, sleep=lambda _: None).get(
            f"https://user:{secret}@example.test/data",
            policy=RetryPolicy(attempts=1, politeness_delay_seconds=0),
        )
    assert secret not in "".join(traceback.format_exception(captured.value))
    assert "REDACTED" in str(captured.value)


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
