from __future__ import annotations

from apigrunn_archiver import Archiver, track_url

from .conftest import fixture_bytes

TECH = track_url("tech")


def test_first_run_stores_every_page(archiver, site):
    result = archiver.run()

    assert result.status == "ok"
    assert result.fetched == 8
    assert result.changed == 8
    assert result.new_blobs == 8
    assert result.errors == 0
    assert archiver.content("tech") == fixture_bytes("tech")


def test_identical_run_adds_snapshots_but_no_blobs(archiver, site):
    archiver.run()
    second = archiver.run()

    assert second.fetched == 8
    assert second.changed == 0
    assert second.new_blobs == 0
    assert [r.id for r in archiver.runs()] == [second.run_id, second.run_id - 1]
    assert len(archiver.snapshots(second.run_id)) == 8


def test_changed_page_adds_one_blob(archiver, site):
    first = archiver.run()
    site.override["tech"] = b"<html>rewritten</html>"
    second = archiver.run()

    assert second.changed == 1
    assert second.new_blobs == 1
    changed = [s for s in second.snapshots if s.changed]
    assert [s.url for s in changed] == [TECH]
    # The old version is still there.
    assert archiver.content("tech", run_id=first.run_id) == fixture_bytes("tech")
    assert archiver.content("tech") == b"<html>rewritten</html>"


def test_reverting_page_reuses_old_blob(archiver, site):
    archiver.run()
    site.override["tech"] = b"<html>rewritten</html>"
    archiver.run()
    del site.override["tech"]
    third = archiver.run()

    assert third.changed == 1  # differs from the previous good version...
    assert third.new_blobs == 0  # ...but that content is already stored


def test_deleted_page_is_recorded_and_old_copy_survives(archiver, site):
    archiver.run()
    site.gone.add("tech")
    result = archiver.run()

    assert result.status == "partial"
    assert result.errors == 1
    snap = archiver.snapshot("tech", run_id=result.run_id)
    assert snap.status_code == 404
    assert snap.error == "HTTP 404"
    assert not snap.changed
    # Reading "the latest" skips the error and returns what we had.
    assert archiver.content("tech") == fixture_bytes("tech")


def test_error_body_is_kept_as_evidence(archiver, site):
    site.fail.add("tech")
    result = archiver.run()
    assert archiver.content("tech", run_id=result.run_id) == b"try later"


def test_network_error_is_recorded(archiver, site):
    site.unreachable.add("tech")
    result = archiver.run()

    snap = archiver.snapshot("tech", run_id=result.run_id)
    assert snap.status_code is None
    assert "connection refused" in snap.error
    assert snap.blob_sha256 is None


def test_all_pages_failing_marks_run_failed(archiver, site):
    site.fail.update(["tech", "business", "government", "education",
                      "sensible-ai", "science", "healthcare", "hardware"])
    result = archiver.run()
    assert result.status == "failed"
    assert archiver.runs()[0].status == "failed"


def test_default_run_downloads_in_full(archiver, site):
    archiver.run()
    site.requests.clear()
    archiver.run()
    assert all("If-None-Match" not in r.headers for r in site.requests)


def test_conditional_run_reuses_blob_on_304(archiver, site):
    first = archiver.run()
    site.requests.clear()
    second = archiver.run(conditional=True)

    assert all(r.headers.get("If-None-Match") for r in site.requests)
    assert second.status == "ok"
    assert second.changed == 0
    assert second.new_blobs == 0
    snap = archiver.snapshot("tech", run_id=second.run_id)
    assert snap.status_code == 304
    assert snap.blob_sha256 == archiver.snapshot("tech", run_id=first.run_id).blob_sha256
    assert archiver.content("tech", run_id=second.run_id) == fixture_bytes("tech")


def test_conditional_run_picks_up_changes(archiver, site):
    archiver.run()
    site.version = "v2"
    site.override["tech"] = b"<html>v2</html>"
    result = archiver.run(conditional=True)
    assert result.changed == 8 - 7  # only tech's body differs
    assert archiver.content("tech") == b"<html>v2</html>"


def test_archive_persists_across_instances(site, db_path):
    with Archiver(db_path=db_path, transport=site.transport) as a:
        a.run()
    with Archiver(db_path=db_path, transport=site.transport) as a:
        second = a.run()
        assert second.new_blobs == 0
        assert len(a.runs()) == 2


def test_raw_bytes_and_declared_charset_are_preserved(archiver, site):
    site.override["tech"] = "<p>Café Grünn</p>".encode("latin-1")
    site.content_type = "text/html; charset=ISO-8859-1"
    archiver.run()

    assert archiver.content("tech") == "<p>Café Grünn</p>".encode("latin-1")
    assert archiver.html("tech") == "<p>Café Grünn</p>"
