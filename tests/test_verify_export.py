from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime

import pytest

from apigrunn_archiver import SnapshotNotFound

from .conftest import fixture_bytes


def test_verify_clean_archive(archiver, site):
    archiver.run()
    site.override["tech"] = b"<html>v2</html>"
    archiver.run()
    result = archiver.verify()
    assert result.ok
    assert result.blobs_checked == 9
    assert str(result) == "ok — 9 blobs verified"


def test_verify_finds_tampered_and_undecompressable_blobs(archiver, db_path):
    archiver.run()
    tech = archiver.snapshot("tech").blob_sha256
    business = archiver.snapshot("business").blob_sha256
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE blobs SET compression = 'none', data = x'00' WHERE sha256 = ?", (tech,)
        )
        conn.execute("UPDATE blobs SET data = x'1f8b0800' WHERE sha256 = ?", (business,))

    result = archiver.verify()
    assert not result.ok
    assert result.blobs_checked == 8
    assert sorted(sha for sha, _ in result.bad_blobs) == sorted([tech, business])
    assert any("decompressed" in reason for _, reason in result.bad_blobs)


def test_verify_finds_dangling_references(archiver, db_path):
    archiver.run()
    tech = archiver.snapshot("tech").blob_sha256
    with sqlite3.connect(db_path) as conn:  # foreign keys are off by default here
        conn.execute("DELETE FROM blobs WHERE sha256 = ?", (tech,))

    result = archiver.verify()
    assert [(run, url) for run, url, _ in result.dangling] == [
        (1, "https://www.aigrunn.org/tech")
    ]


def test_export_latest_run(archiver, site, tmp_path):
    archiver.run()
    site.override["tech"] = b"<html>v2</html>"
    site.gone.add("hardware")
    archiver.run()

    out = tmp_path / "export"
    result = archiver.export(out)
    assert result.runs == [2]
    assert result.files == 7
    assert result.skipped == 1

    run_dir = out / "run-0002"
    assert (run_dir / "www.aigrunn.org" / "tech.html").read_bytes() == b"<html>v2</html>"
    assert (run_dir / "www.aigrunn.org" / "business.html").read_bytes() == fixture_bytes(
        "business"
    )
    assert not (run_dir / "www.aigrunn.org" / "hardware.html").exists()

    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["run"]["id"] == 2
    assert manifest["run"]["status"] == "partial"
    by_url = {s["url"]: s for s in manifest["snapshots"]}
    assert by_url["https://www.aigrunn.org/hardware"]["file"] is None
    assert by_url["https://www.aigrunn.org/hardware"]["error"] == "HTTP 404"
    assert by_url["https://www.aigrunn.org/tech"]["file"] == "www.aigrunn.org/tech.html"
    assert ["content-type", "text/html; charset=utf-8"] in [
        [k.lower(), v] for k, v in by_url["https://www.aigrunn.org/tech"]["headers"]
    ]


def test_export_sets_mtime_to_fetch_time(archiver, tmp_path):
    archiver.run()
    archiver.export(tmp_path)
    snap = archiver.snapshot("tech")
    mtime = os.stat(tmp_path / "run-0001" / "www.aigrunn.org" / "tech.html").st_mtime
    assert mtime == datetime.fromisoformat(snap.fetched_at).timestamp()


def test_export_specific_and_all_runs(archiver, site, tmp_path):
    archiver.run()
    site.override["tech"] = b"<html>v2</html>"
    archiver.run()

    assert archiver.export(tmp_path / "one", run_id=1).runs == [1]
    assert (tmp_path / "one" / "run-0001" / "www.aigrunn.org" / "tech.html").read_bytes() == (
        fixture_bytes("tech")
    )

    result = archiver.export(tmp_path / "all", all_runs=True)
    assert result.runs == [1, 2]
    assert result.files == 16
    assert sorted(p.name for p in (tmp_path / "all").iterdir()) == ["run-0001", "run-0002"]


def test_export_empty_archive_or_unknown_run_raises(archiver, tmp_path):
    with pytest.raises(SnapshotNotFound, match="no runs"):
        archiver.export(tmp_path)
    archiver.run()
    with pytest.raises(SnapshotNotFound, match="no run 7"):
        archiver.export(tmp_path, run_id=7)
