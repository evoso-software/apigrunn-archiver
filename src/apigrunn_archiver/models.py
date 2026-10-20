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
