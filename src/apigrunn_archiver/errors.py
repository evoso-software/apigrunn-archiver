"""Exceptions raised by :mod:`apigrunn_archiver`.

Every error carries a stable ``code`` prefixed onto its message, so failures
are greppable without matching on prose. Give a new condition a new code
rather than reusing or renumbering an existing one.

=====================  ===============================================
``ARCHIVER-E000``      :class:`ArchiverError` (base, not raised)
``ARCHIVER-E200``      :class:`ParseError`
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


class ParseError(ArchiverError):
    """A page has talk cards but none could be extracted.

    The site's markup changed and the parser needs updating. During a run or
    a reindex this is recorded against the page, not raised: the HTML is
    safely stored and can be re-parsed once the parser is fixed.
    """

    code = "ARCHIVER-E200"


class SchemaVersionError(ArchiverError):
    """The archive was written by a different version of this library.

    While the package is at 0.x there are no migrations: an archive at any
    other schema version is refused and left untouched.
    """

    code = "ARCHIVER-E300"

    def __init__(self, path: Path, found: int, supported: int) -> None:
        self.path = path
        self.found = found
        self.supported = supported
        if found > supported:
            advice = "Upgrade apigrunn-archiver"
        else:
            advice = (
                "Move the file aside to start a new archive, or keep using the "
                "apigrunn-archiver version that wrote it"
            )
        super().__init__(
            f"{path} is at schema version {found}, but this version of "
            f"apigrunn-archiver only understands version {supported}. {advice}; "
            f"the archive has not been modified."
        )


class SnapshotNotFound(ArchiverError):
    """No stored snapshot matches the requested URL and point in time."""

    code = "ARCHIVER-E400"


class IntegrityError(ArchiverError):
    """A stored blob no longer matches the hash it was stored under."""

    code = "ARCHIVER-E500"
