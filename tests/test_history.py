from __future__ import annotations

import pytest

from apigrunn_archiver import SnapshotNotFound


def kinds(entries):
    return [(e.kind, e.snapshot.run_id) for e in entries]


def test_unchanged_page_has_single_entry(archiver):
    archiver.run()
    archiver.run()
    archiver.run()
    assert kinds(archiver.history("tech")) == [("first", 1)]


def test_history_reports_transitions_only(archiver, site):
    archiver.run()                                    # 1 first
    archiver.run()                                    # 2 same
    site.override["tech"] = b"<html>v2</html>"
    archiver.run()                                    # 3 changed
    site.gone.add("tech")
    archiver.run()                                    # 4 failed
    archiver.run()                                    # 5 still failing
    site.gone.clear()
    archiver.run()                                    # 6 recovered (same body as 3)

    entries = archiver.history("tech")
    assert kinds(entries) == [("first", 1), ("changed", 3), ("failed", 4), ("recovered", 6)]
    assert entries[2].snapshot.error == "HTTP 404"
    assert not entries[3].snapshot.changed


def test_recovery_with_new_body_is_flagged_changed(archiver, site):
    archiver.run()
    site.gone.add("tech")
    archiver.run()
    site.gone.clear()
    site.override["tech"] = b"<html>back, different</html>"
    archiver.run()
    last = archiver.history("tech")[-1]
    assert last.kind == "recovered"
    assert last.snapshot.changed


def test_page_that_never_worked(archiver, site):
    site.gone.add("tech")
    archiver.run()
    archiver.run()
    assert kinds(archiver.history("tech")) == [("failed", 1)]


def test_everything_lists_each_snapshot(archiver, site):
    archiver.run()
    archiver.run()
    site.fail.add("tech")
    archiver.run()
    archiver.run()
    assert kinds(archiver.history("tech", everything=True)) == [
        ("first", 1), ("same", 2), ("failed", 3), ("failing", 4)
    ]


def test_unknown_page_has_empty_history(archiver):
    archiver.run()
    assert archiver.history("https://www.aigrunn.org/nope") == []


def test_diff_between_runs(archiver, site):
    archiver.run()
    site.override["tech"] = b"<html>\n<p>one</p>\n</html>\n"
    archiver.run()
    site.override["tech"] = b"<html>\n<p>two</p>\n</html>\n"
    archiver.run()

    text = archiver.diff("tech", 2, 3)
    assert "-<p>one</p>\n" in text
    assert "+<p>two</p>\n" in text
    assert "(run 2," in text and "(run 3," in text


def test_diff_defaults_to_latest(archiver, site):
    archiver.run()
    site.override["tech"] = b"<p>new</p>"
    archiver.run()
    site.gone.add("tech")
    archiver.run()  # latest *good* snapshot is still run 2
    text = archiver.diff("tech", 1)
    assert "(run 2," in text
    assert text.endswith("+<p>new</p>\n")


def test_diff_identical_is_empty(archiver):
    archiver.run()
    archiver.run()
    assert archiver.diff("tech", 1, 2) == ""


def test_diff_missing_run_raises(archiver):
    archiver.run()
    with pytest.raises(SnapshotNotFound):
        archiver.diff("tech", 1, 99)
