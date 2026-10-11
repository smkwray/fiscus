from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExecutorKind(StrEnum):
    FRED = "fred"
    FISCALDATA = "fiscaldata"
    Z1 = "z1"
    SOMA = "soma"
    JSON_API = "json_api"
    BLS = "bls"
    FILE = "file"
    LOCAL_FILE = "local_file"
    BROWSER_DOWNLOAD = "browser_download"


class AccessMode(StrEnum):
    MACHINE = "machine"
    BROWSER_DOWNLOAD_ONLY = "browser_download_only"


class AuthMode(StrEnum):
    NONE = "none"
    OPTIONAL = "optional"
    REQUIRED = "required"


class ToolName(StrEnum):
    GET_SERIES = "get_series"
    GET_FILE = "get_file"
    FISCALDATA_QUERY = "fiscaldata_query"
    Z1_SERIES = "z1_series"
    SOMA_HOLDINGS = "soma_holdings"
    BEA_NIPA_TABLE = "bea_nipa_table"


EXECUTOR_TOOLS = {
    ExecutorKind.FRED: frozenset({ToolName.GET_SERIES}),
    ExecutorKind.JSON_API: frozenset({ToolName.GET_SERIES, ToolName.BEA_NIPA_TABLE}),
    ExecutorKind.BLS: frozenset({ToolName.GET_SERIES}),
    ExecutorKind.FISCALDATA: frozenset({ToolName.FISCALDATA_QUERY}),
    ExecutorKind.Z1: frozenset({ToolName.Z1_SERIES}),
    ExecutorKind.SOMA: frozenset({ToolName.SOMA_HOLDINGS}),
    ExecutorKind.FILE: frozenset({ToolName.GET_FILE}),
    ExecutorKind.LOCAL_FILE: frozenset({ToolName.GET_FILE}),
    ExecutorKind.BROWSER_DOWNLOAD: frozenset({ToolName.GET_FILE}),
}


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class ResponseFormat(StrEnum):
    CSV = "csv"
    TSV = "tsv"
    JSON = "json"
    ZIP_CSV = "zip_csv"
    HTML = "html"
    BINARY = "binary"


class VintageStrategy(StrEnum):
    FRED_REALTIME_OR_HTTP = "fred_realtime_or_http"
    HTTP_LAST_MODIFIED_OR_RETRIEVAL = "http_last_modified_or_retrieval"
    LOGICAL_RELEASE_PLUS_SHA256 = "logical_release_plus_sha256"
    RETRIEVAL = "retrieval"
    RELEASE_DATE = "release_date"


Z1_KIND_CODES = ("FA", "FC", "FG", "FI", "FL", "FR", "FS", "FU", "FV", "LA", "LM", "PC")
Z1Kind = Literal[*Z1_KIND_CODES]


class AuthSpec(StrictModel):
    mode: AuthMode = AuthMode.NONE
    env_var: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def validate_env_var(self) -> AuthSpec:
        if self.mode in {AuthMode.OPTIONAL, AuthMode.REQUIRED} and not self.env_var:
            raise ValueError("optional or required auth must name env_var")
        if self.mode == AuthMode.NONE and self.env_var:
            raise ValueError("auth mode none cannot name env_var")
        return self


class AccessSpec(StrictModel):
    executor: ExecutorKind
    mode: AccessMode = AccessMode.MACHINE
    auth: AuthSpec = Field(default_factory=AuthSpec)


class EndpointSpec(StrictModel):
    url: HttpUrl | None = None
    api_url: HttpUrl | None = None
    local_path: str | None = None
    series_id: str | None = None
    path_template: str | None = None
    method: Literal["GET"] = "GET"
    default_params: dict[str, str] = Field(default_factory=dict)
    allowed_params: list[str] = Field(default_factory=list)
    api_key_param: str | None = None
    start_param: str | None = None
    end_param: str | None = None


class PaginationSpec(StrictModel):
    kind: Literal["none", "fiscaldata"] = "none"
    default_page_size: int | None = Field(default=None, ge=1, le=10_000)
    hard_max_pages: int = Field(default=100, ge=1, le=1_000)


class ResponseSpec(StrictModel):
    format: ResponseFormat
    data_path: str | None = None
    date_fields: list[str] = Field(default_factory=list)
    value_fields: list[str] = Field(default_factory=list)
    date_normalizer: Literal["bea_period"] | None = None
    pagination: PaginationSpec = Field(default_factory=PaginationSpec)
    vintage_strategy: VintageStrategy = VintageStrategy.RETRIEVAL

    @model_validator(mode="after")
    def validate_normalizer(self) -> ResponseSpec:
        if self.date_normalizer and len(self.date_fields) < 2:
            raise ValueError(
                "date_normalizer requires date_fields [emitted_iso_field, source_period_field]"
            )
        return self


class HistorySpec(StrictModel):
    start: date
    end: date | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def validate_range(self) -> HistorySpec:
        if self.end is not None and self.end < self.start:
            raise ValueError("history end precedes history start")
        return self


class UnitSpec(StrictModel):
    label: str
    scale: str | None = None
    seasonal_adjustment: str | None = None
    annualized: bool | None = Field(
        default=False,
        description=(
            "Whether source values are annualized; null means the source route supplied "
            "insufficient metadata to determine annualization."
        ),
    )
    unit_class: str = "not_saar"


class RefreshSpec(StrictModel):
    cadence: str
    notes: str | None = None


class ReleaseSpec(StrictModel):
    logical_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    representation: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    published_on: date | None = None
    corrected_on: date | None = None
    discovery_url: HttpUrl
    layout_url: HttpUrl | None = None
    errata_url: HttpUrl | None = None

    @model_validator(mode="after")
    def validate_dates(self) -> ReleaseSpec:
        if (
            self.published_on is not None
            and self.corrected_on is not None
            and self.corrected_on < self.published_on
        ):
            raise ValueError("release corrected_on precedes published_on")
        return self


class SemanticsSpec(StrictModel):
    kind: Z1Kind | None = None
    granularity: str | None = None
    basis: str | None = None
    notes: str | None = None


class AlwaysPredicate(StrictModel):
    type: Literal["always"] = "always"


class DateRangeIntersectsPredicate(StrictModel):
    type: Literal["date_range_intersects"]
    start: date
    end: date | None = None


class DateRangeCrossesPredicate(StrictModel):
    type: Literal["date_range_crosses"]
    boundary: date


class ParamEqualsPredicate(StrictModel):
    type: Literal["param_equals"]
    param: str
    value: str


class ParamInPredicate(StrictModel):
    type: Literal["param_in"]
    param: str
    values: list[str] = Field(min_length=1)


class UnitMixingPredicate(StrictModel):
    type: Literal["unit_mixing"]
    with_unit_classes: list[str] = Field(min_length=1)


class KindInPredicate(StrictModel):
    type: Literal["kind_in"]
    kinds: list[Z1Kind] = Field(min_length=1)


CaveatPredicate = Annotated[
    AlwaysPredicate
    | DateRangeIntersectsPredicate
    | DateRangeCrossesPredicate
    | ParamEqualsPredicate
    | ParamInPredicate
    | UnitMixingPredicate
    | KindInPredicate,
    Field(discriminator="type"),
]


class CaveatWhen(StrictModel):
    match: Literal["all", "any"] = "all"
    predicates: list[CaveatPredicate] = Field(
        default_factory=lambda: [AlwaysPredicate()],
        min_length=1,
    )

    @model_validator(mode="after")
    def validate_always(self) -> CaveatWhen:
        if len(self.predicates) > 1 and any(p.type == "always" for p in self.predicates):
            raise ValueError("always cannot be combined with other caveat predicates")
        return self


class Caveat(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    severity: Severity = Severity.WARNING
    message: str
    when: CaveatWhen = Field(default_factory=CaveatWhen)
    source_note: str | None = None


class CatalogEntry(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    family: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    title: str
    description: str
    publisher: str
    aliases: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    verified_on: date
    history: HistorySpec
    frequency: str
    units: UnitSpec
    endpoint: EndpointSpec
    access: AccessSpec
    response: ResponseSpec
    refresh: RefreshSpec
    release: ReleaseSpec | None = None
    semantics: SemanticsSpec = Field(default_factory=SemanticsSpec)
    tools: list[ToolName] = Field(min_length=1)
    caveats: list[Caveat] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_entry_contract(self) -> CatalogEntry:
        if not self.id.startswith(f"{self.family}."):
            raise ValueError("entry id must begin with '<family>.'")
        compatible_tools = EXECUTOR_TOOLS[self.access.executor]
        if len(self.tools) != 1 or self.tools[0] not in compatible_tools:
            allowed = ", ".join(sorted(tool.value for tool in compatible_tools))
            raise ValueError(
                f"{self.access.executor.value} entries must declare exactly one compatible "
                f"tool from: {allowed}"
            )
        if self.access.executor == ExecutorKind.FRED:
            if not self.endpoint.series_id:
                raise ValueError("FRED entries require endpoint.series_id")
            if self.response.vintage_strategy != VintageStrategy.FRED_REALTIME_OR_HTTP:
                raise ValueError("FRED entries must use fred_realtime_or_http vintage strategy")
        if self.access.executor == ExecutorKind.FISCALDATA:
            if self.response.format != ResponseFormat.JSON:
                raise ValueError("FiscalData entries must declare a JSON source response")
            if self.response.pagination.kind != "fiscaldata":
                raise ValueError("FiscalData entries must declare fiscaldata pagination")
            if self.response.vintage_strategy != VintageStrategy.HTTP_LAST_MODIFIED_OR_RETRIEVAL:
                raise ValueError(
                    "FiscalData entries must use http_last_modified_or_retrieval vintage strategy"
                )
        if (
            self.access.executor in {ExecutorKind.Z1, ExecutorKind.SOMA}
            and self.response.vintage_strategy != VintageStrategy.RELEASE_DATE
        ):
            raise ValueError("Z.1 and SOMA entries must use release_date vintage strategy")
        if self.access.executor == ExecutorKind.JSON_API:
            if self.response.format != ResponseFormat.JSON:
                raise ValueError("json_api entries must declare a JSON source response")
            if not self.response.data_path:
                raise ValueError("json_api entries must declare response.data_path")
            if self.access.auth.mode != AuthMode.NONE and not self.endpoint.api_key_param:
                raise ValueError("keyed json_api entries must declare endpoint.api_key_param")
        if self.access.executor == ExecutorKind.BLS and not self.endpoint.series_id:
            raise ValueError("BLS entries require endpoint.series_id")
        if self.access.executor == ExecutorKind.Z1:
            if not self.endpoint.series_id:
                raise ValueError("Z.1 entries require endpoint.series_id")
            prefix = self.endpoint.series_id.strip().upper().removeprefix("BOGZ1")[:2]
            if self.semantics.kind != prefix:
                raise ValueError("Z.1 semantics.kind must match the endpoint.series_id prefix")
        if self.access.executor in {ExecutorKind.FILE, ExecutorKind.LOCAL_FILE} and (
            self.response.format
            not in {ResponseFormat.CSV, ResponseFormat.TSV, ResponseFormat.BINARY}
        ):
            raise ValueError("file entries must declare csv, tsv, or binary response format")
        if self.access.executor == ExecutorKind.LOCAL_FILE:
            if not self.endpoint.local_path:
                raise ValueError("local_file entries require endpoint.local_path")
        elif not self.endpoint.url:
            raise ValueError("entries require endpoint.url unless the executor is local_file")
        if self.response.vintage_strategy == VintageStrategy.LOGICAL_RELEASE_PLUS_SHA256:
            if self.access.executor != ExecutorKind.FILE:
                raise ValueError(
                    "logical_release_plus_sha256 is available only to machine file entries"
                )
            if self.release is None:
                raise ValueError(
                    "logical_release_plus_sha256 entries must declare release metadata"
                )
        elif self.release is not None:
            raise ValueError(
                "release metadata requires logical_release_plus_sha256 vintage strategy"
            )
        if (
            self.access.mode == AccessMode.BROWSER_DOWNLOAD_ONLY
            and self.access.executor != ExecutorKind.BROWSER_DOWNLOAD
        ):
            raise ValueError("browser-download-only entries must use browser_download executor")
        if (
            self.access.executor == ExecutorKind.BROWSER_DOWNLOAD
            and self.access.mode != AccessMode.BROWSER_DOWNLOAD_ONLY
        ):
            raise ValueError("browser_download entries must use browser_download_only mode")
        if self.family == "z1" and self.semantics.kind is None:
            raise ValueError("Z.1 entries must declare an official series-prefix semantics.kind")
        return self


class CatalogFile(StrictModel):
    schema_version: Literal[1]
    family: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    entries: list[CatalogEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_family(self) -> CatalogFile:
        wrong = [entry.id for entry in self.entries if entry.family != self.family]
        if wrong:
            raise ValueError(f"entries do not match file family {self.family}: {wrong}")
        return self
