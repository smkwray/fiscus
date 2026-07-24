from __future__ import annotations


class FiscusError(Exception):
    """Base error for expected, user-actionable failures."""

    code = "fiscus_error"


class CatalogError(FiscusError):
    """The catalog could not be loaded or validated."""

    code = "catalog_invalid"


class DatasetNotFoundError(FiscusError):
    """A requested catalog identifier does not exist."""

    code = "catalog_absent"


class UnsupportedToolError(FiscusError):
    """A dataset is not available through the requested tool."""

    code = "unsupported_tool"


class MissingKeyError(FiscusError):
    """A catalog entry requires an environment key that is absent."""

    code = "missing_key"


class InvalidVintageError(FiscusError):
    """A requested historical information date is malformed or not closed."""

    code = "invalid_vintage"


class UnsupportedVintageError(FiscusError):
    """A source cannot serve the requested historical information date."""

    code = "unsupported_vintage"


class BrowserDownloadOnlyError(FiscusError):
    """A source is intentionally labeled as browser-download-only."""

    code = "browser_download_only"


class SourceRequestError(FiscusError):
    """A source request failed after retries or returned invalid data."""

    code = "source_request_failed"


class SourceRemovedError(SourceRequestError):
    """A cataloged source locator returned HTTP 404 or 410."""

    code = "source_removed"


class SourceAccessDeniedError(SourceRequestError):
    """A source returned HTTP 403 or 451 without a more specific proven cause."""

    code = "source_access_denied"


class CacheError(FiscusError):
    """The local immutable cache could not satisfy an operation."""

    code = "cache_error"


class CacheCollisionError(CacheError):
    """A vintage key already exists with different bytes."""

    code = "cache_collision"
