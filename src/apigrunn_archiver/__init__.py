"""A versioned local archive of the aiGrunn website.

    >>> from apigrunn_archiver import Archiver
    >>> with Archiver() as archiver:
    ...     print(archiver.run())
    ...     html = archiver.html("tech", at="2026-03-01")

Every run is kept. Page bodies are stored verbatim and deduplicated by
SHA-256, so the archive survives the site deleting or rewriting its pages.
"""

from ._version import __version__
from .archiver import Archiver
from .errors import (
    ArchiverError,
    IntegrityError,
    ParseError,
    SchemaVersionError,
    SnapshotNotFound,
)
from .models import (
    ArchivedTalk,
    ExportResult,
    HistoryEntry,
    IndexResult,
    Run,
    RunResult,
    Snapshot,
    Talk,
    TalkChanges,
    VerifyResult,
)
from .parser import PARSER_VERSION, ParsedCard, parse_track_page
from .store import SCHEMA_VERSION, Store, default_db_path
from .targets import BASE_URL, TRACKS, default_targets, track_url

__all__ = [
    "BASE_URL",
    "PARSER_VERSION",
    "SCHEMA_VERSION",
    "TRACKS",
    "ArchivedTalk",
    "Archiver",
    "ArchiverError",
    "ExportResult",
    "HistoryEntry",
    "IndexResult",
    "IntegrityError",
    "ParseError",
    "ParsedCard",
    "Run",
    "RunResult",
    "SchemaVersionError",
    "Snapshot",
    "SnapshotNotFound",
    "Store",
    "Talk",
    "TalkChanges",
    "VerifyResult",
    "__version__",
    "default_db_path",
    "default_targets",
    "parse_track_page",
    "track_url",
]
