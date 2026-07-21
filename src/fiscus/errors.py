from __future__ import annotations


class FiscusError(Exception):
    """Base error for expected, user-actionable failures."""


class CatalogError(FiscusError):
    """The catalog could not be loaded or validated."""


class DatasetNotFoundError(FiscusError):
    """A requested catalog identifier does not exist."""


class UnsupportedToolError(FiscusError):
    """A dataset is not available through the requested tool."""


class MissingKeyError(FiscusError):
    """A catalog entry requires an environment key that is absent."""


class BrowserDownloadOnlyError(FiscusError):
    """A source is intentionally labeled as browser-download-only."""


class SourceRequestError(FiscusError):
    """A source request failed after retries or returned invalid data."""


class CacheError(FiscusError):
    """The local immutable cache could not satisfy an operation."""


class CacheCollisionError(CacheError):
    """A vintage key already exists with different bytes."""
