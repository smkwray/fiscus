from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from fiscus.cache import canonical_params
from fiscus.errors import (
    CacheCollisionError,
    InvalidCredentialError,
    InvalidVintageError,
    MissingKeyError,
    SourceIdentifierNotFoundError,
    SourceRequestError,
    UnsupportedVintageError,
    VintageHistoryAbsentError,
    VintagePrecedesHistoryError,
)
from fiscus.service import FiscusService


def test_keyless_fred_fetch_is_cached_and_returns_provenance(
    tmp_path: Path,
    fixture_dir: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    content = (fixture_dir / "fred_wdtgal.csv").read_bytes()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/graph/fredgraph.csv"
        assert request.url.params["id"] == "WDTGAL"
        assert request.url.params["cosd"] == "2024-01-01"
        return httpx.Response(
            200,
            content=content,
            headers={
                "Content-Type": "text/csv",
                "Last-Modified": "Thu, 09 Jul 2026 20:32:00 GMT",
            },
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        first = service.get_series("fred.wdtgal", start="2024-01-01")
        second = service.get_series("fred.wdtgal", start="2024-01-01")
        payload = service.read_bytes(first["artifact"]["cache_key"])
        full_rows = list(service.iter_csv_rows(first["artifact"]["cache_key"][:20]))

    assert calls == 1
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert first["preview"]["row_count"] == 3
    assert first["preview"]["date_coverage"] == {
        "start": "2024-01-03",
        "end": "2024-01-17",
    }
    assert first["provenance"]["vintage"].startswith("http-last-modified:")
    assert set(first["artifact"]) == {
        "cache_key",
        "filename",
        "mime_type",
        "sha256",
        "byte_count",
    }
    assert first["artifact"]["filename"] == "data.csv"
    assert first["artifact"]["mime_type"] == "text/csv"
    assert first["artifact"]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert first["artifact"]["byte_count"] == len(payload)
    assert first["artifact"]["cache_key"] == first["provenance"]["cache_key"]
    assert "full_data_path" not in first
    assert "manifest_path" not in first
    assert len(full_rows) == 3
    assert {item["id"] for item in first["caveats"]} >= {"fred.wdtgal.wednesday_level"}
    client.close()


def test_keyed_fred_api_redacts_secret_and_keeps_realtime_period_as_metadata(
    tmp_path: Path,
    fixture_dir: Path,
    monkeypatch,
) -> None:
    secret = "a" * 32
    monkeypatch.setenv("FRED_API_KEY", secret)
    payload = (fixture_dir / "fred_wdtgal_api.json").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fred/series/observations"
        assert request.url.params["api_key"] == secret
        return httpx.Response(
            200,
            content=payload,
            headers={"Content-Type": "application/json"},
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        result = service.get_series(
            "fred.wdtgal",
            start="2024-01-01",
            end="2024-01-31",
        )

    assert result["provenance"]["vintage"].startswith("retrieval:")
    assert result["provenance"]["source_metadata"]["response"] == {
        "realtime_start": "2026-07-13",
        "realtime_end": "2026-07-13",
        "observation_start": "2024-01-01",
        "observation_end": "2024-01-31",
    }
    assert secret not in result["provenance"]["source_url"]
    assert "REDACTED" in result["provenance"]["source_url"]
    assert result["preview"]["row_count"] == 2
    client.close()


def test_keyed_fred_same_day_refresh_uses_http_revision_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    secret = "a" * 32
    monkeypatch.setenv("FRED_API_KEY", secret)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        value = "750000" if calls == 1 else "760000"
        payload = (
            "{"
            '"realtime_start":"2026-07-23",'
            '"realtime_end":"2026-07-23",'
            '"observations":['
            '{"realtime_start":"2026-07-23","realtime_end":"2026-07-23",'
            f'"date":"2026-07-22","value":"{value}"'
            "}]"
            "}"
        ).encode()
        return httpx.Response(
            200,
            content=payload,
            headers={"Content-Type": "application/json", "ETag": f'"revision-{calls}"'},
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        first = service.get_series("fred.wdtgal")
        second = service.get_series("fred.wdtgal", refresh=True)

    assert calls == 2
    assert first["provenance"]["vintage"].startswith("http-etag:")
    assert second["provenance"]["vintage"].startswith("http-etag:")
    assert first["provenance"]["vintage"] != second["provenance"]["vintage"]
    assert first["artifact"]["cache_key"] != second["artifact"]["cache_key"]
    assert first["preview"]["head"][0]["value"] == "750000"
    assert second["preview"]["head"][0]["value"] == "760000"
    assert first["provenance"]["source_metadata"]["response"]["realtime_end"] == "2026-07-23"
    assert second["provenance"]["source_metadata"]["response"]["realtime_end"] == "2026-07-23"
    client.close()


def test_current_fred_refuses_silent_page_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRED_API_KEY", "k" * 32)
    payload = {
        "count": 2,
        "observations": [{"date": "2026-07-08", "value": "1"}],
    }
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload, request=request)
    ))
    with (
        FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service,
        pytest.raises(SourceRequestError, match="incomplete observation page"),
    ):
        service.get_series("fred.wdtgal")
    client.close()


ALFRED_HISTORY_ERROR = (
    "Bad Request.  The series does not exist in ALFRED but may exist in FRED.  "
    "Try setting realtime_start and realtime_end to today's date or removing the "
    "realtime_start and realtime_end variables."
)
FRED_SERIES_ABSENT_ERROR = "Bad Request.  The series does not exist."


def _fred_error(message: str, request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        400,
        json={"error_code": 400, "error_message": message},
        request=request,
    )


def _alfred_payload(
    vintage: str,
    observations: list[tuple[str, str]],
    *,
    count: int | None = None,
    offset: int = 0,
    limit: int = 100_000,
) -> bytes:
    return json.dumps(
        {
            "realtime_start": vintage,
            "realtime_end": vintage,
            "observation_start": observations[0][0] if observations else "1900-01-01",
            "observation_end": observations[-1][0] if observations else "1900-01-01",
            "units": "lin",
            "output_type": 1,
            "sort_order": "asc",
            "order_by": "observation_date",
            "count": len(observations) if count is None else count,
            "offset": offset,
            "limit": limit,
            "observations": [
                {
                    "realtime_start": vintage,
                    "realtime_end": vintage,
                    "date": observation_date,
                    "value": value,
                }
                for observation_date, value in observations
            ],
        }
    ).encode()


def test_alfred_two_snapshots_separate_observation_and_information_dates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    secret = "v" * 32
    monkeypatch.setenv("FRED_API_KEY", secret)
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append(params)
        vintage = params["realtime_start"]
        assert params["realtime_end"] == vintage
        assert params["observation_start"] == "2020-01-01"
        assert params["observation_end"] == "2020-12-31"
        assert params["output_type"] == "1"
        assert params["units"] == "lin"
        assert params["sort_order"] == "asc"
        assert params["limit"] == "100000"
        value = "100" if vintage == "2021-01-15" else "110"
        payload = json.loads(_alfred_payload(vintage, [("2020-01-01", value)]))
        payload["observation_start"] = params["observation_start"]
        payload["observation_end"] = params["observation_end"]
        return httpx.Response(
            200,
            json=payload,
            headers={"Content-Type": "application/json"},
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        first = service.get_series(
            "fred.wdtgal",
            start="2020-01-01",
            end="2020-12-31",
            vintage_as_of="2021-01-15",
        )
        second = service.get_series(
            "fred.wdtgal",
            start="2020-01-01",
            end="2020-12-31",
            vintage_as_of="2021-02-15",
        )

    assert len(seen) == 2
    assert first["preview"]["head"][0]["value"] == "100"
    assert second["preview"]["head"][0]["value"] == "110"
    assert first["provenance"]["vintage"] == "alfred-as-of:2021-01-15"
    assert second["provenance"]["vintage"] == "alfred-as-of:2021-02-15"
    assert first["artifact"]["cache_key"] != second["artifact"]["cache_key"]
    metadata = first["provenance"]["source_metadata"]
    assert metadata["mode"] == "alfred_as_of"
    assert metadata["requested_observation_bounds"] == {
        "start": "2020-01-01",
        "end": "2020-12-31",
    }
    assert metadata["requested_vintage_as_of"] == "2021-01-15"
    assert "api_key" not in metadata["request"]
    assert secret not in first["provenance"]["source_url"]
    assert any(item["id"] == "fred.alfred_information_snapshot" for item in first["caveats"])
    client.close()


def test_alfred_requires_key_and_rejects_non_fred_without_network(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError(f"unexpected network request: {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        with pytest.raises(MissingKeyError, match="FRED_API_KEY is unset") as absent:
            service.get_series("fred.wdtgal", vintage_as_of="2020-01-15")
        assert absent.value.code == "missing_key"

        monkeypatch.setenv("FRED_API_KEY", "")
        with pytest.raises(MissingKeyError, match="FRED_API_KEY is unset") as empty:
            service.get_series("fred.wdtgal", vintage_as_of="2020-01-15")
        assert empty.value.code == "missing_key"

        with pytest.raises(UnsupportedVintageError):
            service.get_series("bls.lns14000000", vintage_as_of="2020-01-15")

    assert calls == 0
    client.close()


@pytest.mark.parametrize(
    "message",
    [
        (
            "Bad Request.  The value for variable api_key is not registered.  "
            "Read https://fred.stlouisfed.org/docs/api/api_key.html for more information."
        ),
        (
            "Bad Request.  The value for variable api_key is not a 32 character "
            "alpha-numeric lower-case string.  Read "
            "https://fred.stlouisfed.org/docs/api/api_key.html for more information."
        ),
    ],
)
def test_alfred_rejected_key_is_a_credential_fault(
    message: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    secret = "invalid-fred-key"
    monkeypatch.setenv("FRED_API_KEY", secret)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fred/series/observations"
        return _fred_error(message, request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with (
        FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service,
        pytest.raises(InvalidCredentialError, match="FRED_API_KEY") as captured,
    ):
        service.get_series("fred.wdtgal", vintage_as_of="2020-01-15")

    assert captured.value.code == "credential_rejected"
    assert "not registered or invalid" in str(captured.value)
    assert secret not in str(captured.value)
    client.close()


def test_alfred_series_with_no_realtime_history_is_distinct(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FRED_API_KEY", "valid-fred-key")
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/fred/series/observations":
            return _fred_error(ALFRED_HISTORY_ERROR, request)
        if request.url.path == "/fred/series/vintagedates":
            return _fred_error(ALFRED_HISTORY_ERROR, request)
        assert request.url.path == "/fred/series"
        return httpx.Response(200, json={"seriess": [{"id": "WDTGAL"}]}, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with (
        FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service,
        pytest.raises(
            VintageHistoryAbsentError,
            match="no ALFRED realtime history",
        ) as captured,
    ):
        service.get_series("fred.wdtgal", vintage_as_of="2020-01-15")

    assert captured.value.code == "vintage_history_absent"
    assert paths == [
        "/fred/series/observations",
        "/fred/series/vintagedates",
        "/fred/series",
    ]
    client.close()


def test_alfred_vintage_before_first_realtime_date_names_boundary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FRED_API_KEY", "valid-fred-key")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fred/series/observations":
            return _fred_error(ALFRED_HISTORY_ERROR, request)
        assert request.url.path == "/fred/series/vintagedates"
        return httpx.Response(
            200,
            json={"count": 2, "vintage_dates": ["2019-03-07", "2020-03-06"]},
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with (
        FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service,
        pytest.raises(VintagePrecedesHistoryError, match="2019-03-07") as captured,
    ):
        service.get_series("fred.wdtgal", vintage_as_of="2018-01-01")

    assert captured.value.code == "vintage_precedes_history"
    client.close()


def test_alfred_nonexistent_identifier_names_z1_prefix_hazard(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FRED_API_KEY", "valid-fred-key")
    requested_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_ids.append(request.url.params["series_id"])
        if request.url.path == "/fred/series/observations":
            return _fred_error(ALFRED_HISTORY_ERROR, request)
        if request.url.path == "/fred/series/vintagedates":
            return _fred_error(FRED_SERIES_ABSENT_ERROR, request)
        assert request.url.path == "/fred/series"
        return _fred_error(FRED_SERIES_ABSENT_ERROR, request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with (
        FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service,
        pytest.raises(SourceIdentifierNotFoundError, match="wrong") as captured,
    ):
        service.get_series("fred.bogz1fl153061105q", vintage_as_of="2019-06-06")

    assert captured.value.code == "source_identifier_absent"
    assert "two-letter prefix encodes the economic concept" in str(captured.value)
    assert "FL, LM, FA, and FU are distinct concepts" in str(captured.value)
    assert "did not guess or retry a different prefix" in str(captured.value)
    assert FRED_SERIES_ABSENT_ERROR in str(captured.value)
    assert set(requested_ids) == {"BOGZ1FL153061105Q"}
    client.close()


def test_alfred_unexplained_error_after_history_start_preserves_upstream_detail(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FRED_API_KEY", "valid-fred-key")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fred/series/observations":
            return _fred_error(ALFRED_HISTORY_ERROR, request)
        assert request.url.path == "/fred/series/vintagedates"
        return httpx.Response(
            200,
            json={"count": 1, "vintage_dates": ["2019-03-07"]},
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with (
        FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service,
        pytest.raises(SourceRequestError, match="does not exist in ALFRED") as captured,
    ):
        service.get_series("fred.wdtgal", vintage_as_of="2020-01-15")

    assert captured.value.code == "source_request_failed"
    assert ALFRED_HISTORY_ERROR in str(captured.value)
    client.close()


def test_alfred_rejects_unclosed_or_malformed_date_before_network(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FRED_API_KEY", "v" * 32)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError(f"unexpected network request: {request.url}")

    today = datetime.now(UTC).date()
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        for invalid in (
            "not-a-date",
            today.isoformat(),
            (today + timedelta(days=1)).isoformat(),
        ):
            with pytest.raises(InvalidVintageError):
                service.get_series("fred.wdtgal", vintage_as_of=invalid)

    assert calls == 0
    client.close()


def test_alfred_future_leak_fails_without_cache_write(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FRED_API_KEY", "v" * 32)
    vintage = "2020-01-15"
    payload = json.loads(_alfred_payload(vintage, [("2019-01-01", "1")]))
    payload["observations"][0]["realtime_end"] = "2020-02-01"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        with pytest.raises(SourceRequestError, match="row real-time bounds"):
            service.get_series("fred.wdtgal", vintage_as_of=vintage)
        params = canonical_params(
            {
                "start": None,
                "end": None,
                "source_mode": "alfred_api_json",
                "vintage_as_of": vintage,
            }
        )
        assert service.cache.load_latest("fred.wdtgal", params) is None

    client.close()


def test_alfred_paginates_contiguously_and_accepts_empty_history(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FRED_API_KEY", "v" * 32)
    monkeypatch.setattr("fiscus.executors.fred.ALFRED_PAGE_LIMIT", 2)
    offsets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        vintage = request.url.params["realtime_start"]
        offset = int(request.url.params["offset"])
        offsets.append(offset)
        if vintage == "2020-01-15":
            page = (
                [("2018-01-01", "1"), ("2019-01-01", "2")] if offset == 0 else [("2020-01-01", "3")]
            )
            return httpx.Response(
                200,
                content=_alfred_payload(vintage, page, count=3, offset=offset, limit=2),
                request=request,
            )
        return httpx.Response(
            200,
            content=_alfred_payload(vintage, [], count=0, offset=0, limit=2),
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        paged = service.get_series("fred.wdtgal", vintage_as_of="2020-01-15")
        empty = service.get_series("fred.wdtgal", vintage_as_of="1900-01-15")

    assert offsets == [0, 2, 0]
    assert paged["preview"]["row_count"] == 3
    assert paged["provenance"]["source_metadata"]["pagination"] == {
        "count": 3,
        "page_count": 2,
        "offsets_fetched": [0, 2],
    }
    assert empty["preview"]["row_count"] == 0
    assert empty["provenance"]["vintage"] == "alfred-as-of:1900-01-15"
    client.close()


def test_alfred_same_information_date_changed_bytes_exposes_correction(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FRED_API_KEY", "v" * 32)
    calls = 0
    vintage = "2020-01-15"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            content=_alfred_payload(vintage, [("2019-01-01", str(calls))]),
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        first = service.get_series("fred.wdtgal", vintage_as_of=vintage)
        with pytest.raises(CacheCollisionError):
            service.get_series("fred.wdtgal", vintage_as_of=vintage, refresh=True)

    assert first["provenance"]["vintage"] == f"alfred-as-of:{vintage}"
    assert calls == 2
    client.close()
