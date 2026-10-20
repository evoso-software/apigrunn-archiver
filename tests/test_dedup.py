"""A page counts as changed only when its talks change: noise in the HTML must
not add blobs, page versions or talk versions."""

from __future__ import annotations

import sqlite3

import pytest

from apigrunn_archiver import index

from .conftest import add_noise, edit_cards, fixture_bytes

YOLO = "tP_kg20VAho"            # 2025, on tech + science
BROKEN = b'<div class="talks-year-group"><a class="talk-card" href="/x"></a></div>'


def counts(db_path) -> dict[str, int]:
    with sqlite3.connect(db_path) as conn:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("snapshots", "blobs", "page_versions", "talk_versions", "page_talks")
        }


def test_talk_shared_by_tracks_is_stored_once(archiver, db_path):
    archiver.run()
    # 145 cards across the eight pages, but only 66 distinct talks.
    assert counts(db_path)["talk_versions"] == 66
    assert counts(db_path)["page_talks"] == 145


def test_noise_only_run_adds_nothing_but_snapshots(archiver, site, db_path):
    archiver.run()
    before = counts(db_path)
    for track in ("tech", "science"):
        site.override[track] = add_noise(fixture_bytes(track), "nonce-1")
    result = archiver.run()

    assert result.changed == 0
    assert result.new_blobs == 0
    assert result.talk_changes.empty
    after = counts(db_path)
    assert after["snapshots"] == before["snapshots"] + 8
    assert {k: v for k, v in after.items() if k != "snapshots"} == {
        k: v for k, v in before.items() if k != "snapshots"
    }

    snap = archiver.snapshot("tech")
    assert not snap.exact
    assert snap.blob_sha256 == archiver.snapshot("tech", run_id=1).blob_sha256
    assert archiver.content("tech") == fixture_bytes("tech")  # the copy stored in run 1
    assert archiver.snapshot("business").exact


def test_edited_talk_adds_one_talk_version(archiver, site, db_path):
    archiver.run()
    before = counts(db_path)
    for track in site.tracks_listing(YOLO):
        site.override[track] = edit_cards(site.page(track), YOLO, title="YOLO, revised")
    result = archiver.run()

    assert result.changed == 2
    assert result.new_blobs == 2
    after = counts(db_path)
    assert after["talk_versions"] == before["talk_versions"] + 1
    assert after["page_versions"] == before["page_versions"] + 2


def test_reverting_with_noise_reuses_the_first_copy(archiver, site):
    archiver.run()
    site.override["tech"] = edit_cards(site.page("tech"), YOLO, remove=True)
    archiver.run()
    site.override["tech"] = add_noise(fixture_bytes("tech"), "back")
    third = archiver.run()

    assert third.changed == 1
    assert third.new_blobs == 0
    assert archiver.snapshot("tech").blob_sha256 == archiver.snapshot("tech", run_id=1).blob_sha256


def test_unparseable_page_is_always_stored(archiver, site):
    archiver.run()
    site.override["tech"] = BROKEN
    second = archiver.run()
    site.override["tech"] = BROKEN + b"<!-- 2 -->"
    third = archiver.run()

    assert second.new_blobs == third.new_blobs == 1
    assert archiver.content("tech") == BROKEN + b"<!-- 2 -->"
    assert archiver.snapshot("tech").page_sha256 is None


def test_conditional_304_keeps_body_hash(archiver, site):
    archiver.run()
    second = archiver.run(conditional=True)
    snap = archiver.snapshot("tech", run_id=second.run_id)
    assert snap.status_code == 304
    assert snap.exact


def test_parser_upgrade_only_rereads_stored_copies(archiver, site, monkeypatch):
    archiver.run()
    site.override["tech"] = add_noise(fixture_bytes("tech"), "n")
    archiver.run()
    assert archiver.history("tech")[-1].kind == "first"

    # A new parser that would have seen a change in the noisy copy.
    noisy = archiver.snapshot("tech", run_id=2)
    original = index.parse_track_page

    def parse(html, slug):
        cards = original(html, slug)
        return cards[1:] if 'nonce="n"' in html else cards

    monkeypatch.setattr(index, "parse_track_page", parse)
    monkeypatch.setattr(index, "PARSER_VERSION", index.PARSER_VERSION + 1)
    archiver.reindex()

    # Run 2 never stored its own HTML, so it still reads as the run-1 copy:
    # the limit of keeping one copy per page version.
    assert noisy.blob_sha256 == archiver.snapshot("tech", run_id=1).blob_sha256
    assert archiver.history("tech")[-1].kind == "first"

    site.override["tech"] = add_noise(fixture_bytes("tech"), "n")
    third = archiver.run()
    assert third.new_blobs == 1
    assert [(e.kind, e.snapshot.run_id) for e in archiver.history("tech")] == [
        ("first", 1), ("changed", 3)
    ]


def test_verify_finds_tampered_talk_version(archiver, db_path):
    archiver.run()
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE talk_versions SET title = 'forged' WHERE video_id = ?", (YOLO,))

    result = archiver.verify()
    assert not result.ok
    assert [reason for _, reason in result.bad_index] == ["talk version does not match its hash"]


@pytest.mark.parametrize("table", ["page_talks", "talk_versions"])
def test_verify_finds_broken_page_versions(archiver, db_path, table):
    archiver.run()
    with sqlite3.connect(db_path) as conn:  # foreign keys are off by default here
        conn.execute(f"DELETE FROM {table} WHERE rowid = (SELECT MIN(rowid) FROM {table})")
    assert not archiver.verify().ok
