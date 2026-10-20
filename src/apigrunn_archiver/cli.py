"""Command-line interface: ``apigrunn-archiver run | runs | show``."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from ._version import __version__
from .archiver import Archiver
from .errors import ArchiverError


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
                # Non-zero so cron/systemd surfaces a page that went missing.
                return 0 if result.status == "ok" else 1

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
    except ArchiverError as exc:
        print(exc, file=sys.stderr)
        return 2
    return 2  # unreachable: argparse requires a command


if __name__ == "__main__":
    raise SystemExit(main())
