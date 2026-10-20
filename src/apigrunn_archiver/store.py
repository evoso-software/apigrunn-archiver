"""Append-only SQLite store of every run, snapshot, page version and page body.

Nothing in here is ever updated in place or deleted, apart from a run's own
status when it finishes and the parse results, which are derived and can be
rebuilt. A page "changes" only when the talks parsed from it change:

- a *talk version* is one distinct set of talk fields, keyed by their hash;
- a *page version* is the ordered list of talk versions on a page;
- a *blob* (the raw HTML) is stored only for a page version not seen before
  on that URL, or when the page could not be parsed at all.

So a run in which the site only changed a nonce, a build hash or a script
adds one snapshot row per page and nothing else. Such a snapshot points at
the blob that was stored for its page version: the HTML that stands for it,
not necessarily the exact bytes received, which ``body_sha256`` records.

The schema version is kept in ``PRAGMA user_version``. There are no
migrations: an archive from any other schema version is refused untouched.
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
from .parser import ParsedCard

SCHEMA_VERSION = 3

SCHEMA = """
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

-- One row per distinct set of talk fields; sha256 is over those fields.
CREATE TABLE talk_versions (
    sha256        TEXT PRIMARY KEY,
    video_id      TEXT NOT NULL,
    title         TEXT NOT NULL,
    speaker       TEXT NOT NULL,
    description   TEXT NOT NULL,
    url           TEXT NOT NULL,
    year          INTEGER,
    thumbnail_url TEXT
);

-- One row per distinct ordered list of talk versions; sha256 is over the list.
CREATE TABLE page_versions (
    sha256 TEXT PRIMARY KEY,
    cards  INTEGER NOT NULL
);

CREATE TABLE page_talks (
    page_sha256 TEXT NOT NULL REFERENCES page_versions(sha256),
    position    INTEGER NOT NULL,
    talk_sha256 TEXT NOT NULL REFERENCES talk_versions(sha256),
    PRIMARY KEY (page_sha256, position)
);

CREATE INDEX idx_page_talks_talk ON page_talks(talk_sha256);

-- What the parser made of a stored blob: a page version, or an error.
CREATE TABLE blob_parses (
    blob_sha256    TEXT NOT NULL REFERENCES blobs(sha256),
    url            TEXT NOT NULL,
    parser_version INTEGER NOT NULL,
    page_sha256    TEXT REFERENCES page_versions(sha256),
    error          TEXT,
    PRIMARY KEY (blob_sha256, url)
);

CREATE INDEX idx_blob_parses_page ON blob_parses(url, page_sha256);

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
    body_sha256   TEXT,
    blob_sha256   TEXT REFERENCES blobs(sha256),
    PRIMARY KEY (run_id, url)
);

CREATE INDEX idx_snapshots_url ON snapshots(url, fetched_at);

-- Snapshots with their page version and whether it differs from the previous
-- good snapshot of the same URL. Computed rather than stored, so a reindex
-- with a new parser corrects it everywhere. A page that could not be parsed
-- is compared by its blob instead.
CREATE VIEW snapshot_states AS
WITH good AS (
    SELECT s.run_id, s.url, p.page_sha256,
           COALESCE(p.page_sha256, 'blob:' || s.blob_sha256) AS content_key,
           LAG(COALESCE(p.page_sha256, 'blob:' || s.blob_sha256))
               OVER (PARTITION BY s.url ORDER BY s.fetched_at, s.run_id) AS previous_key
    FROM snapshots s
    LEFT JOIN blob_parses p ON p.blob_sha256 = s.blob_sha256 AND p.url = s.url
    WHERE s.error IS NULL AND s.blob_sha256 IS NOT NULL
)
SELECT s.*, g.page_sha256,
       CASE WHEN g.run_id IS NULL THEN 0
            ELSE g.previous_key IS NULL OR g.previous_key != g.content_key END AS changed
FROM snapshots s
LEFT JOIN good g ON g.run_id = s.run_id AND g.url = s.url;
"""


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
        try:
            self._create_or_check()
        except BaseException:
            self._conn.close()
            raise

    def close(self) -> None:
        self._conn.close()

    # ---------------------------------------------------------------- schema

    @property
    def schema_version(self) -> int:
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    def _create_or_check(self) -> None:
        version = self.schema_version
        if version == SCHEMA_VERSION:
            return
        if version != 0:
            raise SchemaVersionError(self.path, version, SCHEMA_VERSION)
        # executescript commits on its own, so wrap it explicitly to keep the
        # schema and its version number atomic.
        self._conn.executescript(
            f"BEGIN;\n{SCHEMA}\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;"
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
            FROM runs r LEFT JOIN snapshot_states s ON s.run_id = r.id
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
            SELECT * FROM snapshot_states
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

    def add_snapshot(
        self,
        run_id: int,
        result: FetchResult,
        *,
        parser_version: int,
        cards: Sequence[ParsedCard] | None = None,
        parse_error: str | None = None,
    ) -> tuple[Snapshot, bool]:
        """Record one fetch. Returns the snapshot and whether a new blob was stored.

        For a successful fetch, pass what the parser made of the body: its
        ``cards``, or the ``parse_error``. If this URL already has a blob for
        the same page version, the snapshot points at that blob and the body
        itself is not stored. An unparseable body is always stored, as
        evidence for a fixed parser to try again.

        A ``304`` reuses the previous blob. An error response stores no body.
        """
        new_blob = False
        sha = None
        body_sha = None
        etag = result.header("etag")
        last_modified = result.header("last-modified")

        with self._conn:
            if result.not_modified:
                previous = self.latest_ok(result.url)
                if previous is not None:
                    sha, body_sha = previous.blob_sha256, previous.body_sha256
                    etag = etag or previous.etag
                    last_modified = last_modified or previous.last_modified
            elif result.content is not None:
                body_sha = hashlib.sha256(result.content).hexdigest()

            if result.error is None and result.content is not None:
                page = None
                if parse_error is None:
                    page = self._put_page_version(cards or ())
                    sha = self._blob_for(result.url, page, parser_version)
                if sha is None:
                    sha, new_blob = self._put_blob(result.content)
                    self._save_parse(sha, result.url, parser_version, page, parse_error)

            self._conn.execute(
                """
                INSERT INTO snapshots (run_id, url, final_url, fetched_at, status_code, error,
                                       headers, etag, last_modified, body_sha256, blob_sha256)
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
                    body_sha,
                    sha,
                ),
            )

        snapshot = self.snapshot_in_run(run_id, result.url)
        assert snapshot is not None
        return snapshot, new_blob

    def snapshot_in_run(self, run_id: int, url: str) -> Snapshot | None:
        row = self._conn.execute(
            "SELECT * FROM snapshot_states WHERE run_id = ? AND url = ?", (run_id, url)
        ).fetchone()
        return _snapshot(row) if row else None

    def latest_ok_before(self, url: str, before: str) -> Snapshot | None:
        """Latest good snapshot of ``url`` fetched strictly before ``before`` (ISO)."""
        row = self._conn.execute(
            """
            SELECT * FROM snapshot_states
            WHERE url = ? AND error IS NULL AND blob_sha256 IS NOT NULL AND fetched_at < ?
            ORDER BY fetched_at DESC, run_id DESC LIMIT 1
            """,
            (url, before),
        ).fetchone()
        return _snapshot(row) if row else None

    def url_history(self, url: str) -> list[Snapshot]:
        """Every snapshot of ``url``, oldest first."""
        rows = self._conn.execute(
            "SELECT * FROM snapshot_states WHERE url = ? ORDER BY fetched_at, run_id", (url,)
        ).fetchall()
        return [_snapshot(row) for row in rows]

    def latest_run_id(self) -> int | None:
        return self._conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]

    def snapshots(self, run_id: int) -> list[Snapshot]:
        rows = self._conn.execute(
            "SELECT * FROM snapshot_states WHERE run_id = ? ORDER BY url", (run_id,)
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

    def bad_index_rows(self) -> list[tuple[str, str]]:
        """``(sha256, reason)`` for talk and page versions that do not match
        their hash, or that reference rows which are missing."""
        bad: list[tuple[str, str]] = []
        for row in self._conn.execute("SELECT * FROM talk_versions ORDER BY sha256"):
            if _talk_sha(row) != row["sha256"]:
                bad.append((row["sha256"], "talk version does not match its hash"))
        pages = {
            row["sha256"]: (row["cards"], [])
            for row in self._conn.execute("SELECT sha256, cards FROM page_versions")
        }
        for row in self._conn.execute(
            """
            SELECT pt.page_sha256, pt.talk_sha256, t.sha256 AS found
            FROM page_talks pt LEFT JOIN talk_versions t ON t.sha256 = pt.talk_sha256
            ORDER BY pt.page_sha256, pt.position
            """
        ):
            if row["found"] is None:
                bad.append((row["page_sha256"], f"page version lists missing talk {row['talk_sha256']}"))
            if row["page_sha256"] in pages:
                pages[row["page_sha256"]][1].append(row["talk_sha256"])
        for sha, (cards, talks) in sorted(pages.items()):
            if len(talks) != cards:
                bad.append((sha, f"page version has {len(talks)} of its {cards} talks"))
            elif _page_sha(talks) != sha:
                bad.append((sha, "page version does not match its hash"))
        for row in self._conn.execute(
            """
            SELECT p.blob_sha256, p.page_sha256 FROM blob_parses p
            LEFT JOIN page_versions v ON v.sha256 = p.page_sha256
            WHERE p.page_sha256 IS NOT NULL AND v.sha256 IS NULL
            """
        ):
            bad.append((row["blob_sha256"], f"parse points at missing page version {row['page_sha256']}"))
        return bad

    def integrity_check(self) -> list[str]:
        """SQLite's own page-level check; ``[]`` when the file is sound."""
        rows = self._conn.execute("PRAGMA integrity_check").fetchall()
        messages = [row[0] for row in rows]
        return [] if messages == ["ok"] else messages

    # ------------------------------------------------- page and talk versions

    def _put_page_version(self, cards: Sequence[ParsedCard]) -> str:
        """Store a page version and its talk versions unless already present.
        Caller holds the transaction."""
        talk_shas = []
        for card in cards:
            sha = _talk_sha(card)
            talk_shas.append(sha)
            self._conn.execute(
                """
                INSERT OR IGNORE INTO talk_versions (sha256, video_id, title, speaker,
                                                     description, url, year, thumbnail_url)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (sha, card.video_id, card.title, card.speaker, card.description, card.url,
                 card.year, card.thumbnail_url),
            )
        page = _page_sha(talk_shas)
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO page_versions (sha256, cards) VALUES (?, ?)",
            (page, len(talk_shas)),
        )
        if cursor.rowcount == 1:
            self._conn.executemany(
                "INSERT INTO page_talks (page_sha256, position, talk_sha256) VALUES (?, ?, ?)",
                [(page, i, sha) for i, sha in enumerate(talk_shas)],
            )
        return page

    def _blob_for(self, url: str, page_sha256: str, parser_version: int) -> str | None:
        """A blob already stored for ``url`` that this parser reads as ``page_sha256``."""
        row = self._conn.execute(
            """
            SELECT blob_sha256 FROM blob_parses
            WHERE url = ? AND page_sha256 = ? AND parser_version = ?
            LIMIT 1
            """,
            (url, page_sha256, parser_version),
        ).fetchone()
        return row["blob_sha256"] if row else None

    def _save_parse(
        self,
        blob_sha256: str,
        url: str,
        parser_version: int,
        page_sha256: str | None,
        error: str | None,
    ) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO blob_parses (blob_sha256, url, parser_version, page_sha256, error)
            VALUES (?, ?, ?, ?, ?)
            """,
            (blob_sha256, url, parser_version, page_sha256, error),
        )

    # ------------------------------------------------------------ talk index

    def unparsed_blobs(self, parser_version: int) -> list[tuple[str, str, str]]:
        """``(sha256, url, content_type)`` for stored page bodies not yet parsed
        by ``parser_version``."""
        rows = self._conn.execute(
            """
            SELECT s.blob_sha256 AS sha, s.url AS url, MIN(s.run_id) AS run_id
            FROM snapshots s
            LEFT JOIN blob_parses p
                   ON p.blob_sha256 = s.blob_sha256 AND p.url = s.url
                  AND p.parser_version = ?
            WHERE s.error IS NULL AND s.blob_sha256 IS NOT NULL AND p.url IS NULL
            GROUP BY s.blob_sha256, s.url
            ORDER BY run_id, s.url
            """,
            (parser_version,),
        ).fetchall()
        out = []
        for row in rows:
            content_type = next(
                (v for k, v in self.headers(row["run_id"], row["url"]) if k.lower() == "content-type"),
                "",
            )
            out.append((row["sha"], row["url"], content_type))
        return out

    def save_parse(
        self,
        sha256: str,
        url: str,
        *,
        parser_version: int,
        cards: Sequence[ParsedCard] | None,
        error: str | None,
    ) -> None:
        """Replace the parse result for one stored page body."""
        with self._conn:
            page = self._put_page_version(cards or ()) if error is None else None
            self._save_parse(sha256, url, parser_version, page, error)

    def clear_index(self) -> None:
        """Forget every parse result; the page and talk versions are rebuilt
        from the blobs by the next reindex."""
        with self._conn:
            self._conn.execute("DELETE FROM blob_parses")
            self._conn.execute("DELETE FROM page_talks")
            self._conn.execute("DELETE FROM page_versions")
            self._conn.execute("DELETE FROM talk_versions")

    def parsed_snapshots(self) -> list[tuple[int, str, str | None, str | None]]:
        """``(run_id, url, page_sha256, parse_error)`` for every good, parsed snapshot."""
        rows = self._conn.execute(
            """
            SELECT s.run_id, s.url, p.page_sha256, p.error
            FROM snapshots s
            JOIN blob_parses p ON p.blob_sha256 = s.blob_sha256 AND p.url = s.url
            WHERE s.error IS NULL
            ORDER BY s.run_id, s.url
            """
        ).fetchall()
        return [(r["run_id"], r["url"], r["page_sha256"], r["error"]) for r in rows]

    def cards(self, page_sha256: str) -> list[sqlite3.Row]:
        """The talk versions on a page version, in page order."""
        return self._conn.execute(
            """
            SELECT t.video_id, t.title, t.speaker, t.description, t.url AS talk_url,
                   t.year, t.thumbnail_url
            FROM page_talks pt JOIN talk_versions t ON t.sha256 = pt.talk_sha256
            WHERE pt.page_sha256 = ?
            ORDER BY pt.position
            """,
            (page_sha256,),
        ).fetchall()

    def sightings(self) -> list[sqlite3.Row]:
        """Per talk: the first and last run any good snapshot listed it in."""
        return self._conn.execute(
            """
            SELECT t.video_id, MIN(s.run_id) AS first_run, MAX(s.run_id) AS last_run
            FROM snapshots s
            JOIN blob_parses p ON p.blob_sha256 = s.blob_sha256 AND p.url = s.url
            JOIN page_talks pt ON pt.page_sha256 = p.page_sha256
            JOIN talk_versions t ON t.sha256 = pt.talk_sha256
            WHERE s.error IS NULL
            GROUP BY t.video_id
            """
        ).fetchall()

    def previous_run_id(self, run_id: int) -> int | None:
        return self._conn.execute(
            "SELECT MAX(id) FROM runs WHERE id < ?", (run_id,)
        ).fetchone()[0]


_TALK_FIELDS = ("video_id", "title", "speaker", "description", "url", "year", "thumbnail_url")


def _talk_sha(talk: ParsedCard | sqlite3.Row) -> str:
    """Hash of the fields that make up a talk version; the track is not one."""
    get = talk.__getitem__ if isinstance(talk, sqlite3.Row) else talk.__getattribute__
    canonical = json.dumps([get(name) for name in _TALK_FIELDS], ensure_ascii=False,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _page_sha(talk_shas: Sequence[str]) -> str:
    return hashlib.sha256("\n".join(talk_shas).encode("ascii")).hexdigest()


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
        body_sha256=row["body_sha256"],
        blob_sha256=row["blob_sha256"],
        page_sha256=row["page_sha256"],
        changed=bool(row["changed"]),
    )
