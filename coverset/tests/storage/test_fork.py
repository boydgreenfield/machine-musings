"""A forked child (the ingest pool on Linux) never shares the parent's S3 client, pools or locks.

The ingest pool is a ``ProcessPoolExecutor``, which forks on Linux, so a worker starts with copies of
the parent's module state: the boto3 client with its keep-alive sockets, the transfer pools whose
threads do not exist in the child, and sync locks that a parent thread may hold. The
``os.register_at_fork`` hooks in :mod:`coverset.storage` and :mod:`coverset.facts.store` reset all
of it; this test forks explicitly (whatever the platform's default start method) and checks.
"""

from __future__ import annotations

import multiprocessing
from pathlib import Path
from typing import Any

import pytest

from coverset import storage
from coverset.facts import store as facts_store
from coverset.storage import S3BlobStore, store_from_url, transfer_pool
from coverset.storage.testing import fresh_blob_url


def _child(url: str, key: str, pipe: Any) -> None:
    """Runs in the forked child: report what the hooks left, then use the store for real."""
    try:
        blobs = store_from_url(url)
        assert isinstance(blobs, S3BlobStore)
        report: dict[str, Any] = {
            "client_reset": blobs._client is None,
            "pools_reset": not storage._POOLS,
            "states_reset": not facts_store._STATES,
        }
        data, _ = blobs.get_bytes(key)
        report["read"] = data
        blobs.put_bytes(key + ".child", b"from the child")
        report["pool_works"] = transfer_pool("io").submit(lambda: 7).result()
        pipe.send(report)
    except BaseException as exc:  # the parent asserts on it
        pipe.send({"error": repr(exc)})
    finally:
        pipe.close()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")  # fork() in a multi-threaded process
def test_a_forked_child_rebuilds_its_client_and_pools(tmp_path: Path) -> None:
    url = fresh_blob_url("fork")
    blobs = store_from_url(url)
    assert isinstance(blobs, S3BlobStore)
    blobs.put_bytes("one", b"from the parent")  # creates the client and an open connection
    assert blobs._client is not None
    transfer_pool("io").submit(lambda: None).result()  # a live pool with a thread
    facts_store._state(("run", "x", "1", "r"))  # some sync bookkeeping to inherit
    try:
        context = multiprocessing.get_context("fork")
    except ValueError:
        pytest.skip("no fork start method on this platform")
    parent_end, child_end = context.Pipe(duplex=False)
    process = context.Process(target=_child, args=(url, "one", child_end))
    process.start()
    child_end.close()
    report = parent_end.recv() if parent_end.poll(60) else {"error": "no report within 60 s"}
    process.join(10)
    assert "error" not in report, report
    assert report["client_reset"] and report["pools_reset"] and report["states_reset"]
    assert report["read"] == b"from the parent" and report["pool_works"] == 7
    assert blobs.get_bytes("one.child")[0] == b"from the child", "the child's own client wrote this"
    assert blobs._client is not None, "the parent keeps its client"
    blobs.delete(["one", "one.child"])
