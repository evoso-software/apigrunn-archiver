from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from apigrunn_archiver import Archiver
from apigrunn_archiver.targets import TRACKS

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_bytes(track: str) -> bytes:
    return (FIXTURES / f"{track}.html").read_bytes()


def fixture_html(track: str) -> str:
    return fixture_bytes(track).decode("utf-8")


def edit_cards(html: bytes, video_id: str, *, remove: bool = False, **attrs: str) -> bytes:
    """Remove, or change ``data-*`` attributes of, the talk card for ``video_id``."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    anchors = [a for a in soup.select("a.talk-card") if video_id in (a.get("href") or "")]
    assert anchors, f"no card for {video_id}"
    for anchor in anchors:
        if remove:
            anchor.decompose()
        else:
            for key, value in attrs.items():
                anchor[f"data-{key}"] = value
    return str(soup).encode("utf-8")


def add_noise(html: bytes, token: str) -> bytes:
    """Change the page the way a CMS does on every request: same talks, new bytes."""
    return html.replace(b"</body>", f'<script nonce="{token}"></script></body>'.encode(), 1)


@pytest.fixture
def tech_html() -> str:
    return fixture_html("tech")


class FakeSite:
    """An in-memory aigrunn.org serving the saved fixtures.

    Records every request and honours ``If-None-Match`` so the conditional
    path is exercised offline.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.version = "v1"
        #: Tracks that answer 503.
        self.fail: set[str] = set()
        #: Tracks whose connection fails outright.
        self.unreachable: set[str] = set()
        #: Tracks that have been deleted (404).
        self.gone: set[str] = set()
        #: Serve these bytes for a track instead of its fixture.
        self.override: dict[str, bytes] = {}
        self.content_type = "text/html; charset=utf-8"

    def page(self, track: str) -> bytes:
        return self.override.get(track) or fixture_bytes(track)

    def tracks_listing(self, video_id: str) -> list[str]:
        return [t for t in TRACKS if f"v={video_id}".encode() in self.page(t)]

    def etag(self, track: str) -> str:
        return f'"{track}-{self.version}"'

    def handle(self, request: httpx.Request) -> httpx.Response:
        track = request.url.path.lstrip("/")
        self.requests.append(request)

        if track in self.unreachable:
            raise httpx.ConnectError("connection refused", request=request)
        if track in self.fail:
            return httpx.Response(503, text="try later")
        if track in self.gone or track not in TRACKS:
            return httpx.Response(404, text="not found")

        etag = self.etag(track)
        if request.headers.get("If-None-Match") == etag:
            return httpx.Response(304, headers={"ETag": etag})
        return httpx.Response(
            200,
            content=self.page(track),
            headers={
                "ETag": etag,
                "Last-Modified": "Mon, 14 Sep 2026 08:01:00 GMT",
                "Content-Type": self.content_type,
            },
        )

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


@pytest.fixture
def site() -> FakeSite:
    return FakeSite()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "archive.db"


@pytest.fixture
def archiver(site: FakeSite, db_path: Path):
    with Archiver(db_path=db_path, transport=site.transport) as a:
        yield a
