"""Plain records returned by the public API."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Run:
    """One archiving pass over every target page."""

    id: int
    started_at: str
    finished_at: str | None
    tool_version: str
    status: str  # running | ok | partial | failed
    pages: int = 0
    changed: int = 0
    errors: int = 0


@dataclass(frozen=True, slots=True)
class Snapshot:
    """What one URL looked like during one run."""

    run_id: int
    url: str
    fetched_at: str
    status_code: int | None
    final_url: str | None
    error: str | None
    etag: str | None
    last_modified: str | None
    blob_sha256: str | None
    changed: bool

    @property
    def ok(self) -> bool:
        return self.error is None and self.status_code is not None


@dataclass(frozen=True, slots=True)
class RunResult:
    """Summary of a finished :meth:`Archiver.run`."""

    run_id: int
    status: str
    fetched: int
    changed: int
    new_blobs: int
    errors: int
    snapshots: list[Snapshot]

    def __str__(self) -> str:
        return (
            f"run {self.run_id}: {self.status} — {self.fetched} pages, "
            f"{self.changed} changed, {self.new_blobs} new blobs, {self.errors} errors"
        )


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """A moment a page's archived state changed.

    ``kind`` is one of:

    - ``first``: the first good copy of the page
    - ``changed``: the body differs from the previous good copy
    - ``failed``: the page started erroring (or never worked)
    - ``recovered``: a good copy again after failures; check
      ``snapshot.changed`` to see whether the body differs from before
    """

    kind: str
    snapshot: Snapshot


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """Outcome of :meth:`Archiver.verify`."""

    blobs_checked: int
    #: ``(sha256, reason)`` for each blob that failed its check.
    bad_blobs: list[tuple[str, str]]
    #: ``(run_id, url, sha256)`` for snapshots pointing at a missing blob.
    dangling: list[tuple[int, str, str]]
    #: Messages from SQLite's ``PRAGMA integrity_check``; empty when sound.
    sqlite_errors: list[str]

    @property
    def ok(self) -> bool:
        return not (self.bad_blobs or self.dangling or self.sqlite_errors)

    def __str__(self) -> str:
        if self.ok:
            return f"ok — {self.blobs_checked} blobs verified"
        return (
            f"DAMAGED — {self.blobs_checked} blobs checked, {len(self.bad_blobs)} bad, "
            f"{len(self.dangling)} dangling references, "
            f"{len(self.sqlite_errors)} SQLite errors"
        )


@dataclass(frozen=True, slots=True)
class ExportResult:
    """Outcome of :meth:`Archiver.export`."""

    runs: list[int]
    files: int
    #: Snapshots listed in a manifest but without a body to write (errors).
    skipped: int
