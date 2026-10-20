"""Plain records returned by the public API."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_SPEAKER_SPLIT = re.compile(r"\s*(?:&|,| and )\s*")


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
class Talk:
    """A talk as listed on the track pages at some point in the archive.

    ``tracks`` holds the slugs of every track page that listed it.
    """

    video_id: str
    title: str
    speaker: str
    description: str
    url: str
    year: int | None
    tracks: tuple[str, ...]
    thumbnail_url: str | None

    @property
    def speakers(self) -> list[str]:
        """``speaker`` split into names, best effort ("A & B", "A, B and C")."""
        if not self.speaker.strip():
            return []
        return [p for p in (p.strip() for p in _SPEAKER_SPLIT.split(self.speaker)) if p]

    def __str__(self) -> str:
        year = self.year or "????"
        return f"[{year}] {self.title}" + (f" — {self.speaker}" if self.speaker else "")


@dataclass(frozen=True, slots=True)
class TalkChanges:
    """How the talk archive differs between two runs.

    Each run is read as "every page's latest good copy as of that run", so a
    page that fails in ``to_run`` keeps its earlier talks rather than making
    them look deleted. Such pages are listed in ``stale_pages``.
    """

    from_run: int
    to_run: int
    added: list[Talk] = field(default_factory=list)
    removed: list[Talk] = field(default_factory=list)
    #: ``(before, after)`` for talks whose details or tracks changed.
    changed: list[tuple[Talk, Talk]] = field(default_factory=list)
    #: Pages in ``to_run`` that failed or could not be parsed.
    stale_pages: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed)

    def __str__(self) -> str:
        text = (
            f"talks run {self.from_run} → {self.to_run}: +{len(self.added)} added, "
            f"-{len(self.removed)} removed, ~{len(self.changed)} changed"
        )
        if self.stale_pages:
            text += f" ({len(self.stale_pages)} pages stale)"
        return text


@dataclass(frozen=True, slots=True)
class ArchivedTalk:
    """A talk the archive has ever seen, and whether the site still lists it."""

    talk: Talk  # the latest version seen
    first_seen_run: int
    last_seen_run: int
    present: bool


@dataclass(frozen=True, slots=True)
class IndexResult:
    """Outcome of (re)building the talk index."""

    parsed: int
    #: ``(url, sha256, message)`` for page versions that could not be parsed.
    errors: list[tuple[str, str, str]] = field(default_factory=list)


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
    #: Talk changes since the previous run; ``None`` for the first run.
    talk_changes: TalkChanges | None = None
    #: Pages fetched this run whose HTML could not be parsed.
    parse_errors: list[tuple[str, str, str]] = field(default_factory=list)

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
