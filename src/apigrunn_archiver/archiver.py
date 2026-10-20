"""The public entry point: :class:`Archiver`."""

from __future__ import annotations

import dataclasses
import difflib
import json
import logging
import os
import re
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Self
from urllib.parse import urlsplit

import httpx

from . import index
from ._version import __version__
from .errors import IntegrityError, SnapshotNotFound
from .fetcher import DEFAULT_TIMEOUT, build_client, iter_fetch
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
from .store import Store
from .targets import default_targets, resolve

logger = logging.getLogger(__name__)

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


class Archiver:
    """Take snapshots of aigrunn.org and read them back.

        >>> with Archiver() as archiver:
        ...     print(archiver.run())
        ...     html = archiver.html("tech", at="2026-03-01")
    """

    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        targets: Sequence[str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._store = Store(db_path)
        self._targets = list(targets) if targets is not None else default_targets()
        self._timeout = timeout
        self._transport = transport

    @property
    def db_path(self) -> Path:
        return self._store.path

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ run

    def run(
        self,
        *,
        conditional: bool = False,
        on_snapshot: Callable[[Snapshot], None] | None = None,
    ) -> RunResult:
        """Fetch every target page and record the result as a new run.

        By default every page is downloaded in full and parsed. Its HTML is
        stored only if its talks differ from every copy of the page already
        stored (or it cannot be parsed); otherwise the snapshot points at the
        stored copy with the same talks. ``conditional=True`` sends
        ``If-None-Match``/``If-Modified-Since`` from the previous snapshot
        instead, trusting the server to report changes.

        ``on_snapshot`` is called with each snapshot as soon as it is stored,
        in target order, error snapshots included. If it raises, the run stops,
        keeps the pages stored so far, is marked ``failed`` and the exception
        propagates; that is how a caller cancels a run.

        Never raises for network or HTTP failures: those become error
        snapshots and set the run's status to ``partial`` or ``failed``.
        """
        run_id = self._store.start_run(__version__)
        validators = self._store.validators(self._targets) if conditional else None

        snapshots: list[Snapshot] = []
        new_blobs = 0
        parse_errors: list[tuple[str, str, str]] = []
        try:
            with build_client(timeout=self._timeout, transport=self._transport) as client:
                for result in iter_fetch(self._targets, client=client, validators=validators):
                    cards, parse_error = [], None
                    if result.error is None and result.content is not None:
                        cards, parse_error = index.parse_page(
                            result.content, result.header("content-type") or "", result.url
                        )
                    snapshot, added = self._store.add_snapshot(
                        run_id,
                        result,
                        parser_version=index.PARSER_VERSION,
                        cards=cards,
                        parse_error=parse_error,
                    )
                    if parse_error is not None:
                        parse_errors.append((result.url, snapshot.blob_sha256, parse_error))
                    snapshots.append(snapshot)
                    new_blobs += added
                    if on_snapshot is not None:
                        on_snapshot(snapshot)
        except BaseException:
            # Keep whatever was stored, but don't leave the run marked running.
            self._store.finish_run(run_id, "failed")
            raise

        errors = sum(1 for s in snapshots if not s.ok)
        if errors == 0:
            status = "ok"
        elif errors == len(snapshots):
            status = "failed"
        else:
            status = "partial"
        self._store.finish_run(run_id, status)

        # Normally a no-op; catches up blobs parsed by an older parser.
        indexed = index.ensure_index(self._store)
        previous = self._store.previous_run_id(run_id)
        changes = index.talk_changes(self._store, previous, run_id) if previous else None

        result = RunResult(
            run_id=run_id,
            status=status,
            fetched=len(snapshots),
            changed=sum(1 for s in snapshots if s.changed),
            new_blobs=new_blobs,
            errors=errors,
            snapshots=snapshots,
            talk_changes=changes,
            parse_errors=parse_errors + indexed.errors,
        )
        logger.info("%s", result)
        return result

    # ---------------------------------------------------------------- reads

    def runs(self, *, limit: int | None = None) -> list[Run]:
        """Every run, newest first."""
        return self._store.runs(limit=limit)

    def snapshots(self, run_id: int) -> list[Snapshot]:
        """Every snapshot taken during one run."""
        return self._store.snapshots(run_id)

    def snapshot(
        self,
        url: str,
        *,
        at: str | date | datetime | None = None,
        run_id: int | None = None,
    ) -> Snapshot:
        """Find the snapshot of ``url`` (or a track slug) to read.

        With ``run_id``: the snapshot from that run, even if it was an error.
        With ``at``: the latest good snapshot taken at or before that moment;
        a bare date means "by the end of that day" (UTC).
        Otherwise: the latest good snapshot.
        """
        url = resolve(url)
        if run_id is not None and at is not None:
            raise ValueError("pass at most one of 'at' and 'run_id'")

        if run_id is not None:
            snap = self._store.snapshot_in_run(run_id, url)
            where = f"in run {run_id}"
        elif at is not None:
            bound = _exclusive_bound(at)
            snap = self._store.latest_ok_before(url, bound)
            where = f"before {bound}"
        else:
            snap = self._store.latest_ok(url)
            where = "in any run"

        if snap is None:
            raise SnapshotNotFound(f"no snapshot of {url} {where}")
        return snap

    def content(self, url: str, **kwargs: object) -> bytes:
        """Raw bytes of the HTML stored for a snapshot.

        That is exactly what the server sent when ``snapshot.exact``; otherwise
        it is an earlier response with the same talks.
        """
        snap = self.snapshot(url, **kwargs)  # type: ignore[arg-type]
        if snap.blob_sha256 is None:
            raise SnapshotNotFound(
                f"snapshot of {snap.url} in run {snap.run_id} has no body ({snap.error})"
            )
        return self._store.blob(snap.blob_sha256)

    def html(self, url: str, **kwargs: object) -> str:
        """:meth:`content` decoded to text, using the charset the server declared."""
        snap = self.snapshot(url, **kwargs)  # type: ignore[arg-type]
        raw = self.content(snap.url, run_id=snap.run_id)
        content_type = next(
            (v for k, v in self._store.headers(snap.run_id, snap.url) if k.lower() == "content-type"),
            "",
        )
        return index.decode_html(raw, content_type)

    # ---------------------------------------------------------------- talks

    def reindex(self, *, force: bool = False) -> IndexResult:
        """Parse archived pages into the talk index.

        Runs do this automatically, so this is only needed after upgrading to
        a version with a changed parser (which it detects by itself) or, with
        ``force=True``, to rebuild the index from scratch.
        """
        return index.ensure_index(self._store, force=force)

    def talks(
        self,
        *,
        run_id: int | None = None,
        year: int | None = None,
        track: str | None = None,
        search: str | None = None,
    ) -> list[Talk]:
        """The talks the site listed as of a run (default: the latest run).

        Each page contributes its latest good copy as of that run, so a page
        that failed in the run still contributes its earlier talks.
        """
        run_id = self._resolve_run(run_id)
        if run_id is None:
            return []
        talks = index.talks_in_run(self._store, run_id).values()
        if year is not None:
            talks = [t for t in talks if t.year == year]
        if track is not None:
            talks = [t for t in talks if track in t.tracks]
        if search:
            needle = search.casefold()
            talks = [
                t
                for t in talks
                if needle in t.title.casefold()
                or needle in t.description.casefold()
                or needle in t.speaker.casefold()
            ]
        return index.sort_talks(talks)

    def talk_changes(self, from_run: int, to_run: int | None = None) -> TalkChanges:
        """Talks added, removed and edited between two runs (default: up to latest)."""
        target = self._resolve_run(to_run)
        for rid in (from_run, target):
            if rid is None or self._store.run(rid) is None:
                raise SnapshotNotFound(
                    "the archive has no runs" if rid is None else f"no run {rid}"
                )
        return index.talk_changes(self._store, from_run, target)

    def archived_talks(self, *, gone_only: bool = False) -> list[ArchivedTalk]:
        """Every talk ever seen. ``gone_only`` keeps those the site no longer lists."""
        self._ensure_index()
        talks = index.archived_talks(self._store)
        return [t for t in talks if not t.present] if gone_only else talks

    def _ensure_index(self) -> None:
        # Cheap when up to date: one query finds nothing to parse.
        index.ensure_index(self._store)

    def _resolve_run(self, run_id: int | None) -> int | None:
        self._ensure_index()
        if run_id is None:
            return self._store.latest_run_id()
        if self._store.run(run_id) is None:
            raise SnapshotNotFound(f"no run {run_id}")
        return run_id

    def history(self, url: str, *, everything: bool = False) -> list[HistoryEntry]:
        """The moments ``url``'s archived state changed, oldest first.

        Only transitions are reported: the first good copy, each change of
        talks (or new copy that could not be parsed), the start of a run of
        failures and the recovery from one.
        ``everything=True`` reports every snapshot instead, with ``kind`` set
        to ``same`` or ``failing`` for the ones that would otherwise be hidden.
        """
        url = resolve(url)
        self._ensure_index()
        entries: list[HistoryEntry] = []
        seen_good = False
        failing = False
        for snap in self._store.url_history(url):
            if snap.ok:
                if not seen_good:
                    kind = "first"
                elif failing:
                    kind = "recovered"
                elif snap.changed:
                    kind = "changed" if snap.page_sha256 is not None else "unparseable"
                else:
                    kind = "same"
                seen_good, failing = True, False
            else:
                kind = "failing" if failing else "failed"
                failing = True
            if everything or kind not in ("same", "failing"):
                entries.append(HistoryEntry(kind=kind, snapshot=snap))
        return entries

    def diff(
        self,
        url: str,
        run_a: int,
        run_b: int | None = None,
        *,
        html: bool = False,
        context: int = 3,
    ) -> str:
        """What changed on a page between two runs; ``""`` if nothing did.

        By default the talks are compared, one line per talk added (``+``),
        removed (``-``) or edited (``~``). ``html=True`` gives a unified diff of
        the stored HTML instead, as does a page that could not be parsed.
        ``run_b`` defaults to the latest good snapshot.
        """
        self._ensure_index()
        a = self.snapshot(url, run_id=run_a)
        b = self.snapshot(url) if run_b is None else self.snapshot(url, run_id=run_b)
        fromfile = f"{a.url} (run {a.run_id}, {a.fetched_at})"
        tofile = f"{b.url} (run {b.run_id}, {b.fetched_at})"

        if not html and a.page_sha256 is not None and b.page_sha256 is not None:
            if a.page_sha256 == b.page_sha256:
                return ""
            before = index.page_talks(self._store, a.page_sha256, a.url)
            after = index.page_talks(self._store, b.page_sha256, b.url)
            lines = [f"--- {fromfile}\n", f"+++ {tofile}\n"]
            lines += [f"+ {after[v]}\n" for v in _by_talk(after, after.keys() - before.keys())]
            lines += [f"- {before[v]}\n" for v in _by_talk(before, before.keys() - after.keys())]
            for v in _by_talk(after, before.keys() & after.keys()):
                fields = changed_fields(before[v], after[v])
                if fields:
                    lines.append(f"~ {after[v]}  ({', '.join(fields)} changed)\n")
            # Same talks, different order.
            if len(lines) == 2:
                lines.append("~ talks reordered\n")
            return "".join(lines)

        if a.blob_sha256 == b.blob_sha256:
            return ""
        diff = difflib.unified_diff(
            self.html(a.url, run_id=a.run_id).splitlines(keepends=True),
            self.html(b.url, run_id=b.run_id).splitlines(keepends=True),
            fromfile=fromfile,
            tofile=tofile,
            n=context,
        )
        return "".join(line if line.endswith("\n") else line + "\n" for line in diff)

    def verify(self) -> VerifyResult:
        """Re-hash every stored body and check the database file itself."""
        bad: list[tuple[str, str]] = []
        checked = 0
        for sha in self._store.blob_shas():
            checked += 1
            try:
                self._store.blob(sha)
            except IntegrityError as exc:
                bad.append((sha, exc.message))
        return VerifyResult(
            blobs_checked=checked,
            bad_blobs=bad,
            dangling=self._store.dangling_blob_refs(),
            sqlite_errors=self._store.integrity_check(),
            bad_index=self._store.bad_index_rows(),
        )

    def export(
        self, directory: str | Path, *, run_id: int | None = None, all_runs: bool = False
    ) -> ExportResult:
        """Write runs out as plain files that need nothing but a browser.

        Each run becomes ``run-NNNN/`` holding one ``<host>/<path>.html`` per
        good snapshot, with the stored HTML (see :meth:`content`) and the fetch
        time as the file's mtime, plus a ``manifest.json`` describing every
        snapshot of the run, failed ones included; its ``exact`` field tells
        whether a file is byte for byte what the server sent in that run.
        Defaults to the latest run.
        """
        if run_id is not None and all_runs:
            raise ValueError("pass at most one of 'run_id' and 'all_runs'")
        if all_runs:
            run_ids = sorted(r.id for r in self._store.runs())
        else:
            target = run_id if run_id is not None else self._store.latest_run_id()
            if target is None or self._store.run(target) is None:
                raise SnapshotNotFound(
                    "the archive has no runs" if target is None else f"no run {target}"
                )
            run_ids = [target]

        root = Path(directory)
        files = skipped = 0
        for rid in run_ids:
            run = self._store.run(rid)
            assert run is not None
            run_dir = root / f"run-{rid:04d}"
            manifest: list[dict[str, object]] = []
            for snap in self._store.snapshots(rid):
                entry = dataclasses.asdict(snap)
                entry["exact"] = snap.exact
                entry["headers"] = self._store.headers(rid, snap.url)
                entry["file"] = None
                if snap.ok and snap.blob_sha256 is not None:
                    rel = _url_to_relpath(snap.url)
                    path = run_dir / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(self._store.blob(snap.blob_sha256))
                    mtime = datetime.fromisoformat(snap.fetched_at).timestamp()
                    os.utime(path, (mtime, mtime))
                    entry["file"] = rel.as_posix()
                    files += 1
                else:
                    skipped += 1
                manifest.append(entry)
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "manifest.json").write_text(
                json.dumps(
                    {"run": dataclasses.asdict(run), "snapshots": manifest},
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
        return ExportResult(runs=run_ids, files=files, skipped=skipped)


_TALK_FIELDS = ("title", "speaker", "description", "url", "year", "tracks", "thumbnail_url")


def changed_fields(before: Talk, after: Talk) -> list[str]:
    """Names of the fields that differ between two versions of a talk."""
    return [name for name in _TALK_FIELDS if getattr(before, name) != getattr(after, name)]


def _by_talk(talks: dict[str, Talk], ids) -> list[str]:
    return [t.video_id for t in index.sort_talks(talks[v] for v in ids)]


def _url_to_relpath(url: str) -> Path:
    """``https://www.aigrunn.org/tech`` -> ``www.aigrunn.org/tech.html``.

    Every segment is reduced to a safe character set, so a URL can never
    escape the export directory.
    """
    parts = urlsplit(url)
    segments = [seg for seg in parts.path.split("/") if seg]
    if not segments:
        segments = ["index"]
    if parts.query:
        segments[-1] += "_" + parts.query
    safe = [_UNSAFE.sub("_", seg).lstrip(".") or "_" for seg in segments]
    safe[-1] += ".html"
    host = _UNSAFE.sub("_", parts.netloc).lstrip(".") or "_"
    return Path(host, *safe)


def _exclusive_bound(at: str | date | datetime) -> str:
    """Turn a point in time into an exclusive upper bound on ``fetched_at``."""
    if isinstance(at, str):
        at = date.fromisoformat(at) if len(at) == 10 else datetime.fromisoformat(at)
    if isinstance(at, datetime):
        moment = at if at.tzinfo else at.replace(tzinfo=UTC)
        moment = moment.astimezone(UTC).replace(microsecond=0) + timedelta(seconds=1)
        return moment.isoformat(timespec="seconds")
    # A date: everything fetched on that day counts.
    return datetime.combine(at + timedelta(days=1), datetime.min.time(), UTC).isoformat(
        timespec="seconds"
    )
