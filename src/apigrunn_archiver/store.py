"""Append-only SQLite store of every run, snapshot and page body.

Nothing in here is ever updated in place or deleted, apart from a run's own
status when it finishes. Page bodies are content-addressed: a page that did
not change between runs adds a snapshot row pointing at the blob it already
had, so a daily run of an unchanged site costs a handful of rows.

The schema is versioned with ``PRAGMA user_version`` and migrated forward in
place. Only an archive from a *newer* version is refused.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
import zlib
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

from .errors import IntegrityError, SchemaVersionError
from .fetcher import FetchResult, Validators
from .models import Run, Snapshot

#: Ordered forward migrations; index ``i`` takes the schema from ``i`` to ``i + 1``.
MIGRATIONS: Sequence[str] = (
    """
    CREATE TABLE runs (
        id           INTEGER PRIMARY KEY,
        started_at   TEXT NOT NULL,
        finished_at  TEXT,
        tool_version TEXT NOT NULL,
        status       TEXT NOT NULL
    );

    CREATE TABLE blobs (
        sha256      TEXT PRIMARY KEY,
        size        INTEGER NOT NULL,
        compression TEXT NOT NULL,
        data        BLOB NOT NULL
    );

    CREATE TABLE snapshots (
        run_id        INTEGER NOT NULL REFERENCES runs(id),
        url           TEXT NOT NULL,
        final_url     TEXT,
        fetched_at    TEXT NOT NULL,
        status_code   INTEGER,
        error         TEXT,
        headers       TEXT,
        etag          TEXT,
        last_modified TEXT,
        blob_sha256   TEXT REFERENCES blobs(sha256),
        changed       INTEGER NOT NULL,
        PRIMARY KEY (run_id, url)
    );

    CREATE INDEX idx_snapshots_url ON snapshots(url, fetched_at);
    """,
)

SCHEMA_VERSION = len(MIGRATIONS)


def default_db_path() -> Path:
    """Return the default archive location, honouring ``$APIGRUNN_ARCHIVE`` and XDG.

    This is data, not cache: it lives under ``$XDG_DATA_HOME`` so that tools
    which clean ``~/.cache`` leave it alone.
    """
    override = os.environ.get("APIGRUNN_ARCHIVE")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "share"
    return root / "apigrunn-archiver" / "archive.db"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _compress(content: bytes) -> bytes:
    # mtime=0 keeps the stored bytes deterministic for identical content.
    return gzip.compress(content, compresslevel=9, mtime=0)


def _decompress(data: bytes, compression: str) -> bytes:
    if compression == "gzip":
        return gzip.decompress(data)
    if compression == "none":
        return data
    raise ValueError(f"unknown blob compression {compression!r}")


class Store:
    """The archive database. Not thread-safe; use one per thread."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else default_db_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if str(self.path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    # ---------------------------------------------------------------- schema

    @property
    def schema_version(self) -> int:
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    def _migrate(self) -> None:
        version = self.schema_version
        if version > SCHEMA_VERSION:
            raise SchemaVersionError(self.path, version, SCHEMA_VERSION)
        for target, script in enumerate(MIGRATIONS[version:], start=version + 1):
            # executescript commits on its own, so wrap each step explicitly to
            # keep a migration and its version bump atomic.
            self._conn.executescript(
                f"BEGIN;\n{script}\nPRAGMA user_version = {target};\nCOMMIT;"
            )

    # ------------------------------------------------------------------ runs

    def start_run(self, tool_version: str) -> int:
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO runs (started_at, tool_version, status) VALUES (?, ?, 'running')",
                (_now(), tool_version),
            )
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE runs SET status = ?, finished_at = ? WHERE id = ?",
                (status, _now(), run_id),
            )

    def runs(self, *, limit: int | None = None) -> list[Run]:
        """Runs newest first, with per-run page, change and error counts."""
        sql = """
            SELECT r.*,
                   COUNT(s.url)                                   AS pages,
                   COALESCE(SUM(s.changed), 0)                    AS changed,
                   COALESCE(SUM(s.error IS NOT NULL), 0)          AS errors
            FROM runs r LEFT JOIN snapshots s ON s.run_id = r.id
            GROUP BY r.id
            ORDER BY r.id DESC
        """
        params: tuple[object, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        return [
            Run(
                id=row["id"],
                started_at=row["started_at"],
                finished_at=row["finished_at"],
                tool_version=row["tool_version"],
                status=row["status"],
                pages=row["pages"],
                changed=row["changed"],
                errors=row["errors"],
            )
            for row in self._conn.execute(sql, params)
        ]

    def run(self, run_id: int) -> Run | None:
        return next((r for r in self.runs() if r.id == run_id), None)

    # ------------------------------------------------------------- snapshots

    def latest_ok(self, url: str) -> Snapshot | None:
        """The most recent snapshot of ``url`` that has a usable body."""
        row = self._conn.execute(
            """
            SELECT * FROM snapshots
            WHERE url = ? AND error IS NULL AND blob_sha256 IS NOT NULL
            ORDER BY fetched_at DESC, run_id DESC LIMIT 1
            """,
            (url,),
        ).fetchone()
        return _snapshot(row) if row else None

    def validators(self, urls: Sequence[str]) -> dict[str, Validators]:
        """ETag/Last-Modified from each URL's latest good snapshot."""
        out: dict[str, Validators] = {}
        for url in urls:
            snap = self.latest_ok(url)
            if snap and (snap.etag or snap.last_modified):
                out[url] = Validators(snap.etag, snap.last_modified)
        return out

    def add_snapshot(self, run_id: int, result: FetchResult) -> tuple[Snapshot, bool]:
        """Record one fetch. Returns the snapshot and whether a new blob was stored.

        ``changed`` is true when a successful fetch's body differs from the
        previous successful body of the same URL (or there was none). A ``304``
        reuses the previous blob; an error keeps its body as evidence but never
        counts as a content change.
        """
        previous = self.latest_ok(result.url)
        new_blob = False

        with self._conn:
            if result.not_modified:
                sha = previous.blob_sha256 if previous else None
                etag = result.header("etag") or (previous.etag if previous else None)
                last_modified = result.header("last-modified") or (
                    previous.last_modified if previous else None
                )
            else:
                sha, new_blob = self._put_blob(result.content) if result.content is not None else (None, False)
                etag = result.header("etag")
                last_modified = result.header("last-modified")

            changed = (
                result.error is None
                and sha is not None
                and (previous is None or previous.blob_sha256 != sha)
            )

            self._conn.execute(
                """
                INSERT INTO snapshots (run_id, url, final_url, fetched_at, status_code, error,
                                       headers, etag, last_modified, blob_sha256, changed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    result.url,
                    result.final_url,
                    result.fetched_at,
                    result.status_code,
                    result.error,
                    json.dumps(result.headers) if result.headers else None,
                    etag,
                    last_modified,
                    sha,
                    int(changed),
                ),
            )

        snapshot = self.snapshot_in_run(run_id, result.url)
        assert snapshot is not None
        return snapshot, new_blob

    def snapshot_in_run(self, run_id: int, url: str) -> Snapshot | None:
        row = self._conn.execute(
            "SELECT * FROM snapshots WHERE run_id = ? AND url = ?", (run_id, url)
        ).fetchone()
        return _snapshot(row) if row else None

    def latest_ok_before(self, url: str, before: str) -> Snapshot | None:
        """Latest good snapshot of ``url`` fetched strictly before ``before`` (ISO)."""
        row = self._conn.execute(
            """
            SELECT * FROM snapshots
            WHERE url = ? AND error IS NULL AND blob_sha256 IS NOT NULL AND fetched_at < ?
            ORDER BY fetched_at DESC, run_id DESC LIMIT 1
            """,
            (url, before),
        ).fetchone()
        return _snapshot(row) if row else None

    def url_history(self, url: str) -> list[Snapshot]:
        """Every snapshot of ``url``, oldest first."""
        rows = self._conn.execute(
            "SELECT * FROM snapshots WHERE url = ? ORDER BY fetched_at, run_id", (url,)
        ).fetchall()
        return [_snapshot(row) for row in rows]

    def latest_run_id(self) -> int | None:
        return self._conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]

    def snapshots(self, run_id: int) -> list[Snapshot]:
        rows = self._conn.execute(
            "SELECT * FROM snapshots WHERE run_id = ? ORDER BY url", (run_id,)
        ).fetchall()
        return [_snapshot(row) for row in rows]

    def headers(self, run_id: int, url: str) -> list[tuple[str, str]]:
        row = self._conn.execute(
            "SELECT headers FROM snapshots WHERE run_id = ? AND url = ?", (run_id, url)
        ).fetchone()
        if not row or not row["headers"]:
            return []
        return [(k, v) for k, v in json.loads(row["headers"])]

    # ----------------------------------------------------------------- blobs

    def _put_blob(self, content: bytes) -> tuple[str, bool]:
        """Store ``content`` unless already present. Caller holds the transaction."""
        sha = hashlib.sha256(content).hexdigest()
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO blobs (sha256, size, compression, data) VALUES (?, ?, ?, ?)",
            (sha, len(content), "gzip", _compress(content)),
        )
        return sha, cursor.rowcount == 1

    def blob(self, sha256: str) -> bytes:
        """Return a blob's original bytes, checking them against their hash."""
        row = self._conn.execute(
            "SELECT size, compression, data FROM blobs WHERE sha256 = ?", (sha256,)
        ).fetchone()
        if row is None:
            raise IntegrityError(f"blob {sha256} is referenced but missing from the archive")
        try:
            content = _decompress(row["data"], row["compression"])
        except (OSError, EOFError, zlib.error, ValueError) as exc:
            raise IntegrityError(f"blob {sha256} cannot be decompressed: {exc}") from exc
        if hashlib.sha256(content).hexdigest() != sha256 or len(content) != row["size"]:
            raise IntegrityError(f"blob {sha256} does not match its hash; the archive is damaged")
        return content

    def blob_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]

    def blob_shas(self) -> Iterator[str]:
        for row in self._conn.execute("SELECT sha256 FROM blobs ORDER BY sha256").fetchall():
            yield row["sha256"]

    def dangling_blob_refs(self) -> list[tuple[int, str, str]]:
        """``(run_id, url, sha256)`` for snapshots whose blob row is missing."""
        rows = self._conn.execute(
            """
            SELECT s.run_id, s.url, s.blob_sha256 FROM snapshots s
            LEFT JOIN blobs b ON b.sha256 = s.blob_sha256
            WHERE s.blob_sha256 IS NOT NULL AND b.sha256 IS NULL
            ORDER BY s.run_id, s.url
            """
        ).fetchall()
        return [(row["run_id"], row["url"], row["blob_sha256"]) for row in rows]

    def integrity_check(self) -> list[str]:
        """SQLite's own page-level check; ``[]`` when the file is sound."""
        rows = self._conn.execute("PRAGMA integrity_check").fetchall()
        messages = [row[0] for row in rows]
        return [] if messages == ["ok"] else messages


def _snapshot(row: sqlite3.Row) -> Snapshot:
    return Snapshot(
        run_id=row["run_id"],
        url=row["url"],
        fetched_at=row["fetched_at"],
        status_code=row["status_code"],
        final_url=row["final_url"],
        error=row["error"],
        etag=row["etag"],
        last_modified=row["last_modified"],
        blob_sha256=row["blob_sha256"],
        changed=bool(row["changed"]),
    )
