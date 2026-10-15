"""Object stores for tests: an in-process moto S3 server, or the real one ``COVERSET_TEST_BLOB_URL`` names.

The suite's default needs no service: :func:`moto_blob_url` starts moto's ``ThreadedMotoServer``
once per process (one per xdist worker) on a free loopback port, creates the bucket and puts dummy
credentials in this process's environment (``AWS_ACCESS_KEY_ID=testing``; child processes the
tests spawn inherit them, which is what an ingest pool's workers need). Point the suite at a real
store instead with::

    COVERSET_TEST_BLOB_URL='s3://coverset?endpoint_url=http://127.0.0.1:9000&region=us-east-1' \\
    AWS_ACCESS_KEY_ID=minioadmin AWS_SECRET_ACCESS_KEY=minioadmin uv run pytest tests/storage

(the compose MinIO of ``deploy/dev-compose.yml``). Either way every test gets its own key prefix
(:func:`fresh_blob_url`), so tests never see each other's objects and nothing needs a new bucket.

moto evaluates ``If-Match`` / ``If-None-Match`` on ``PutObject`` but not atomically (it reads the
current object, then stores the new one, and its server runs a thread per request), so two racing
conditional writes can both succeed there, which S3, MinIO and R2 never allow. The server started
here serialises ``PutObject`` handling with one lock, which makes moto's check-and-store atomic like
the real stores; the dictionary's compare-and-swap tests rely on it.
"""

from __future__ import annotations

import functools
import os
import threading
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from coverset.storage.s3 import S3BlobStore, parse_blob_url

if TYPE_CHECKING:
    from coverset.facts.store import FactsStore

__all__ = [
    "TEST_BLOB_URL_ENV",
    "fresh_blob_url",
    "local_blob_url",
    "local_facts_store",
    "moto_blob_url",
    "test_blob_url",
]

TEST_BLOB_URL_ENV = "COVERSET_TEST_BLOB_URL"
_BUCKET = "coverset-test"
_server: Any = None
_url: str | None = None
_lock = threading.Lock()


def test_blob_url() -> str | None:
    """``COVERSET_TEST_BLOB_URL`` when set (an ``s3://`` URL with its ``endpoint_url``), else None."""
    value = os.environ.get(TEST_BLOB_URL_ENV, "").strip()
    return value or None


test_blob_url.__test__ = False  # type: ignore[attr-defined]  # not a test, despite the name


def _serialise_puts() -> None:
    """Make moto's conditional ``PutObject`` atomic (see the module docstring)."""
    from moto.s3.responses import S3Response

    if getattr(S3Response.put_object, "_coverset_serialised", False):
        return
    original = S3Response.put_object
    guard = threading.Lock()

    @functools.wraps(original)
    def put_object(self: Any) -> Any:
        with guard:
            return original(self)

    put_object._coverset_serialised = True  # type: ignore[attr-defined]
    S3Response.put_object = put_object  # type: ignore[method-assign]


def moto_blob_url() -> str:
    """The URL of this process's moto S3 server (started, with its bucket, on first use)."""
    global _server, _url
    with _lock:
        if _url is not None:
            return _url
        import logging

        from moto.server import ThreadedMotoServer

        logging.getLogger("werkzeug").setLevel(logging.ERROR)  # no access log line per request
        _serialise_puts()
        for name, value in (
            ("AWS_ACCESS_KEY_ID", "testing"),
            ("AWS_SECRET_ACCESS_KEY", "testing"),
            ("AWS_DEFAULT_REGION", "us-east-1"),
        ):
            os.environ[name] = value
        os.environ.pop("AWS_PROFILE", None)
        os.environ.pop("AWS_SESSION_TOKEN", None)
        server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
        server.start()
        host, port = server.get_host_and_port()
        url = f"s3://{_BUCKET}?endpoint_url=http://{host}:{port}&region=us-east-1"
        S3BlobStore.from_url(url).client.create_bucket(Bucket=_BUCKET)
        _server, _url = server, url
        return url


def fresh_blob_url(label: str = "t") -> str:
    """A URL with a key prefix no other test uses: below ``COVERSET_TEST_BLOB_URL`` when set, else
    in the moto server's bucket."""
    base = test_blob_url() or moto_blob_url()
    parsed = parse_blob_url(base)
    prefix = "/".join(p for p in (parsed["prefix"], f"{label}-{uuid.uuid4().hex[:12]}") if p)
    query = "&".join(
        f"{name}={value}" for name in ("endpoint_url", "region") if (value := parsed[name]) is not None
    )
    return f"s3://{parsed['bucket']}/{prefix}" + (f"?{query}" if query else "")


def local_blob_url(facts_dir: Path) -> str:
    """The ``file://`` URL of the blob store :func:`local_facts_store` puts beside ``facts_dir`` (an
    ``IngestJob`` for that store carries it)."""
    facts_dir = Path(facts_dir)
    return facts_dir.with_name(f"{facts_dir.name}.blobs").resolve().as_uri()


def local_facts_store(facts_dir: Path) -> FactsStore:
    """A facts store whose cache is ``facts_dir`` and whose blob store is files in the sibling
    directory ``<facts_dir>.blobs``: what ``Settings`` gives a data directory, for tests that work on
    a bare directory."""
    from coverset.facts.store import FactsStore
    from coverset.storage import store_from_url

    return FactsStore(Path(facts_dir), store_from_url(local_blob_url(facts_dir)))
