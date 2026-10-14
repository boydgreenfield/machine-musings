"""``instance_id`` on filesystems without hard links (Modal Volumes answer EPERM to ``os.link``)."""

from __future__ import annotations

import errno
import os
import threading
import uuid
from pathlib import Path

import pytest

from coverset.executors import modal_
from coverset.executors.modal_ import instance_id


def _no_links(error: OSError) -> object:
    def link(src: object, dst: object, *args: object, **kwargs: object) -> None:
        raise error

    return link


@pytest.mark.parametrize(
    "error",
    [
        PermissionError(errno.EPERM, "Operation not permitted"),
        OSError(errno.ENOTSUP, "Operation not supported"),
        OSError(errno.EOPNOTSUPP, "Operation not supported on socket"),
    ],
)
def test_an_exclusive_create_replaces_the_hard_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    monkeypatch.setattr(os, "link", _no_links(error))
    first = instance_id(tmp_path / "data")
    assert str(uuid.UUID(first)) == first
    assert instance_id(tmp_path / "data") == first
    assert (tmp_path / "data" / "instance_id").read_text().strip() == first
    assert [p.name for p in (tmp_path / "data").iterdir()] == ["instance_id"], "no temporary file left"


def test_other_link_errors_still_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "link", _no_links(OSError(errno.EIO, "I/O error")))
    with pytest.raises(OSError, match="I/O error"):
        instance_id(tmp_path / "data")


def test_concurrent_creators_agree_without_hard_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every racer proposes its own UUID; the exclusive create lets exactly one win."""
    monkeypatch.setattr(os, "link", _no_links(PermissionError(errno.EPERM, "Operation not permitted")))
    barrier = threading.Barrier(8)
    results: list[str] = []

    def race() -> None:
        barrier.wait()
        results.append(instance_id(tmp_path / "data"))

    threads = [threading.Thread(target=race) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 8 and len(set(results)) == 1


def test_a_loser_waits_for_the_winners_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The file exists but is still empty (the winner created it and has not written yet)."""
    data = tmp_path / "data"
    data.mkdir()
    path = data / "instance_id"
    path.touch()
    winner = str(uuid.uuid4())
    monkeypatch.setattr(os, "link", _no_links(PermissionError(errno.EPERM, "Operation not permitted")))
    timer = threading.Timer(0.05, lambda: path.write_text(winner + "\n"))
    timer.start()
    try:
        assert instance_id(data) == winner
    finally:
        timer.cancel()


def test_an_empty_file_that_stays_empty_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "instance_id").touch()
    monkeypatch.setattr(modal_, "INSTANCE_WAIT_S", 0.05)
    with pytest.raises(RuntimeError, match="holds no instance id"):
        instance_id(data)
