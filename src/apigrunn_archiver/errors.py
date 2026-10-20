"""Exceptions raised by :mod:`apigrunn_archiver`.

Every error carries a stable ``code`` prefixed onto its message, so failures
are greppable without matching on prose. Give a new condition a new code
rather than reusing or renumbering an existing one.

=====================  ===============================================
``ARCHIVER-E000``      :class:`ArchiverError` (base, not raised)
``ARCHIVER-E300``      :class:`SchemaVersionError`
``ARCHIVER-E400``      :class:`SnapshotNotFound`
``ARCHIVER-E500``      :class:`IntegrityError`
=====================  ===============================================

Fetch failures are deliberately *not* exceptions: a page that errors is
recorded as a snapshot, because "this page started failing" is exactly what an
archive should remember.
"""

from __future__ import annotations

from pathlib import Path


class ArchiverError(Exception):
    """Base class for every error raised by this library."""

    code = "ARCHIVER-E000"

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(f"[{self.code}] {message}")


class SchemaVersionError(ArchiverError):
    """The archive was written by a newer version of this library.

    Older archives are migrated forward automatically; only a file from the
    future is refused, since downgrading could lose data.
    """

    code = "ARCHIVER-E300"

    def __init__(self, path: Path, found: int, supported: int) -> None:
        self.path = path
        self.found = found
        self.supported = supported
        super().__init__(
            f"{path} is at schema version {found}, but this version of "
            f"apigrunn-archiver only understands up to {supported}. Upgrade "
            f"apigrunn-archiver; the archive has not been modified."
        )


class SnapshotNotFound(ArchiverError):
    """No stored snapshot matches the requested URL and point in time."""

    code = "ARCHIVER-E400"


class IntegrityError(ArchiverError):
    """A stored blob no longer matches the hash it was stored under."""

    code = "ARCHIVER-E500"
