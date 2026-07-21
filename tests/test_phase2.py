from __future__ import annotations

import pytest

from fiscus.phase2 import release_table


def test_phase2_stub_is_explicit() -> None:
    with pytest.raises(NotImplementedError, match="phase 2"):
        release_table()
