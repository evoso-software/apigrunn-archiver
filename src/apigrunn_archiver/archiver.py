"""The public entry point: :class:`Archiver`."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Self

import httpx

from ._version import __version__
from .errors import SnapshotNotFound
from .fetcher import DEFAULT_TIMEOUT, build_client, fetch
from .models import Run, RunResult, Snapshot
from .store import Store
from .targets import default_targets, resolve

logger = logging.getLogger(__name__)

_CHARSET = re.compile(r"charset=([\w.:-]+)", re.IGNORECASE)


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

    def run(self, *, conditional: bool = False) -> RunResult:
        """Fetch every target page and record the result as a new run.

        By default every page is downloaded in full and deduplicated by hash.
        ``conditional=True`` sends ``If-None-Match``/``If-Modified-Since`` from
        the previous snapshot instead, trusting the server to report changes.

        Never raises for network or HTTP failures: those become error
        snapshots and set the run's status to ``partial`` or ``failed``.
        """
        run_id = self._store.start_run(__version__)
        validators = self._store.validators(self._targets) if conditional else None

        snapshots: list[Snapshot] = []
        new_blobs = 0
        try:
            with build_client(timeout=self._timeout, transport=self._transport) as client:
                for result in fetch(self._targets, client=client, validators=validators):
                    snapshot, added = self._store.add_snapshot(run_id, result)
                    snapshots.append(snapshot)
                    new_blobs += added
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

        result = RunResult(
            run_id=run_id,
            status=status,
            fetched=len(snapshots),
            changed=sum(1 for s in snapshots if s.changed),
            new_blobs=new_blobs,
            errors=errors,
            snapshots=snapshots,
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
        """Raw bytes of a snapshot, exactly as the server sent them."""
        snap = self.snapshot(url, **kwargs)  # type: ignore[arg-type]
        if snap.blob_sha256 is None:
            raise SnapshotNotFound(
                f"snapshot of {snap.url} in run {snap.run_id} has no body ({snap.error})"
            )
        return self._store.blob(snap.blob_sha256)

    def html(self, url: str, **kwargs: object) -> str:
        """A snapshot decoded to text, using the charset the server declared."""
        snap = self.snapshot(url, **kwargs)  # type: ignore[arg-type]
        raw = self.content(snap.url, run_id=snap.run_id)
        content_type = next(
            (v for k, v in self._store.headers(snap.run_id, snap.url) if k.lower() == "content-type"),
            "",
        )
        match = _CHARSET.search(content_type)
        encoding = match.group(1) if match else "utf-8"
        try:
            return raw.decode(encoding, errors="replace")
        except LookupError:
            return raw.decode("utf-8", errors="replace")


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
