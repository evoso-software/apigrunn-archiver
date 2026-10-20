from __future__ import annotations

import sqlite3

import pytest

from apigrunn_archiver import SCHEMA_VERSION, IntegrityError, SchemaVersionError, Store
from apigrunn_archiver.store import default_db_path


def test_new_archive_is_at_current_schema(db_path):
    store = Store(db_path)
    assert store.schema_version == SCHEMA_VERSION
    store.close()


def test_newer_archive_is_refused_untouched(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")

    with pytest.raises(SchemaVersionError, match=r"ARCHIVER-E300") as info:
        Store(db_path)
    assert info.value.found == SCHEMA_VERSION + 1
    assert info.value.supported == SCHEMA_VERSION


@pytest.mark.parametrize("old", [1, 2])
def test_older_archive_is_refused_untouched(db_path, old):
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY)")
        conn.execute(f"PRAGMA user_version = {old}")

    with pytest.raises(SchemaVersionError, match=r"Move the file aside") as info:
        Store(db_path)
    assert info.value.found == old
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == old
        assert [r[0] for r in conn.execute("SELECT name FROM sqlite_master")] == ["runs"]


def test_corrupted_blob_is_detected(archiver, db_path):
    archiver.run()
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE blobs SET compression = 'none', data = x'00'")

    with pytest.raises(IntegrityError, match=r"ARCHIVER-E500"):
        archiver.content("tech")


def test_default_path_is_data_not_cache(monkeypatch, tmp_path):
    monkeypatch.delenv("APIGRUNN_ARCHIVE", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert default_db_path() == tmp_path / "apigrunn-archiver" / "archive.db"

    monkeypatch.setenv("APIGRUNN_ARCHIVE", str(tmp_path / "elsewhere.db"))
    assert default_db_path() == tmp_path / "elsewhere.db"
