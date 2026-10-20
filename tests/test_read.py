from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime

import pytest

from apigrunn_archiver import SnapshotNotFound


def _backdate(db_path, run_id: int, fetched_at: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE snapshots SET fetched_at = ? WHERE run_id = ?", (fetched_at, run_id))


def test_at_returns_version_as_of_that_moment(archiver, site, db_path):
    first = archiver.run()
    _backdate(db_path, first.run_id, "2026-03-01T09:00:00+00:00")
    site.override["tech"] = b"<html>april</html>"
    second = archiver.run()
    _backdate(db_path, second.run_id, "2026-04-01T09:00:00+00:00")

    assert archiver.html("tech", at="2026-03-01") != "<html>april</html>"
    assert archiver.snapshot("tech", at="2026-03-15").run_id == first.run_id
    assert archiver.snapshot("tech", at=date(2026, 4, 1)).run_id == second.run_id
    assert archiver.snapshot("tech", at="2026-04-01T09:00:00+00:00").run_id == second.run_id
    assert (
        archiver.snapshot("tech", at=datetime(2026, 4, 1, 8, 59, tzinfo=UTC)).run_id
        == first.run_id
    )
    assert archiver.html("tech") == "<html>april</html>"


def test_at_before_first_run_raises(archiver, db_path):
    run = archiver.run()
    _backdate(db_path, run.run_id, "2026-03-01T09:00:00+00:00")
    with pytest.raises(SnapshotNotFound, match=r"ARCHIVER-E400"):
        archiver.snapshot("tech", at="2026-02-28")


def test_at_skips_error_snapshots(archiver, site, db_path):
    good = archiver.run()
    _backdate(db_path, good.run_id, "2026-03-01T09:00:00+00:00")
    site.gone.add("tech")
    bad = archiver.run()
    _backdate(db_path, bad.run_id, "2026-03-02T09:00:00+00:00")

    assert archiver.snapshot("tech", at="2026-03-03").run_id == good.run_id


def test_unknown_url_raises(archiver):
    archiver.run()
    with pytest.raises(SnapshotNotFound):
        archiver.html("https://www.aigrunn.org/nope")


def test_full_url_and_slug_are_equivalent(archiver):
    archiver.run()
    assert archiver.content("tech") == archiver.content("https://www.aigrunn.org/tech")


def test_at_and_run_id_are_exclusive(archiver):
    archiver.run()
    with pytest.raises(ValueError):
        archiver.snapshot("tech", at="2026-01-01", run_id=1)
