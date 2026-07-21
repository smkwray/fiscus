from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from fiscus.errors import CacheError
from fiscus.service import FiscusService


def test_provenance_resolves_immutable_artifact_by_key_and_prefix(
    tmp_path: Path,
    fixture_dir: Path,
    monkeypatch,
) -> None:
    """provenance() must resolve only immutable artifact identities: the full
    cache key or a unique prefix, never a mutable dataset alias."""
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    content = (fixture_dir / "fred_wdtgal.csv").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        fetched = service.get_series("fred.wdtgal", start="2024-01-01")
        key = fetched["provenance"]["cache_key"]

        by_full_key = service.provenance(key)
        by_prefix = service.provenance(key[:16])
        assert by_full_key["manifest"]["cache_key"] == key
        assert by_prefix["manifest"]["cache_key"] == key
        assert by_full_key["artifact"] == fetched["artifact"]
        assert by_prefix["artifact"] == fetched["artifact"]
        assert by_full_key["sha256_verified"] is True
        assert "full_data_path" not in by_full_key
        assert "manifest_path" not in by_full_key

        with pytest.raises(CacheError, match="at least 12 hex"):
            service.provenance("fred.wdtgal")
        with pytest.raises(CacheError, match="no cached artifact"):
            service.provenance("0" * 64)
    client.close()
