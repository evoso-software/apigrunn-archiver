"""HTTP retrieval of the pages to archive.

Unlike apigrunn's fetcher this keeps the raw response bytes and every header:
an archive should preserve exactly what the server sent, encoding included.
``robots.txt`` on aigrunn.org is ``Allow: /``; we still identify ourselves and
fetch one page at a time.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from ._version import __version__

logger = logging.getLogger(__name__)

USER_AGENT = f"apigrunn-archiver/{__version__} (+https://www.aigrunn.org/; personal archive)"
DEFAULT_TIMEOUT = 15.0


@dataclass(frozen=True, slots=True)
class Validators:
    """The ``ETag``/``Last-Modified`` pair that makes a request conditional."""

    etag: str | None
    last_modified: str | None


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Everything the server said about one URL, or why it said nothing."""

    url: str
    fetched_at: str
    status_code: int | None
    final_url: str | None = None
    content: bytes | None = None
    headers: list[tuple[str, str]] = field(default_factory=list)
    error: str | None = None

    @property
    def not_modified(self) -> bool:
        return self.status_code == 304

    @property
    def ok(self) -> bool:
        return self.status_code is not None and (200 <= self.status_code < 300 or self.not_modified)

    def header(self, name: str) -> str | None:
        name = name.lower()
        return next((value for key, value in self.headers if key.lower() == name), None)


def build_client(
    *, timeout: float = DEFAULT_TIMEOUT, transport: httpx.BaseTransport | None = None
) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        timeout=timeout,
        follow_redirects=True,
        transport=transport,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _conditional_headers(validators: Validators | None) -> dict[str, str]:
    if validators is None:
        return {}
    headers = {}
    if validators.etag:
        headers["If-None-Match"] = validators.etag
    if validators.last_modified:
        headers["If-Modified-Since"] = validators.last_modified
    return headers


def iter_fetch(
    urls: Iterable[str],
    *,
    client: httpx.Client,
    validators: Mapping[str, Validators] | None = None,
) -> Iterator[FetchResult]:
    """Fetch each URL in turn over one connection, yielding as each one lands.

    Pass ``validators`` to send conditional requests; without them every page
    is downloaded in full, which is the archive's default because it does not
    trust the server's ETags to notice every change.
    """
    validators = validators or {}
    for url in urls:
        fetched_at = _now()
        try:
            response = client.get(url, headers=_conditional_headers(validators.get(url)))
        except httpx.HTTPError as exc:
            logger.warning("%s: request failed: %s", url, exc)
            yield FetchResult(url=url, fetched_at=fetched_at, status_code=None, error=str(exc))
            continue

        error = f"HTTP {response.status_code}" if response.status_code >= 400 else None
        if error:
            logger.warning("%s: %s", url, error)
        yield FetchResult(
            url=url,
            fetched_at=fetched_at,
            status_code=response.status_code,
            final_url=str(response.url),
            # A 304 has no body. Error pages do, but the store keeps only their status.
            content=None if response.status_code == 304 else response.content,
            headers=list(response.headers.multi_items()),
            error=error,
        )


def fetch(
    urls: Iterable[str],
    *,
    client: httpx.Client,
    validators: Mapping[str, Validators] | None = None,
) -> list[FetchResult]:
    """Fetch each URL in turn; :func:`iter_fetch`, collected into a list."""
    return list(iter_fetch(urls, client=client, validators=validators))
