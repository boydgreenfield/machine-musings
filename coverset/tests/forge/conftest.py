"""Fixtures for the forge tests: settings with the fixtures' webhook secret, a fake forge, RSA keys
and a live Hypercorn server for ``coverset-ci wait``. Helpers live in ``forge_helpers.py``."""

from __future__ import annotations

import asyncio
import threading
import time
import urllib.request
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from forge_helpers import LiveServer, StubJwkClient, free_port
from hypercorn.asyncio import serve
from hypercorn.config import Config as HypercornConfig
from quart import Quart, request

from coverset.app import create_app
from coverset.config import Settings
from coverset.db.session import Database, init_db
from coverset.forge.fake import FakeForge
from coverset.forge.signing import TEST_WEBHOOK_SECRET


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    """Webhooks enabled with the fixtures' secret; only the demo owner is allowed."""
    return {"github_webhook_secret": TEST_WEBHOOK_SECRET, "allowed_owners": ["coverset-demo"]}


@pytest.fixture
def fake_forge(app: Quart) -> FakeForge:
    """A fake forge recording into the app's database, installed as the app's forge."""
    forge = FakeForge(session_factory=app.extensions["db"].sessionmaker)
    app.extensions["forge"] = forge
    return forge


@pytest.fixture
def db(app: Quart) -> Database:
    """The started app's database."""
    return app.extensions["db"]


@pytest.fixture(scope="session")
def rsa_key() -> rsa.RSAPrivateKey:
    """A throwaway RSA key (OIDC signing and GitHub App JWTs)."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def rsa_pem(rsa_key: rsa.RSAPrivateKey) -> str:
    """``rsa_key`` as a PKCS#8 PEM string."""
    return rsa_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


@pytest.fixture
def jwk_client(rsa_key: rsa.RSAPrivateKey) -> StubJwkClient:
    """A stub JWK client serving ``rsa_key``'s public half."""
    return StubJwkClient(rsa_key)


@pytest.fixture
def live_server(settings: Settings) -> Iterator[LiveServer]:
    """Serve ``create_app(settings)`` with Hypercorn on a free loopback port until the test ends."""
    app = create_app(settings)
    port = free_port()
    server = LiveServer(f"http://127.0.0.1:{port}", app, settings)

    @app.before_request
    async def _log() -> None:
        query = f"?{request.query_string.decode()}" if request.query_string else ""
        server.requests.append(f"{request.method} {request.path}{query} {request.headers.get('Accept', '')}")

    config = HypercornConfig()
    config.bind = [f"127.0.0.1:{port}"]
    config.accesslog = None
    config.graceful_timeout = 1.0
    state: dict[str, Any] = {}
    started = threading.Event()

    def run() -> None:
        async def main() -> None:
            state["loop"] = asyncio.get_running_loop()
            state["stop"] = asyncio.Event()
            await init_db(settings)
            started.set()
            await serve(app, config, shutdown_trigger=state["stop"].wait)  # type: ignore[arg-type]

        asyncio.run(main())

    thread = threading.Thread(target=run, name="coverset-live-server", daemon=True)
    thread.start()
    assert started.wait(15), "server thread did not start"
    deadline = time.monotonic() + 15
    while True:
        try:
            with urllib.request.urlopen(f"{server.url}/healthz", timeout=1) as response:
                if response.status == 200:
                    break
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)
    try:
        yield server
    finally:
        state["loop"].call_soon_threadsafe(state["stop"].set)
        thread.join(10)


@pytest.fixture
async def live_db(live_server: LiveServer) -> AsyncIterator[Database]:
    """A separate engine on the live server's database (for seeding from the test's loop)."""
    database = Database.from_url(live_server.settings.database_url)
    yield database
    await database.engine.dispose()
