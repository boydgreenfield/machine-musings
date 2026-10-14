"""Static files are versioned by content and cached accordingly; text responses are gzipped.

``coverset.app.install_response_caching``: ``url_for('static', ...)`` carries ``?v=<digest>``, a
request at the current version is ``immutable`` for a year and anything else revalidates; HTML,
CSS, JS, SVG and JSON over 1 KiB are gzipped for clients that accept it, never a streamed body,
and a dynamic body only for requests that cannot come from another site (BREACH).
"""

from __future__ import annotations

import gzip
import hashlib
import re
from collections.abc import AsyncIterator
from pathlib import Path

from quart import Quart, Response, url_for

from coverset.app import GZIP_MIN_BYTES, STATIC_IMMUTABLE, STATIC_REVALIDATE, create_app
from coverset.config import Settings
from coverset.web.auth import csrf_token

STATIC = Path(__file__).resolve().parents[2] / "src" / "coverset" / "web" / "static"


def _digest(name: str) -> str:
    return hashlib.sha256((STATIC / name).read_bytes()).hexdigest()[:12]


def _app(settings: Settings) -> Quart:
    """The real app plus three probe routes (added before it starts serving)."""
    application = create_app(settings)

    @application.get("/__probe/big")
    async def big() -> Response:
        return Response("<p>" + "coverage " * 2000 + "</p>", mimetype="text/html")

    @application.get("/__probe/small")
    async def small() -> Response:
        return Response("<p>tiny</p>", mimetype="text/html")

    @application.get("/__probe/stream")
    async def stream() -> Response:
        async def chunks() -> AsyncIterator[bytes]:
            for _ in range(3):
                yield b"x" * 4096

        return Response(chunks(), mimetype="text/html")

    @application.get("/__probe/secret")
    async def secret() -> Response:
        return Response(f"<body data-csrf='{csrf_token()}'>" + "coverage " * 2000, mimetype="text/html")

    @application.get("/__probe/url")
    async def static_url() -> Response:
        return Response(url_for("static", filename="app.css"), mimetype="text/plain")

    return application


async def test_static_urls_carry_the_content_version(settings: Settings) -> None:
    application = _app(settings)
    async with application.test_app():
        response = await application.test_client().get("/__probe/url")
        url = (await response.get_data()).decode()
    assert re.fullmatch(r"/static/app\.css\?v=[0-9a-f]{12}", url), url
    assert url.endswith("v=" + _digest("app.css"))


async def test_current_version_is_immutable_and_anything_else_revalidates(settings: Settings) -> None:
    application = _app(settings)
    async with application.test_app():
        client = application.test_client()
        current = await client.get(f"/static/app.css?v={_digest('app.css')}")
        stale = await client.get("/static/app.css?v=000000000000")
        bare = await client.get("/static/app.css")
        assert current.status_code == stale.status_code == bare.status_code == 200
        assert current.headers["Cache-Control"] == STATIC_IMMUTABLE
        assert stale.headers["Cache-Control"] == STATIC_REVALIDATE
        assert bare.headers["Cache-Control"] == STATIC_REVALIDATE
        assert "Expires" not in current.headers
        # Revalidation still answers 304 from the ETag.
        etag = bare.headers["ETag"]
        again = await client.get("/static/app.css", headers={"If-None-Match": etag})
        assert again.status_code == 304
        assert again.headers["Cache-Control"] == STATIC_REVALIDATE
        # The security headers are still there.
        assert "Content-Security-Policy" in current.headers


async def test_static_files_are_gzipped_for_clients_that_accept_it(settings: Settings) -> None:
    application = _app(settings)
    raw = (STATIC / "app.css").read_bytes()
    async with application.test_app():
        client = application.test_client()
        packed = await client.get("/static/app.css", headers={"Accept-Encoding": "gzip, br"})
        plain = await client.get("/static/app.css")
        body = await packed.get_data()
        assert packed.headers["Content-Encoding"] == "gzip"
        assert "Accept-Encoding" in packed.headers["Vary"]
        assert gzip.decompress(body) == raw
        assert int(packed.headers["Content-Length"]) == len(body) < len(raw)
        assert packed.headers["ETag"].startswith('W/"')
        assert "Content-Encoding" not in plain.headers
        assert await plain.get_data() == raw
        # A second request is served from the compressed copy, byte for byte.
        again = await client.get("/static/app.css", headers={"Accept-Encoding": "gzip"})
        assert await again.get_data() == body


async def test_dynamic_html_is_gzipped_only_when_worth_it(settings: Settings) -> None:
    application = _app(settings)
    async with application.test_app():
        client = application.test_client()
        big = await client.get("/__probe/big", headers={"Accept-Encoding": "gzip"})
        small = await client.get("/__probe/small", headers={"Accept-Encoding": "gzip"})
        refused = await client.get("/__probe/big", headers={"Accept-Encoding": "gzip;q=0, identity"})
        streamed = await client.get("/__probe/stream", headers={"Accept-Encoding": "gzip"})
        assert big.headers["Content-Encoding"] == "gzip"
        text = gzip.decompress(await big.get_data()).decode()
        assert text.startswith("<p>coverage ") and len(text) > GZIP_MIN_BYTES
        assert "Content-Encoding" not in small.headers
        assert "Content-Encoding" not in refused.headers
        assert "Content-Encoding" not in streamed.headers
        assert await streamed.get_data() == b"x" * 4096 * 3


async def test_dynamic_bodies_are_never_compressed_for_another_site(settings: Settings) -> None:
    """BREACH: a page carrying the session's CSRF token is gzipped only for the app's own requests
    (``Sec-Fetch-Site: same-origin`` or ``none``), never for one another site's page started; a
    client without ``Sec-Fetch-Site`` gets gzip only when the body holds no token."""
    application = _app(settings)
    async with application.test_app():
        client = application.test_client()

        async def encoding(url: str, site: str | None) -> str | None:
            headers = {"Accept-Encoding": "gzip"}
            if site is not None:
                headers["Sec-Fetch-Site"] = site
            response = await client.get(url, headers=headers)
            assert "Sec-Fetch-Site" in response.headers.get("Vary", "")
            return response.headers.get("Content-Encoding")

        assert await encoding("/__probe/secret", "same-origin") == "gzip"
        assert await encoding("/__probe/secret", "none") == "gzip"
        assert await encoding("/__probe/secret", "cross-site") is None
        assert await encoding("/__probe/secret", "same-site") is None
        assert await encoding("/__probe/secret", None) is None  # the token is in the body
        assert await encoding("/__probe/big", None) == "gzip"  # no token in it
        assert await encoding("/__probe/big", "cross-site") is None
        static = await client.get(
            "/static/app.css", headers={"Accept-Encoding": "gzip", "Sec-Fetch-Site": "cross-site"}
        )
        assert static.headers["Content-Encoding"] == "gzip"  # no secret in a static file
