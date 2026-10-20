"""Checks against the real aigrunn.org. Run with ``pytest -m network``."""

from __future__ import annotations

import pytest

from apigrunn_archiver import Archiver

pytestmark = pytest.mark.network


def test_live_run(tmp_path):
    with Archiver(db_path=tmp_path / "live.db") as archiver:
        result = archiver.run()
        assert result.status == "ok", [s.error for s in result.snapshots if s.error]
        assert "aiGrunn" in archiver.html("tech")
