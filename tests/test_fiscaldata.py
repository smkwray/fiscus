from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from fiscus.errors import SourceRequestError
from fiscus.service import FiscusService, infer_filter_date_range


def test_fiscaldata_paginates_and_attaches_date_seam_caveat(
    tmp_path: Path,
    fixture_dir: Path,
) -> None:
    page_one = (fixture_dir / "fiscaldata_dts_page1.json").read_bytes()
    page_two = (fixture_dir / "fiscaldata_dts_page2.json").read_bytes()
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page[number]"])
        pages.append(page)
        content = page_one if page == 1 else page_two
        return httpx.Response(
            200,
            content=content,
            headers={
                "Content-Type": "application/json",
                "Last-Modified": "Mon, 06 Apr 2022 12:00:00 GMT",
            },
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        result = service.fiscaldata_query(
            "fiscaldata.dts_operating_cash_balance",
            params={
                "filter": "record_date:gte:2022-03-31,record_date:lte:2022-04-04",
                "page[size]": "2",
            },
            max_pages=2,
        )

    assert pages == [1, 2]
    assert result["preview"]["row_count"] == 3
    assert result["preview"]["date_coverage"] == {
        "start": "2022-03-31",
        "end": "2022-04-04",
    }
    assert "fiscaldata.dts.schema_seam_2022_04" in {item["id"] for item in result["caveats"]}
    assert result["provenance"]["source_metadata"]["pages_fetched"] == 2
    assert result["artifact"]["mime_type"] == "text/csv"
    assert result["artifact"]["cache_key"] == result["provenance"]["cache_key"]
    assert "full_data_path" not in result
    assert "manifest_path" not in result
    client.close()


def test_fiscaldata_refuses_silent_page_truncation(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = {
            "data": [{"record_date": "2024-01-02", "value": "1"}],
            "meta": {"total-pages": 3},
            "links": {"next": "next"},
        }
        return httpx.Response(200, json=payload, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with (
        FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service,
        pytest.raises(SourceRequestError, match="narrow the filter"),
    ):
        service.fiscaldata_query(
            "fiscaldata.dts_operating_cash_balance",
            params={"page[size]": "1"},
            max_pages=2,
            refresh=True,
        )
    client.close()


def test_fiscaldata_refuses_unknown_total_with_next_link(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = {
            "data": [{"record_date": "2024-01-02", "value": "1"}],
            "meta": {},
            "links": {"next": "next"},
        }
        return httpx.Response(200, json=payload, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with (
        FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service,
        pytest.raises(SourceRequestError, match="more pages"),
    ):
        service.fiscaldata_query(
            "fiscaldata.dts_operating_cash_balance",
            params={"page[size]": "1"},
            max_pages=1,
            refresh=True,
        )
    client.close()


def test_fiscaldata_rejects_inverted_date_filter_before_fetch() -> None:
    with pytest.raises(ValueError, match="upper bound precedes lower bound"):
        infer_filter_date_range(
            "record_date:gte:2024-02-01,record_date:lte:2024-01-01",
            ["record_date"],
        )
