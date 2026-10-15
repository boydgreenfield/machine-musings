"""A :class:`~coverset.storage.base.BlobStore` on any S3-compatible object store, through boto3.

``COVERSET_BLOB_URL=s3://<bucket>[/<prefix>][?endpoint_url=<url>&region=<region>]`` names it:

* AWS S3: ``s3://my-bucket/coverset?region=eu-west-1`` (no endpoint);
* MinIO: ``s3://coverset?endpoint_url=http://127.0.0.1:9000&region=us-east-1``;
* Cloudflare R2: ``s3://coverset?endpoint_url=https://<account id>.r2.cloudflarestorage.com&region=auto``;
* Tigris: ``s3://coverset?endpoint_url=https://fly.storage.tigris.dev&region=auto``.

Credentials come from boto3's own chain and never from the URL: ``AWS_ACCESS_KEY_ID`` /
``AWS_SECRET_ACCESS_KEY`` (and ``AWS_SESSION_TOKEN``), ``AWS_PROFILE``, or an instance or task role.

**Requests.** One client per store, created on first use and shared by every thread (boto3 clients
are thread-safe; their creation is not, so it happens under a lock). Retries use botocore's
``standard`` mode with 5 attempts (throttling, 5xx and connection errors back off and retry), with
a 5 s connect and 60 s read timeout. When an endpoint is given, addressing is path-style
(``http://host/bucket/key``: MinIO and most S3-compatible stores need it, and it works on the rest)
and request checksums are sent only where an operation requires one (botocore's newer default of a
CRC32 trailer on every upload is refused by some S3-compatible stores).

**Uploads** above :data:`~coverset.storage.base.MULTIPART_THRESHOLD` (8 MiB) go up in 8 MiB parts
through boto3's transfer manager. A conditional write (``if_match`` / ``if_none_match``) is always
one ``PutObject`` with the ``If-Match`` / ``If-None-Match: *`` header, which AWS S3, MinIO,
Cloudflare R2 and Tigris evaluate atomically on the server; a lost race answers 412 (or 409
``ConditionalRequestConflict`` when two land together) and raises
:class:`~coverset.storage.base.PreconditionFailed`. Conditional objects (the dictionary, the run
manifests) are small, so the single-request limit (5 GiB) is never near. A store that ignores the
headers would turn the compare-and-swap into a blind overwrite: ``coverset deploy check`` probes for
it (:func:`coverset.storage.check_store`).

**Etags** are S3's ``ETag`` without its quotes. Errors become
:class:`~coverset.storage.base.StorageError` with the operation, the key and S3's error code;
a missing key is :class:`~coverset.storage.base.BlobNotFound`.
"""

from __future__ import annotations

import os
import re
import threading
import uuid
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from coverset.storage.base import (
    MULTIPART_THRESHOLD,
    BlobNotFound,
    BlobTooLarge,
    ObjectInfo,
    PreconditionFailed,
    StorageError,
    check_key,
    check_prefix,
)

__all__ = ["S3BlobStore", "parse_blob_url"]

_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_QUERY_KEYS = frozenset({"endpoint_url", "region"})
_NOT_FOUND = frozenset({"404", "NoSuchKey", "NotFound"})
_PRECONDITION = frozenset({"412", "PreconditionFailed", "ConditionalRequestConflict"})
_DELETE_BATCH = 1000
_CHUNK = 1024 * 1024


def parse_blob_url(url: str) -> dict[str, str | None]:
    """``bucket``, ``prefix``, ``endpoint_url`` and ``region`` of an ``s3://`` blob URL.

    Raises :class:`ValueError` for anything else: another scheme, an invalid bucket name, an
    unknown query parameter, credentials in the URL or an endpoint that is not http(s).
    """
    parts = urlsplit(url)
    if parts.scheme != "s3":
        raise ValueError(f"not an s3:// URL: {url!r}")
    if parts.username or parts.password or "@" in parts.netloc:
        raise ValueError("put credentials in AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY, not in the URL")
    bucket = parts.netloc
    if not _BUCKET.match(bucket) or ".." in bucket:
        raise ValueError(f"invalid bucket name {bucket!r}")
    prefix = parts.path.strip("/")
    if prefix:
        check_key(prefix)
    query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=False)
    if unknown := set(query) - _QUERY_KEYS:
        raise ValueError(f"unknown blob URL parameters {sorted(unknown)} (allowed: endpoint_url, region)")
    single = {name: values[-1].strip() or None for name, values in query.items()}
    endpoint = single.get("endpoint_url")
    if endpoint is not None:
        scheme = urlsplit(endpoint).scheme
        if scheme not in ("http", "https") or not urlsplit(endpoint).hostname:
            raise ValueError(f"endpoint_url must be an http(s) URL, got {endpoint!r}")
    return {"bucket": bucket, "prefix": prefix, "endpoint_url": endpoint, "region": single.get("region")}


def _strip(etag: object) -> str:
    return str(etag or "").strip('"')


def _code(exc: Exception) -> str:
    response = getattr(exc, "response", None) or {}
    code = str(response.get("Error", {}).get("Code", ""))
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code or str(status or "")


class S3BlobStore:
    """Blobs in one bucket under an optional key prefix (see the module docstring)."""

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        endpoint_url: str | None = None,
        region: str | None = None,
    ) -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.endpoint_url = endpoint_url
        self.region = region
        self._client: Any = None
        self._lock = threading.Lock()

    @classmethod
    def from_url(cls, url: str) -> S3BlobStore:
        """The store an ``s3://`` URL names (:func:`parse_blob_url`)."""
        parsed = parse_blob_url(url)
        return cls(
            str(parsed["bucket"]),
            parsed["prefix"] or "",
            endpoint_url=parsed["endpoint_url"],
            region=parsed["region"],
        )

    def __repr__(self) -> str:
        return f"S3BlobStore({self.url!r})"

    def __getstate__(self) -> dict[str, Any]:
        """Pickle without the client and the lock (a worker process builds its own)."""
        state = dict(self.__dict__)
        state["_client"] = None
        del state["_lock"]
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__dict__.update(state)
        self._lock = threading.Lock()

    def forget_client(self) -> None:
        """Drop the client (and its connection pool) so the next call builds a new one: what a
        process forked while this one held open connections must do (:mod:`coverset.storage`
        registers it with :func:`os.register_at_fork`), and what a test does to change credentials."""
        self._lock = threading.Lock()
        self._client = None

    @property
    def url(self) -> str:
        """``s3://bucket/prefix`` plus the endpoint, for messages (never credentials)."""
        base = f"s3://{self.bucket}" + (f"/{self.prefix}" if self.prefix else "")
        return base + (f" at {self.endpoint_url}" if self.endpoint_url else "")

    # -- the client --------------------------------------------------------------------------------

    @property
    def client(self) -> Any:
        """The boto3 S3 client (created once, thread-safe to use)."""
        if self._client is None:
            with self._lock:
                if self._client is None:
                    self._client = self._make_client()
        return self._client

    def _make_client(self) -> Any:
        import boto3
        from botocore.config import Config

        options: dict[str, Any] = {
            "retries": {"max_attempts": 5, "mode": "standard"},
            "connect_timeout": 5,
            "read_timeout": 60,
            "max_pool_connections": 32,
        }
        if self.endpoint_url:
            options["s3"] = {"addressing_style": "path"}
            options["request_checksum_calculation"] = "when_required"
            options["response_checksum_validation"] = "when_required"
        session = boto3.session.Session()
        return session.client(
            "s3", endpoint_url=self.endpoint_url, region_name=self.region, config=Config(**options)
        )

    def _key(self, key: str) -> str:
        check_key(key)
        return f"{self.prefix}/{key}" if self.prefix else key

    def _full_prefix(self, prefix: str) -> str:
        check_prefix(prefix)
        return f"{self.prefix}/{prefix}" if self.prefix else prefix

    def _relative(self, full: str) -> str:
        return full[len(self.prefix) + 1 :] if self.prefix else full

    def _fail(self, operation: str, key: str, exc: Exception) -> StorageError:
        code = _code(exc)
        if code in _NOT_FOUND:
            return BlobNotFound(f"no object {key} in {self.url}")
        return StorageError(f"{operation} {key} in {self.url} failed: {code or type(exc).__name__}: {exc}")

    # -- writing ----------------------------------------------------------------------------------

    def _conditions(self, if_match: str | None, if_none_match: bool) -> dict[str, str]:
        extra: dict[str, str] = {}
        if if_match is not None:
            extra["IfMatch"] = f'"{_strip(if_match)}"'
        if if_none_match:
            extra["IfNoneMatch"] = "*"
        return extra

    def _put(self, key: str, body: Any, if_match: str | None, if_none_match: bool) -> str:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            response = self.client.put_object(
                Bucket=self.bucket, Key=self._key(key), Body=body, **self._conditions(if_match, if_none_match)
            )
        except ClientError as exc:
            code = _code(exc)
            if code in _PRECONDITION or (if_match is not None and code in _NOT_FOUND):
                raise PreconditionFailed(
                    f"{key} in {self.url}: the expected version is gone ({code})"
                ) from None
            raise self._fail("put", key, exc) from exc
        except BotoCoreError as exc:
            raise self._fail("put", key, exc) from exc
        return _strip(response.get("ETag"))

    def put_bytes(
        self, key: str, data: bytes, *, if_match: str | None = None, if_none_match: bool = False
    ) -> str:
        """One ``PutObject`` (conditional when asked); returns the etag."""
        return self._put(key, data, if_match, if_none_match)

    def put_file(
        self, key: str, path: Path, *, if_match: str | None = None, if_none_match: bool = False
    ) -> str:
        """Upload ``path``: one request up to 8 MiB (or when conditional), else 8 MiB parts."""
        size = os.path.getsize(path)
        if size <= MULTIPART_THRESHOLD or if_match is not None or if_none_match:
            with open(path, "rb") as body:
                return self._put(key, body, if_match, if_none_match)
        from boto3.s3.transfer import TransferConfig
        from botocore.exceptions import BotoCoreError, ClientError

        config = TransferConfig(
            multipart_threshold=MULTIPART_THRESHOLD,
            multipart_chunksize=MULTIPART_THRESHOLD,
            max_concurrency=4,
        )
        try:
            self.client.upload_file(str(path), self.bucket, self._key(key), Config=config)
        except (ClientError, BotoCoreError) as exc:
            raise self._fail("upload", key, exc) from exc
        except Exception as exc:  # s3transfer wraps client errors in S3UploadFailedError
            raise StorageError(f"upload {key} to {self.url} failed: {exc}") from exc
        info = self.head(key)
        if info is None:
            raise StorageError(f"upload {key} to {self.url}: the object is missing right after the upload")
        return info.etag

    def copy(self, source: str, target: str) -> str:
        """Server-side ``CopyObject`` (objects up to 5 GiB); returns the copy's etag."""
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            response = self.client.copy_object(
                Bucket=self.bucket,
                Key=self._key(target),
                CopySource={"Bucket": self.bucket, "Key": self._key(source)},
            )
        except (ClientError, BotoCoreError) as exc:
            raise self._fail("copy", source, exc) from exc
        return _strip(response.get("CopyObjectResult", {}).get("ETag"))

    # -- reading ----------------------------------------------------------------------------------

    def _get(self, key: str) -> dict[str, Any]:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            return self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        except (ClientError, BotoCoreError) as exc:
            raise self._fail("get", key, exc) from exc

    def get_bytes(self, key: str) -> tuple[bytes, str]:
        """One ``GetObject``: the bytes and the etag of the version read."""
        response = self._get(key)
        body = response["Body"]
        try:
            return body.read(), _strip(response.get("ETag"))
        finally:
            body.close()

    def get_file(self, key: str, path: Path, *, max_bytes: int | None = None) -> str:
        """Stream the object into a temporary sibling of ``path``, then rename it into place.

        ``max_bytes`` is checked against the response's ``Content-Length`` before the body is read.
        """
        from botocore.exceptions import BotoCoreError

        response = self._get(key)
        size = int(response.get("ContentLength") or 0)
        if max_bytes is not None and size > max_bytes:
            response["Body"].close()
            raise BlobTooLarge(f"{key} in {self.url} is {size} bytes, more than {max_bytes}")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            body = response["Body"]
            try:
                with open(tmp, "xb") as out:
                    for chunk in body.iter_chunks(_CHUNK):
                        out.write(chunk)
            finally:
                body.close()
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BotoCoreError as exc:
            raise self._fail("get", key, exc) from exc
        finally:
            tmp.unlink(missing_ok=True)
        return _strip(response.get("ETag"))

    def head(self, key: str) -> ObjectInfo | None:
        """``HeadObject``: None when the key does not exist."""
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            response = self.client.head_object(Bucket=self.bucket, Key=self._key(key))
        except ClientError as exc:
            if _code(exc) in _NOT_FOUND:
                return None
            raise self._fail("head", key, exc) from exc
        except BotoCoreError as exc:
            raise self._fail("head", key, exc) from exc
        modified = response.get("LastModified") or datetime.now(UTC)
        return ObjectInfo(key, int(response.get("ContentLength", 0)), _strip(response.get("ETag")), modified)

    def list(self, prefix: str = "") -> Iterator[ObjectInfo]:
        """``ListObjectsV2`` pages under ``prefix``."""
        from botocore.exceptions import BotoCoreError, ClientError

        full = self._full_prefix(prefix)
        paginator = self.client.get_paginator("list_objects_v2")
        try:
            for page in paginator.paginate(Bucket=self.bucket, Prefix=full):
                for item in page.get("Contents", ()):
                    yield ObjectInfo(
                        self._relative(item["Key"]),
                        int(item["Size"]),
                        _strip(item.get("ETag")),
                        item["LastModified"],
                    )
        except (ClientError, BotoCoreError) as exc:
            raise self._fail("list", prefix or "/", exc) from exc

    def list_dirs(self, prefix: str = "") -> list[str]:
        """``ListObjectsV2`` with ``Delimiter="/"``: the common prefixes directly below ``prefix``."""
        from botocore.exceptions import BotoCoreError, ClientError

        full = self._full_prefix(prefix)
        names: set[str] = set()
        paginator = self.client.get_paginator("list_objects_v2")
        try:
            for page in paginator.paginate(Bucket=self.bucket, Prefix=full, Delimiter="/"):
                for item in page.get("CommonPrefixes", ()):
                    name = item["Prefix"][len(full) :].rstrip("/")
                    if name:
                        names.add(name)
        except (ClientError, BotoCoreError) as exc:
            raise self._fail("list", prefix or "/", exc) from exc
        return sorted(names)

    # -- deleting ---------------------------------------------------------------------------------

    def delete(self, keys: Iterable[str]) -> None:
        """``DeleteObjects`` in batches of 1,000 (a missing key is not an error)."""
        from botocore.exceptions import BotoCoreError, ClientError

        batch: list[dict[str, str]] = []
        try:
            for key in keys:
                batch.append({"Key": self._key(key)})
                if len(batch) == _DELETE_BATCH:
                    self._delete_batch(batch)
                    batch = []
            if batch:
                self._delete_batch(batch)
        except (ClientError, BotoCoreError) as exc:
            raise self._fail("delete", batch[0]["Key"] if batch else "", exc) from exc

    def _delete_batch(self, batch: list[dict[str, str]]) -> None:
        response = self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": batch, "Quiet": True})
        errors = [e for e in response.get("Errors", ()) if e.get("Code") not in _NOT_FOUND]
        if errors:
            first = errors[0]
            raise StorageError(
                f"delete {first.get('Key')} in {self.url} failed: {first.get('Code')}: {first.get('Message')}"
                + (f" (and {len(errors) - 1} more)" if len(errors) > 1 else "")
            )

    def delete_prefix(self, prefix: str) -> int:
        """List, then delete, every object under ``prefix`` (never the whole store)."""
        if not check_prefix(prefix):
            raise ValueError("refusing to delete the whole store")
        keys = [info.key for info in self.list(prefix)]
        self.delete(keys)
        return len(keys)

    def local_path(self, key: str) -> None:
        """Always None: objects live in the bucket."""
        check_key(key)
        return None
