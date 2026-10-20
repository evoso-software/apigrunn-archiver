from __future__ import annotations

import sqlite3

import pytest

from apigrunn_archiver import Archiver, SnapshotNotFound, index

from .conftest import edit_cards

YOLO = "tP_kg20VAho"            # 2025, on tech + science
FOUR_TRACKS = "ys__Y1mICwo"     # on tech, government, education, sensible-ai


def drop_talk(site, video_id):
    """Delete a talk from every page that lists it, like the site would."""
    for track in site.tracks_listing(video_id):
        site.override[track] = edit_cards(site.page(track), video_id, remove=True)


def test_first_run_indexes_the_whole_archive(archiver):
    result = archiver.run()
    assert result.talk_changes is None
    assert result.parse_errors == []

    talks = archiver.talks()
    assert len(talks) == 66
    assert sum(len(t.tracks) for t in talks) == 145
    by_id = {t.video_id: t for t in talks}
    assert by_id[FOUR_TRACKS].tracks == ("education", "government", "sensible-ai", "tech")
    yolo = by_id[YOLO]
    assert yolo.title == "We Put YOLO on a Tractor — What could go wrong?"
    assert yolo.speakers == ["Wieneke Keller", "Navaneeth Krishnan"]
    assert yolo.year == 2025


def test_talks_are_sorted_and_filterable(archiver):
    archiver.run()
    talks = archiver.talks()
    keys = [(-(t.year or 0), t.title.casefold()) for t in talks]
    assert keys == sorted(keys)
    assert {t.year for t in archiver.talks(year=2024)} == {2024}
    assert len(archiver.talks(year=2024)) == 27
    assert len(archiver.talks(track="healthcare")) == 6
    assert YOLO in {t.video_id for t in archiver.talks(search="tractor")}


def test_unchanged_run_reports_no_changes_and_parses_nothing(archiver):
    archiver.run()
    result = archiver.run()
    assert result.talk_changes is not None
    assert result.talk_changes.empty
    assert archiver.reindex().parsed == 0


def test_each_page_version_is_parsed_once(archiver, site):
    archiver.run()
    archiver.run()
    drop_talk(site, YOLO)
    archiver.run()
    # 8 versions from run 1, plus tech and science rewritten in run 3.
    assert archiver.reindex(force=True).parsed == 10


def test_removed_talk_is_reported_and_kept(archiver, site):
    archiver.run()
    drop_talk(site, YOLO)
    result = archiver.run()

    changes = result.talk_changes
    assert [t.video_id for t in changes.removed] == [YOLO]
    assert changes.added == [] and changes.changed == []
    assert YOLO not in {t.video_id for t in archiver.talks()}
    # Still there as of run 1...
    assert YOLO in {t.video_id for t in archiver.talks(run_id=1)}
    # ...and in the list of talks the site dropped.
    gone = archiver.archived_talks(gone_only=True)
    assert [(g.talk.video_id, g.first_seen_run, g.last_seen_run) for g in gone] == [(YOLO, 1, 1)]
    assert gone[0].talk.title.startswith("We Put YOLO")


def test_returning_talk_is_added_again(archiver, site):
    archiver.run()
    drop_talk(site, YOLO)
    archiver.run()
    site.override.clear()
    result = archiver.run()
    assert [t.video_id for t in result.talk_changes.added] == [YOLO]
    entry = next(a for a in archiver.archived_talks() if a.talk.video_id == YOLO)
    assert entry.present and entry.last_seen_run == 3


def test_talk_dropped_from_one_track_is_a_change_not_a_removal(archiver, site):
    archiver.run()
    site.override["government"] = edit_cards(site.page("government"), FOUR_TRACKS, remove=True)
    changes = archiver.run().talk_changes

    assert changes.removed == []
    [(before, after)] = changes.changed
    assert "government" in before.tracks and "government" not in after.tracks


def test_edited_talk_is_a_change(archiver, site):
    archiver.run()
    for track in site.tracks_listing(YOLO):
        site.override[track] = edit_cards(site.page(track), YOLO, title="YOLO, revised")
    changes = archiver.run().talk_changes
    [(before, after)] = changes.changed
    assert before.title.startswith("We Put YOLO")
    assert after.title == "YOLO, revised"


def test_failed_page_is_stale_not_a_mass_deletion(archiver, site):
    archiver.run()
    site.gone.add("tech")
    changes = archiver.run().talk_changes

    assert changes.empty  # tech's talks are carried from run 1
    assert changes.stale_pages == ["https://www.aigrunn.org/tech"]
    assert len(archiver.talks()) == 66


def test_unparseable_page_is_recorded_and_stale(archiver, site):
    archiver.run()
    broken = '<div class="talks-year-group"><a class="talk-card" href="/x"></a></div>'
    site.override["tech"] = broken.encode()
    result = archiver.run()

    [(url, _, message)] = result.parse_errors
    assert url == "https://www.aigrunn.org/tech"
    assert "ARCHIVER-E200" not in message and "markup has probably changed" in message
    assert result.talk_changes.empty
    assert result.talk_changes.stale_pages == ["https://www.aigrunn.org/tech"]
    # The page itself archived fine; only the parse failed.
    assert result.status == "ok"


def test_parser_upgrade_reparses_old_pages(archiver, monkeypatch):
    archiver.run()
    monkeypatch.setattr(index, "PARSER_VERSION", index.PARSER_VERSION + 1)
    assert archiver.reindex().parsed == 8
    assert archiver.reindex().parsed == 0


def test_archive_from_before_the_index_is_indexed_on_open(site, db_path):
    with Archiver(db_path=db_path, transport=site.transport) as a:
        a.run()
    # Turn the file back into a schema-1 archive, as milestone 2 wrote it.
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            "DROP TABLE talk_cards; DROP TABLE parsed_pages; PRAGMA user_version = 1;"
        )

    with Archiver(db_path=db_path, transport=site.transport) as a:
        assert len(a.talks()) == 66


def test_talks_on_empty_archive_and_unknown_run(archiver):
    assert archiver.talks() == []
    assert archiver.archived_talks() == []
    archiver.run()
    with pytest.raises(SnapshotNotFound):
        archiver.talks(run_id=9)
    with pytest.raises(SnapshotNotFound):
        archiver.talk_changes(9)


def test_talk_changes_between_arbitrary_runs(archiver, site):
    archiver.run()
    drop_talk(site, YOLO)
    archiver.run()
    archiver.run()
    assert [t.video_id for t in archiver.talk_changes(1).removed] == [YOLO]
    assert archiver.talk_changes(2, 3).empty
