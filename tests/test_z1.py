from __future__ import annotations

import hashlib
import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import httpx
import pytest

from fiscus.errors import DatasetNotFoundError, SourceSchemaChangedError
from fiscus.executors.z1 import Z1Executor
from fiscus.http import HttpRequester
from fiscus.models import Z1_KIND_CODES
from fiscus.service import FiscusService, normalize_z1_code

EXPECTED_Z1_MEANINGS = {
    "FA": "a seasonally adjusted annual-rate transaction flow",
    "FC": "the change in an unadjusted level",
    "FG": "a seasonally adjusted growth rate",
    "FI": "an index",
    "FL": "a level at period end, not seasonally adjusted",
    "FR": "a revaluation (holding gain or loss)",
    "FS": "a seasonal factor",
    "FU": "a transaction flow, not seasonally adjusted",
    "FV": "an other change in volume",
    "LA": "a seasonally adjusted level",
    "LM": "a market-value or current-cost level, not seasonally adjusted",
    "PC": "a percent change in an index",
}

EXPECTED_Z1_CAVEAT_IDS = {
    "FA": "z1.fa.saar_transaction",
    "FC": "z1.fc.change_not_transaction",
    "FG": "z1.fg.growth_rate",
    "FI": "z1.fi.index",
    "FL": "z1.fl.level_not_flow",
    "FR": "z1.fr.revaluation_not_transaction",
    "FS": "z1.fs.seasonal_factor",
    "FU": "z1.fu.transaction_not_level",
    "FV": "z1.fv.other_volume_change",
    "LA": "z1.la.level_not_flow",
    "LM": "z1.lm.valuation_effects",
    "PC": "z1.pc.index_percent_change",
}


def _package_bytes(*, final_period: str = "2026:Q1") -> bytes:
    # ZipInfo uses a fixed 1980 timestamp; fixture bytes must not depend on the clock.
    dictionary = (
        "FL313020005.Q\tFederal government; checkable deposits; asset\tLine 25\t"
        "F2.2.s Checkable deposits and currency\t"
        "Millions of dollars; amounts outstanding end of period, not seasonally adjusted\n"
        "FU313020005.Q\tFederal government; checkable deposits; asset\tLine 25\t"
        "F2.2.s Checkable deposits and currency\t"
        "Millions of dollars; quarterly figures, not seasonally adjusted\n"
        "FL999000000.Q\tExample uncataloged series\tLine 1\tF9.9 Example\t"
        "Millions of dollars; amounts outstanding end of period, not seasonally adjusted\n"
    )
    table = (
        "date,FL313020005.Q,FU313020005.Q,FL999000000.Q\n"
        "1945:Q4,26937,ND,10\n"
        "2025:Q4,830000,-1200,20\n"
        f"{final_period},896092,18306,30\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(zipfile.ZipInfo("data_dictionary/F2_2_s.txt"), dictionary)
        archive.writestr(zipfile.ZipInfo("csv/F2_2_s.csv"), table)
    return buffer.getvalue()


def _accumulation_package_bytes() -> bytes:
    dictionary = (
        "FC152090005.Q\tHouseholds and nonprofit organizations; net worth\tLine 1\t"
        "S1M.r Households and nonprofit organizations\t"
        "Millions of dollars; change in unadjusted amounts outstanding end of period\n"
        "FU152090005.Q\tHouseholds and nonprofit organizations; net worth\tLine 2\t"
        "S1M.r Households and nonprofit organizations\t"
        "Millions of dollars; transactions, not seasonally adjusted\n"
        "FR152090005.Q\tHouseholds and nonprofit organizations; net worth\tLine 11\t"
        "S1M.r Households and nonprofit organizations\tMillions of dollars; revaluation\n"
        "FV152090005.Q\tHouseholds and nonprofit organizations; net worth\tLine 26\t"
        "S1M.r Households and nonprofit organizations\t"
        "Millions of dollars; other changes in volume of assets\n"
        "LM152090005.Q\tHouseholds and nonprofit organizations; net worth\tLine 27\t"
        "S1M.r Households and nonprofit organizations\t"
        "Millions of dollars; amounts outstanding end of period, market value, "
        "not seasonally adjusted\n"
    )
    table = (
        "date,FC152090005.Q,FU152090005.Q,FR152090005.Q,FV152090005.Q,LM152090005.Q\n"
        "2025:Q3,ND,ND,ND,ND,1000\n"
        "2025:Q4,100,20,70,10,1100\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(zipfile.ZipInfo("data_dictionary/S1M_r.txt"), dictionary)
        archive.writestr(zipfile.ZipInfo("csv/S1M_r.csv"), table)
    return buffer.getvalue()


def _exact_discovery_package_bytes() -> bytes:
    codes = ("FL154090005.Q", "FU154090005.Q", "FV108090105.Q")
    dictionary = "".join(
        f"{code}\tExact {code[:2]} series\tLine {index}\tF9.9 Exact\t"
        "Millions of dollars; source-reported metadata\n"
        for index, code in enumerate(codes, start=1)
    )
    table = "date," + ",".join(codes) + "\n2026:Q1,1,2,3\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(zipfile.ZipInfo("data_dictionary/F9_9.txt"), dictionary)
        archive.writestr(zipfile.ZipInfo("csv/F9_9.csv"), table)
    return buffer.getvalue()


def _all_prefix_package_bytes() -> bytes:
    units = {
        "FA": "Millions of dollars; seasonally adjusted annual rate",
        "FC": "Millions of dollars; change in unadjusted level",
        "FG": "Percent; seasonally adjusted growth rate",
        "FI": "Index",
        "FL": "Millions of dollars; level, not seasonally adjusted",
        "FR": "Millions of dollars; revaluation",
        "FS": "Millions of dollars; seasonal factor",
        "FU": "Millions of dollars; transactions, not seasonally adjusted",
        "FV": "Millions of dollars; other changes in volume",
        "LA": "Millions of dollars; level, seasonally adjusted",
        "LM": "Millions of dollars; market value or current cost, not seasonally adjusted",
        "PC": "Percent change in index",
    }
    codes = [f"{kind}999000000.Q" for kind in Z1_KIND_CODES]
    dictionary = "".join(
        f"{code}\tExample {kind} series\tLine {index}\tF9.9 Example\t{units[kind]}\n"
        for index, (kind, code) in enumerate(zip(Z1_KIND_CODES, codes, strict=True), start=1)
    )
    table = "date," + ",".join(codes) + "\n2025:Q4," + ",".join(
        str(index) for index in range(1, len(codes) + 1)
    ) + "\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(zipfile.ZipInfo("data_dictionary/F9_9.txt"), dictionary)
        archive.writestr(zipfile.ZipInfo("csv/F9_9.csv"), table)
    return buffer.getvalue()


def _client(package: bytes, counters: dict[str, int]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("z1_csv_files.zip")
        counters[request.method] = counters.get(request.method, 0) + 1
        headers = {"Last-Modified": "Thu, 11 Jun 2026 16:00:11 GMT"}
        if request.method == "HEAD":
            return httpx.Response(200, headers=headers, request=request)
        return httpx.Response(200, content=package, headers=headers, request=request)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _revised_package(package: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(package)) as source, zipfile.ZipFile(buffer, "w") as target:
        for name in source.namelist():
            content = source.read(name)
            content = content.replace(b"Federal government", b"Revised federal government")
            content = content.replace(b"896092", b"999999")
            target.writestr(zipfile.ZipInfo(name), content)
    return buffer.getvalue()


def _package_with_member(member: str, content: bytes) -> bytes:
    buffer = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(_package_bytes())) as source,
        zipfile.ZipFile(buffer, "w") as target,
    ):
        for name in source.namelist():
            target.writestr(
                zipfile.ZipInfo(name), content if name == member else source.read(name)
            )
    return buffer.getvalue()


@pytest.mark.parametrize(
    "content",
    [
        b"date,FL313020005.Q,FL313020005.Q\n2026:Q1,1,999\n",
        b"date,FL313020005.Q\n2026:Q1\n",
        b"date,FL313020005.Q\n2026:Q1,1,999\n",
        b"date,FL313020005.Q\n2026:Q1,\xff\n",
        b"period,FL313020005.Q\n2026:Q1,1\n",
        b"date,FU313020005.Q\n2026:Q1,1\n",
        b'date,FL313020005.Q\n2026:Q1,"1\n',
    ],
    ids=[
        "duplicate-header", "short-row", "extra-cell", "invalid-utf8", "no-date", "no-code", "quote"
    ],
)
def test_z1_rejects_malformed_csv_inside_valid_zip(tmp_path: Path, content: bytes) -> None:
    package = _package_with_member("csv/F2_2_s.csv", content)
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        assert archive.testzip() is None
    with (
        _client(package, {}) as client,
        FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service,
        pytest.raises(SourceSchemaChangedError),
    ):
        service.z1_series("FL313020005.Q", "FL")


def test_z1_rejects_invalid_utf8_dictionary_inside_valid_zip(tmp_path: Path) -> None:
    package = _package_with_member(
        "data_dictionary/F2_2_s.txt",
        b"FL313020005.Q\tInvalid \xff metadata\tLine 25\tF2.2.s\tMillions of dollars\n",
    )
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        assert archive.testzip() is None
    with (
        _client(package, {}) as client,
        FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service,
        pytest.raises(SourceSchemaChangedError, match=r"dictionary.*UTF-8"),
    ):
        service.z1_series("FL313020005.Q", "FL")


def test_z1_extracts_unique_selected_series_with_duplicate_unselected_columns(
    tmp_path: Path,
) -> None:
    # The official S1M_i_q.csv repeats unrelated FU columns alongside unique
    # FR/FC net-worth columns. Unselected columns cannot affect extraction.
    package = _package_with_member(
        "csv/F2_2_s.csv",
        b"date,FU313020005.Q,FL313020005.Q,FU313020005.Q\n2026:Q1,1,42,999\n",
    )
    with (
        _client(package, {}) as client,
        FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service,
    ):
        result = service.z1_series("FL313020005.Q", "FL")

    assert result["preview"]["head"] == [{"date": "2026-03-31", "value": "42"}]


@pytest.mark.parametrize(
    "content",
    [
        b"date,FL313020005.Q,date\n2026:Q1,42,2026:Q1\n",
        b"date,FL313020005.Q,FU313020005.Q,FU313020005.Q\n2026:Q1,42,1\n",
        b"date,FL313020005.Q,FU313020005.Q,FU313020005.Q\n2026:Q1,42,1,1,999\n",
    ],
    ids=["date-duplicate", "short-unselected", "extra-unselected"],
)
def test_z1_selection_preserves_ambiguity_and_full_row_width_checks(
    tmp_path: Path, content: bytes
) -> None:
    with (
        _client(_package_with_member("csv/F2_2_s.csv", content), {}) as client,
        FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service,
        pytest.raises(SourceSchemaChangedError),
    ):
        service.z1_series("FL313020005.Q", "FL")


def test_z1_accepts_duplicate_requested_columns_only_when_every_row_agrees(tmp_path: Path) -> None:
    package = _package_with_member(
        "csv/F2_2_s.csv",
        b"date,FL313020005.Q,FL313020005.Q\n2025:Q4,12,12\n2026:Q1,42,42\n",
    )
    with (
        _client(package, {}) as client,
        FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service,
    ):
        result = service.z1_series("FL313020005.Q", "FL")

    assert result["preview"]["head"] == [
        {"date": "2025-12-31", "value": "12"},
        {"date": "2026-03-31", "value": "42"},
    ]


def test_z1_rejects_later_duplicate_conflict_before_date_filtering(tmp_path: Path) -> None:
    package = _package_with_member(
        "csv/F2_2_s.csv",
        b"date,FL313020005.Q,FL313020005.Q\n2025:Q4,12,12\n2026:Q1,42,999\n",
    )
    with (
        _client(package, {}) as client,
        FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service,
        pytest.raises(SourceSchemaChangedError, match="conflicting duplicate"),
    ):
        service.z1_series("FL313020005.Q", "FL", end="2025-12-31")


def test_z1_download_binds_release_to_get_response_when_head_changes(tmp_path: Path) -> None:
    package = _package_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        modified = (
            "Thu, 11 Jun 2026 16:00:11 GMT"
            if request.method == "HEAD"
            else "Fri, 12 Jun 2026 16:00:11 GMT"
        )
        return httpx.Response(
            200, content=package, headers={"Last-Modified": modified}, request=request
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service,
    ):
        result = service.z1_series("FL313020005.Q", "FL")

    assert result["provenance"]["source_metadata"]["release_date"] == "2026-06-12"
    assert result["provenance"]["vintage"].startswith("z1-release:2026-06-12|")


def test_z1_refresh_by_another_executor_invalidates_dictionary_cache(tmp_path: Path) -> None:
    original = _package_bytes()
    packages = iter([original, _revised_package(original)])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"" if request.method == "HEAD" else next(packages),
            headers={"Last-Modified": "Thu, 11 Jun 2026 16:00:11 GMT"},
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        requester = HttpRequester(client=client, sleep=lambda _: None)
        first = Z1Executor(requester, tmp_path)
        second = Z1Executor(requester, tmp_path)
        assert first.load_series("FL313020005.Q").meta.description.startswith("Federal")
        second.load_series("FL313020005.Q", refresh=True)
        updated = first.load_series("FL313020005.Q")

    assert updated.meta.description.startswith("Revised federal")
    assert updated.rows[-1]["value"] == "999999"


def test_z1_read_uses_the_bytes_validated_for_its_provenance(tmp_path: Path, monkeypatch) -> None:
    original = _package_bytes()
    revised = _revised_package(original)
    counters: dict[str, int] = {}
    with _client(original, counters) as client:
        executor = Z1Executor(HttpRequester(client=client, sleep=lambda _: None), tmp_path)
        build_index = executor._series_index

        def replace_after_index(*args, **kwargs):
            index = build_index(*args, **kwargs)
            (tmp_path / "z1-release-2026-06-11.zip").write_bytes(revised)
            return index

        monkeypatch.setattr(executor, "_series_index", replace_after_index)
        result = executor.load_series("FL313020005.Q")

    assert result.rows[-1]["value"] == "896092"
    assert result.package_metadata["package_sha256"] == hashlib.sha256(original).hexdigest()


def test_z1_concurrent_downloads_use_distinct_temporary_files(tmp_path: Path, monkeypatch) -> None:
    import os

    ready = Barrier(2)
    replace = os.replace

    def simultaneous_replace(source, destination):
        if Path(destination).suffix == ".zip":
            ready.wait(timeout=5)
        return replace(source, destination)

    monkeypatch.setattr(os, "replace", simultaneous_replace)
    with _client(_package_bytes(), {}) as client:
        requester = HttpRequester(client=client, sleep=lambda _: None)
        executors = [Z1Executor(requester, tmp_path) for _ in range(2)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(lambda executor: executor.load_series("FL313020005.Q"), executors)
            )

    assert len(results) == 2
    assert results[0].vintage == results[1].vintage


@pytest.mark.parametrize("period", ["2026:Q5", "0000:Q1", "2026-02-30", "unknown"])
def test_z1_rejects_unrecognized_or_invalid_source_periods(tmp_path: Path, period: str) -> None:
    with (
        _client(_package_bytes(final_period=period), {}) as client,
        FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service,
        pytest.raises(SourceSchemaChangedError, match="observation period"),
    ):
        service.z1_series("FL313020005.Q", "FL", start="2026-01-01")


def test_z1_code_normalization() -> None:
    assert normalize_z1_code("FL313020005") == "FL313020005.Q"
    assert normalize_z1_code("fl313020005.q") == "FL313020005.Q"
    assert normalize_z1_code("BOGZ1FL313020005Q") == "FL313020005.Q"
    assert normalize_z1_code("BOGZ1LM153061105A") == "LM153061105.A"


def test_z1_kind_enforcement_refuses_mismatch(tmp_path: Path) -> None:
    """The whole point of z1_series: an FL level must never be served as a
    flow (or vice versa), because level changes include valuation and
    reclassification effects that are not transactions."""
    counters: dict[str, int] = {}
    client = _client(_package_bytes(), counters)
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        with pytest.raises(ValueError, match=r"refusing to present FL313020005\.Q as FU"):
            service.z1_series("FL313020005.Q", "FU")
        with pytest.raises(ValueError, match=r"refusing to present FA313020005\.Q as FU"):
            service.z1_series("FA313020005.Q", "FU")
        with pytest.raises(ValueError, match=r"official Z\.1 series prefix"):
            service.z1_series("FL313020005.Q", "XX")
    assert counters == {}
    client.close()


def test_z1_all_official_prefixes_preserve_semantics_and_refuse_relabeling(
    tmp_path: Path,
) -> None:
    counters: dict[str, int] = {}
    client = _client(_all_prefix_package_bytes(), counters)
    units = {
        "FA": "Millions of dollars; seasonally adjusted annual rate",
        "FC": "Millions of dollars; change in unadjusted level",
        "FG": "Percent; seasonally adjusted growth rate",
        "FI": "Index",
        "FL": "Millions of dollars; level, not seasonally adjusted",
        "FR": "Millions of dollars; revaluation",
        "FS": "Millions of dollars; seasonal factor",
        "FU": "Millions of dollars; transactions, not seasonally adjusted",
        "FV": "Millions of dollars; other changes in volume",
        "LA": "Millions of dollars; level, seasonally adjusted",
        "LM": "Millions of dollars; market value or current cost, not seasonally adjusted",
        "PC": "Percent change in index",
    }
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        for index, kind in enumerate(Z1_KIND_CODES):
            wrong_kind = Z1_KIND_CODES[(index + 1) % len(Z1_KIND_CODES)]
            with pytest.raises(ValueError, match=rf"refusing to present {kind}999000000\.Q"):
                service.z1_series(f"{kind}999000000.Q", wrong_kind)
        assert counters == {}

        fetched = {
            kind: service.z1_series(f"{kind}999000000.Q", kind) for kind in Z1_KIND_CODES
        }

    assert counters.get("GET") == 1
    for kind, result in fetched.items():
        assert result["dataset"]["title"] == f"Example {kind} series"
        assert result["dataset"]["semantics"]["kind"] == kind
        assert result["dataset"]["semantics"]["notes"] == EXPECTED_Z1_MEANINGS[kind]
        assert result["dataset"]["units"]["label"] == units[kind]
        assert EXPECTED_Z1_CAVEAT_IDS[kind] in {
            item["id"] for item in result["caveats"] if item["severity"] == "critical"
        }

    fa_caveat = next(
        item
        for item in fetched["FA"]["caveats"]
        if item["id"] == "z1.fa.saar_transaction"
    )
    assert "divide the published value by 4 exactly once" in fa_caveat["message"].casefold()
    client.close()


def test_z1_cataloged_series_uses_package_and_caches(tmp_path: Path) -> None:
    counters: dict[str, int] = {}
    client = _client(_package_bytes(), counters)
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        first = service.z1_series("BOGZ1FL313020005Q", "FL", start="2025-01-01")
        second = service.z1_series("FL313020005.Q", "FL", start="2025-01-01")

    assert counters.get("GET") == 1
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert first["dataset"]["id"] == "z1.federal_checkable_deposits_level"
    assert first["preview"]["row_count"] == 2
    assert first["preview"]["date_coverage"] == {"start": "2025-12-31", "end": "2026-03-31"}
    package_sha256 = hashlib.sha256(_package_bytes()).hexdigest()
    assert first["provenance"]["vintage"] == (
        f"z1-release:2026-06-11|sha256:{package_sha256}"
    )
    assert first["provenance"]["source_metadata"]["package_sha256"] == package_sha256
    assert first["provenance"]["source_metadata"]["package_byte_count"] == len(
        _package_bytes()
    )
    assert first["provenance"]["source_metadata"]["release_date"] == "2026-06-11"
    assert first["artifact"]["mime_type"] == "text/csv"
    assert first["artifact"] == second["artifact"]
    assert "full_data_path" not in first
    assert "manifest_path" not in first
    assert {item["id"] for item in first["caveats"]} >= {"z1.fl.level_not_flow"}
    client.close()


@pytest.mark.parametrize("damage", ["bytes", "manifest-json", "manifest-field"])
def test_z1_sidecache_revalidates_and_repairs_damaged_package(
    tmp_path: Path,
    damage: str,
) -> None:
    package = _package_bytes()
    counters: dict[str, int] = {}
    client = _client(package, counters)
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        service.z1_series("FL313020005.Q", "FL")

    zip_path = tmp_path / "packages" / "z1" / "z1-release-2026-06-11.zip"
    manifest_path = zip_path.with_suffix(".manifest.json")
    if damage == "bytes":
        zip_path.write_bytes(zip_path.read_bytes() + b"tamper")
    elif damage == "manifest-json":
        manifest_path.write_text("{", encoding="utf-8")
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.pop("sha256")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        repaired = service.z1_series("FL313020005.Q", "FL")

    digest = hashlib.sha256(package).hexdigest()
    assert counters["GET"] == 2
    assert zip_path.read_bytes() == package
    assert repaired["provenance"]["vintage"] == (
        f"z1-release:2026-06-11|sha256:{digest}"
    )
    assert repaired["provenance"]["source_metadata"]["package_sha256"] == digest
    client.close()


def test_z1_uncataloged_series_synthesizes_entry_from_dictionary(tmp_path: Path) -> None:
    counters: dict[str, int] = {}
    client = _client(_package_bytes(), counters)
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        result = service.z1_series("FL999000000", "FL")
        with pytest.raises(DatasetNotFoundError, match="not present in release"):
            service.z1_series("FL000000000.Q", "FL")

    assert result["dataset"]["id"] == "z1.fl999000000_q"
    assert result["dataset"]["title"] == "Example uncataloged series"
    assert result["preview"]["row_count"] == 3
    assert {item["id"] for item in result["caveats"]} == {
        "z1.fl.level_not_flow",
        "z1.revisions",
    }
    client.close()


def test_z1_serves_complete_accumulation_identity_including_revaluation(tmp_path: Path) -> None:
    counters: dict[str, int] = {}
    client = _client(_accumulation_package_bytes(), counters)
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        fetched = {
            kind: service.z1_series(f"{kind}152090005.Q", kind)
            for kind in ("FC", "FU", "FR", "FV", "LM")
        }

    assert counters.get("GET") == 1
    values = {
        kind: float(result["preview"]["head"][-1]["value"]) for kind, result in fetched.items()
    }
    assert values["FC"] == values["FU"] + values["FR"] + values["FV"]
    assert values["LM"] == 1100
    assert fetched["FR"]["dataset"]["semantics"] == {
        "kind": "FR",
        "granularity": None,
        "basis": None,
        "notes": "a revaluation (holding gain or loss)",
    }
    assert fetched["FR"]["dataset"]["units"] == {
        "label": "Millions of dollars; revaluation",
        "scale": None,
        "seasonal_adjustment": None,
        "annualized": False,
        "unit_class": "usd_millions",
    }
    assert {item["id"] for item in fetched["FR"]["caveats"]} == {
        "z1.fr.revaluation_not_transaction",
        "z1.revisions",
    }
    assert {item["id"] for item in fetched["FV"]["caveats"]} == {
        "z1.fv.other_volume_change",
        "z1.revisions",
    }
    client.close()


def test_find_series_discovers_uncataloged_z1_revaluation_without_hardcoded_code(
    tmp_path: Path,
) -> None:
    counters: dict[str, int] = {}
    client = _client(_accumulation_package_bytes(), counters)
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        result = service.find_series(
            "quarterly household net worth holding gains",
            family="z1",
        )

    assert counters.get("GET") == 1
    assert result["count"] == 0
    assert result["uncurated_upstream"][0] == {
        "id": "FR152090005.Q",
        "series_id": "FR152090005.Q",
        "title": "Households and nonprofit organizations; net worth",
        "family": "z1",
        "units": "Millions of dollars; revaluation",
        "frequency": "quarterly",
        "kind": "FR",
        "table": "S1M.r Households and nonprofit organizations",
        "curated": False,
        "source": "z1_data_dictionary",
        "release_date": "2026-06-11",
        "tools": {"z1_series": {"code": "FR152090005.Q", "kind": "FR"}},
    }
    assert "no curated caveat review" in result["note"].casefold()
    client.close()


def test_find_series_resolves_exact_z1_mnemonics_before_fuzzy_scoring(tmp_path: Path) -> None:
    counters: dict[str, int] = {}
    client = _client(_exact_discovery_package_bytes(), counters)
    queries = {
        "FL154090005": "FL154090005.Q",
        "FU154090005.Q": "FU154090005.Q",
        "BOGZ1FV108090105Q": "FV108090105.Q",
    }
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        found = {
            query: service.find_series(query, family="z1") for query in queries
        }

    assert counters.get("GET") == 1
    for query, code in queries.items():
        response = found[query]
        assert response["count"] == 0
        assert response["results"] == []
        assert len(response["uncurated_upstream"]) == 1
        result = response["uncurated_upstream"][0]
        assert result["id"] == code
        assert result["curated"] is False
        assert result["source"] == "z1_data_dictionary"
        assert result["tools"] == {"z1_series": {"code": code, "kind": code[:2]}}
        assert "history_start" not in result
        assert "units" not in result
        assert "verified_on" not in result
    client.close()


def test_find_series_does_not_substitute_for_an_absent_exact_z1_mnemonic(tmp_path: Path) -> None:
    counters: dict[str, int] = {}
    client = _client(_exact_discovery_package_bytes(), counters)
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        result = service.find_series("FV105013665.Q", family="z1")

    assert counters.get("GET") == 1
    assert result["count"] == 0
    assert result["results"] == []
    assert result["uncurated_upstream"] == []
    assert "data dictionary returned nothing" in result["note"]
    client.close()


def test_find_series_keeps_free_text_z1_discovery_unchanged(tmp_path: Path) -> None:
    counters: dict[str, int] = {}
    client = _client(_accumulation_package_bytes(), counters)
    with FiscusService(data_dir=tmp_path, http_client=client, sleep=lambda _: None) as service:
        result = service.find_series("quarterly household net worth holding gains", family="z1")

    assert counters.get("GET") == 1
    assert result["results"] == []
    assert result["uncurated_upstream"][0]["id"] == "FR152090005.Q"
    assert result["uncurated_upstream"][0]["units"] == "Millions of dollars; revaluation"
    client.close()
