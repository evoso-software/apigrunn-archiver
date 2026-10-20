"""Command-line interface for :mod:`apigrunn_archiver`.

Exit codes: 0 success, 1 a page failed / pages differ / archive damaged
(per command), 2 usage or lookup error, 3 ``run`` saw talks disappear.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from ._version import __version__
from .archiver import Archiver
from .errors import ArchiverError
from .models import TalkChanges

EXIT_TALKS_REMOVED = 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apigrunn-archiver",
        description="Keep a versioned local archive of the aiGrunn track pages.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--db", help="archive file (default: $APIGRUNN_ARCHIVE or XDG data dir)")
    parser.add_argument("-v", "--verbose", action="store_true", help="log each request")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="take a snapshot of every page")
    run.add_argument(
        "--conditional",
        action="store_true",
        help="send If-None-Match/If-Modified-Since instead of downloading every page",
    )

    runs = sub.add_parser("runs", help="list runs, newest first")
    runs.add_argument("-n", "--limit", type=int, default=20)

    show = sub.add_parser("show", help="print a stored page")
    show.add_argument("page", help="track slug (e.g. tech) or full URL")
    when = show.add_mutually_exclusive_group()
    when.add_argument("--at", help="ISO date or datetime; latest snapshot at or before it")
    when.add_argument("--run", type=int, help="the snapshot from this run id")
    show.add_argument("--raw", action="store_true", help="write the exact bytes to stdout")

    history = sub.add_parser("history", help="when a page changed, failed or recovered")
    history.add_argument("page", help="track slug (e.g. tech) or full URL")
    history.add_argument("-a", "--all", action="store_true", help="list every snapshot")

    diff = sub.add_parser(
        "diff", help="unified diff of a page between two runs (exit 1 if they differ)"
    )
    diff.add_argument("page", help="track slug (e.g. tech) or full URL")
    diff.add_argument("run_a", type=int)
    diff.add_argument("run_b", type=int, nargs="?", help="default: latest good snapshot")
    diff.add_argument("-U", "--context", type=int, default=3, help="lines of context")

    sub.add_parser("verify", help="re-hash every stored page (exit 1 if damaged)")

    talks = sub.add_parser("talks", help="list talks as of a run, or ones the site dropped")
    which_talks = talks.add_mutually_exclusive_group()
    which_talks.add_argument("--run", type=int, help="as of this run (default: latest)")
    which_talks.add_argument(
        "--gone", action="store_true", help="talks once listed that the site no longer lists"
    )
    which_talks.add_argument(
        "--ever", action="store_true", help="every talk ever seen, with first/last sighting"
    )
    talks.add_argument("--year", type=int)
    talks.add_argument("--track")
    talks.add_argument("--search", help="substring of title, description or speaker")

    changes = sub.add_parser("changes", help="talks added, removed or edited between runs")
    changes.add_argument("run_a", type=int)
    changes.add_argument("run_b", type=int, nargs="?", help="default: latest run")

    reindex = sub.add_parser("reindex", help="re-parse archived HTML into the talk index")
    reindex.add_argument("--force", action="store_true", help="rebuild from scratch")

    export = sub.add_parser("export", help="write runs out as plain .html files")
    export.add_argument("directory")
    which = export.add_mutually_exclusive_group()
    which.add_argument("--run", type=int, help="run id (default: latest)")
    which.add_argument("--all", action="store_true", help="every run")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        with Archiver(db_path=args.db) as archiver:
            if args.command == "run":
                result = archiver.run(conditional=args.conditional)
                print(result)
                for snap in result.snapshots:
                    if not snap.ok:
                        print(f"  error  {snap.url}: {snap.error}", file=sys.stderr)
                    elif snap.changed:
                        print(f"  changed  {snap.url}")
                for url, sha, message in result.parse_errors:
                    print(f"  unparseable  {url} ({sha[:12]}): {message}", file=sys.stderr)
                if result.talk_changes is not None:
                    _print_changes(result.talk_changes, removed_to=sys.stderr)
                # Non-zero so cron/systemd surfaces a page or talk that went missing.
                if result.status != "ok":
                    return 1
                if result.talk_changes is not None and result.talk_changes.removed:
                    return EXIT_TALKS_REMOVED
                return 0

            if args.command == "talks":
                if args.gone or args.ever:
                    entries = archiver.archived_talks(gone_only=args.gone)
                    runs = {r.id: r for r in archiver.runs()}
                    for entry in entries:
                        t = entry.talk
                        if not _matches(t, args):
                            continue
                        last = runs[entry.last_seen_run].started_at[:10]
                        state = "" if entry.present else f"  [gone; last seen run {entry.last_seen_run}, {last}]"
                        print(f"{t}  ({', '.join(t.tracks)}){state}")
                else:
                    for t in archiver.talks(
                        run_id=args.run, year=args.year, track=args.track, search=args.search
                    ):
                        print(f"{t}  ({', '.join(t.tracks)})")
                return 0

            if args.command == "changes":
                _print_changes(archiver.talk_changes(args.run_a, args.run_b))
                return 0

            if args.command == "reindex":
                indexed = archiver.reindex(force=args.force)
                print(f"parsed {indexed.parsed} page versions, {len(indexed.errors)} errors")
                for url, sha, message in indexed.errors:
                    print(f"  unparseable  {url} ({sha[:12]}): {message}", file=sys.stderr)
                return 1 if indexed.errors else 0

            if args.command == "runs":
                print(f"{'id':>4}  {'started (UTC)':<25}  {'status':<8}  pages  changed  errors")
                for run in archiver.runs(limit=args.limit):
                    print(
                        f"{run.id:>4}  {run.started_at:<25}  {run.status:<8}  "
                        f"{run.pages:>5}  {run.changed:>7}  {run.errors:>6}"
                    )
                return 0

            if args.command == "show":
                kwargs = {"at": args.at, "run_id": args.run}
                if args.raw:
                    sys.stdout.buffer.write(archiver.content(args.page, **kwargs))
                else:
                    sys.stdout.write(archiver.html(args.page, **kwargs))
                return 0

            if args.command == "history":
                for entry in archiver.history(args.page, everything=args.all):
                    snap = entry.snapshot
                    kind = entry.kind
                    if kind == "recovered" and snap.changed:
                        kind = "recovered, changed"
                    detail = snap.error or (snap.blob_sha256 or "")[:12]
                    print(f"run {snap.run_id:>4}  {snap.fetched_at}  {kind:<18}  {detail}")
                return 0

            if args.command == "diff":
                text = archiver.diff(args.page, args.run_a, args.run_b, context=args.context)
                sys.stdout.write(text)
                return 1 if text else 0

            if args.command == "verify":
                result = archiver.verify()
                print(result)
                for sha, reason in result.bad_blobs:
                    print(f"  bad blob  {sha}: {reason}", file=sys.stderr)
                for run_id, url, sha in result.dangling:
                    print(f"  missing   {sha} (run {run_id}, {url})", file=sys.stderr)
                for message in result.sqlite_errors:
                    print(f"  sqlite    {message}", file=sys.stderr)
                return 0 if result.ok else 1

            if args.command == "export":
                exported = archiver.export(args.directory, run_id=args.run, all_runs=args.all)
                print(
                    f"exported {len(exported.runs)} run(s), {exported.files} files "
                    f"to {args.directory} ({exported.skipped} failed snapshots in manifests only)"
                )
                return 0
    except ArchiverError as exc:
        print(exc, file=sys.stderr)
        return 2
    return 2  # unreachable: argparse requires a command


def _matches(talk, args: argparse.Namespace) -> bool:
    if args.year is not None and talk.year != args.year:
        return False
    if args.track is not None and args.track not in talk.tracks:
        return False
    if args.search:
        needle = args.search.casefold()
        haystack = f"{talk.title}\n{talk.description}\n{talk.speaker}".casefold()
        if needle not in haystack:
            return False
    return True


def _print_changes(changes: TalkChanges, *, removed_to=None) -> None:
    print(changes)
    for talk in changes.added:
        print(f"  + {talk}")
    for talk in changes.removed:
        print(f"  - {talk}", file=removed_to or sys.stdout)
    for before, after in changes.changed:
        fields = [
            name
            for name in ("title", "speaker", "description", "url", "year", "tracks", "thumbnail_url")
            if getattr(before, name) != getattr(after, name)
        ]
        print(f"  ~ {after}  ({', '.join(fields)} changed)")
    for url in changes.stale_pages:
        print(f"  ! stale  {url} (failed or unparseable; using an older copy)")


if __name__ == "__main__":
    raise SystemExit(main())
