from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_cache_path


def data_root(override: str | Path | None = None) -> Path:
    """Return the root for fiscus-owned cache and package data.

    Explicit artifact exports are caller-owned and may be written elsewhere; local-file
    sources are read in place and are never copied into this root.
    """
    if override is not None:
        return Path(override).expanduser()
    configured = os.environ.get("FISCUS_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    return user_cache_path("fiscus", appauthor=False)
