"""``coverset-ci wait``: block a CI job on a coverset verdict and replay its results pytest-style.

(``coverset-ci upload``, upload mode's command, lives in :mod:`pytest_coverset.ci_upload`.)

The command never creates runs. It resolves the run for ``(repo_id, sha)`` through ``GET /v1/verdict``
(404 means "not there yet": keep polling until the timeout), streams ``/v1/runs/<id>/events`` as
``text/event-stream`` (reconnecting with ``Last-Event-ID``) and falls back to JSON polling
(``?since=<seq>``) when SSE is unavailable. Exit codes (plan Addendum A): 0 success, 1 failure,
2 interrupted/error/timed out/cancelled, 3 network or server error after retries, 5 no tests.

A ``failure`` verdict (the early first-failure one included) ends the wait at once with 1. Any
other verdict is followed by the run's ``done`` event, whose ``exit_code`` decides (the runner sends
``verdict`` ``neutral`` "no tests collected" and then ``done`` with exit code 5), so the wait reads
on until ``done`` (or ``--timeout``).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TextIO

from pytest_coverset.ci_http import (
    HEARTBEAT,
    NETWORK_ERRORS,
    ApiClient,
    ServerUnavailable,
    SseUnavailable,
    iter_sse,
    stream_lines,
)
from pytest_coverset.junit import write_junit

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_INTERRUPTED = 2
EXIT_SERVER_ERROR = 3
EXIT_NO_TESTS = 5

SSE_READ_TIMEOUT_S = 60.0
SSE_MAX_FAILURES = 3
PROGRESS_EVERY_S = 5.0
OUTCOME_LABELS = {
    "passed": "PASSED",
    "failed": "FAILED",
    "error": "ERROR",
    "skipped": "SKIPPED",
    "xfailed": "XFAIL",
    "xpassed": "XPASS",
}
COUNT_ORDER = ("failed", "error", "passed", "skipped", "xfailed", "xpassed", "not_run")


_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def printable(text: str) -> str:
    """``text`` without terminal escape sequences and C0/C1 control characters (newline and tab kept).

    Everything ``coverset-ci`` prints from the server (node ids, failure text, log lines, reasons) goes
    through here, so a hostile or merely colourful payload cannot move the cursor, retitle the
    terminal or hide lines in the CI log.
    """
    return _CONTROL.sub("", _ANSI_ESCAPE.sub("", text))


class TimedOut(Exception):
    """The overall ``--timeout`` elapsed."""


class Fatal(Exception):
    """A non-retriable answer (authentication, bad request) that ends the wait with ``code``."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


SERVER_EXIT_CODES = frozenset({EXIT_SUCCESS, EXIT_FAILURE, EXIT_INTERRUPTED, EXIT_NO_TESTS})


def exit_code_for(
    conclusion: str | None, exit_code: int | None = None, counts: Mapping[str, int] | None = None
) -> int:
    """Map a run's conclusion (plus the server's exit code and counts) to ``coverset-ci`` exit codes.

    A ``failure`` always exits 1. Otherwise an ``exit_code`` sent by the server (the ``done``
    event) wins, so an errored run (``neutral``, exit 2) is not mistaken for "no tests"; it can
    never turn a conclusion other than ``success`` into 0. Only without one do the counts decide:
    no test ran ⇒ 5.
    """
    if conclusion == "failure":
        return EXIT_FAILURE
    if exit_code is not None:
        if exit_code not in SERVER_EXIT_CODES:
            return EXIT_INTERRUPTED
        if exit_code == EXIT_SUCCESS and conclusion != "success":
            return EXIT_INTERRUPTED
        return exit_code
    ran = sum(v for k, v in (counts or {}).items() if k != "not_run") if counts is not None else None
    if ran == 0 and conclusion in (None, "success", "neutral"):
        return EXIT_NO_TESTS
    if conclusion == "success":
        return EXIT_SUCCESS
    return EXIT_INTERRUPTED


class Waiter:
    """Follows one run to its verdict and renders it."""

    def __init__(
        self,
        client: ApiClient,
        *,
        repo_id: int,
        sha: str,
        poll_interval: float,
        out: TextIO,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.repo_id = repo_id
        self.sha = sha
        self.poll_interval = poll_interval
        self.out = out
        self.clock = clock
        self.sleep = sleep
        self.started = clock()
        self.tests: list[dict[str, Any]] = []
        self.counts: dict[str, int] = {}
        self.last_seq = 0
        self.summary_url: str | None = None
        self.verdict: tuple[str, str | None] | None = None
        """The last non-failure ``verdict`` event (conclusion, reason), while waiting for ``done``."""
        self._last_progress = float("-inf")

    def run(self) -> int:
        """Resolve the run, follow its events and return the exit code."""
        try:
            verdict = self._resolve_run()
            self.summary_url = verdict.get("summary_url")
            run_id = str(verdict["run_id"])
            self._print(f"coverset run {run_id} for {self.sha[:12]}: {verdict.get('state', '?')}")
            return self._follow(run_id)
        except TimedOut:
            self._print(f"coverset-ci: timed out after {self.clock() - self.started:.0f} s")
            self._summary("timed out")
            return EXIT_INTERRUPTED
        except ServerUnavailable as exc:
            self._print(f"coverset-ci: server unavailable: {exc}")
            return EXIT_SERVER_ERROR
        except Fatal as exc:
            self._print(f"coverset-ci: {exc}")
            return exc.code

    def _print(self, text: str) -> None:
        self.out.write(printable(text) + "\n")
        self.out.flush()

    def _check_deadline(self) -> None:
        if self.client.remaining() <= 0:
            raise TimedOut

    def _pause(self, seconds: float) -> None:
        self.sleep(max(0.0, min(seconds, self.client.remaining())))
        self._check_deadline()

    def _resolve_run(self) -> dict[str, Any]:
        announced = False
        while True:
            self._check_deadline()
            response = self.client.get_json("/v1/verdict", {"repo_id": self.repo_id, "sha": self.sha})
            body = response.body if isinstance(response.body, dict) else {}
            if response.status == 200 and body.get("run_id"):
                return body
            if response.status in (200, 404):
                if not announced:
                    self._print(
                        f"coverset-ci: waiting for a coverset run of {self.sha[:12]} (repo {self.repo_id})"
                    )
                    announced = True
                self._pause(self.poll_interval)
            elif response.status == 429:
                self._pause(_retry_after(response.headers, self.poll_interval))
            elif response.status in (401, 403):
                raise Fatal(
                    f"not authorised by the coverset server (HTTP {response.status})", EXIT_SERVER_ERROR
                )
            else:
                raise Fatal(f"unexpected HTTP {response.status} from /v1/verdict: {body}", EXIT_SERVER_ERROR)

    def _follow(self, run_id: str) -> int:
        sse_failures = 0
        while sse_failures < SSE_MAX_FAILURES:
            self._check_deadline()
            seq_before = self.last_seq
            try:
                code = self._follow_sse(run_id)
            except SseUnavailable as exc:
                self._print(f"coverset-ci: event stream unavailable ({exc}); polling instead")
                break
            except NETWORK_ERRORS as exc:
                # A stream that delivered events before dropping restarts the count of failed streams.
                sse_failures = 1 if self.last_seq > seq_before else sse_failures + 1
                if sse_failures < SSE_MAX_FAILURES:
                    self._print(f"coverset-ci: event stream dropped ({type(exc).__name__}); reconnecting")
                    self._pause(min(self.poll_interval, 2.0**sse_failures))
                continue
            if code is not None:
                return code
            sse_failures = 0 if self.last_seq > seq_before else sse_failures + 1
        return self._follow_polling(run_id)

    def _follow_sse(self, run_id: str) -> int | None:
        last_id = str(self.last_seq) if self.last_seq else None
        timeout = max(1.0, min(SSE_READ_TIMEOUT_S, self.client.remaining()))
        response = self.client.open_stream(f"/v1/runs/{run_id}/events", last_id, timeout)
        with response:
            lines = stream_lines(response, lambda: min(SSE_READ_TIMEOUT_S, self.client.remaining()))
            for event in iter_sse(lines):
                # Checked on every line, pings included: a queued run whose stream only carries
                # `: ping` comments must still end at --timeout.
                self._check_deadline()
                if event is HEARTBEAT:
                    continue
                seq = _int_or_none(event.id)
                payload = _json_or_none(event.data)
                code = self._handle(seq, event.event, payload if isinstance(payload, dict) else {})
                if code is not None:
                    return code
        return None

    def _follow_polling(self, run_id: str) -> int:
        while True:
            self._check_deadline()
            response = self.client.get_json(f"/v1/runs/{run_id}/events", {"since": self.last_seq})
            body = response.body if isinstance(response.body, dict) else {}
            if response.status == 429:
                self._pause(_retry_after(response.headers, self.poll_interval))
                continue
            if response.status != 200:
                raise Fatal(f"unexpected HTTP {response.status} from the events endpoint", EXIT_SERVER_ERROR)
            for item in body.get("events") or []:
                if not isinstance(item, dict):
                    continue
                payload = item.get("payload")
                code = self._handle(
                    _int_or_none(item.get("seq")),
                    str(item.get("kind", "")),
                    payload if isinstance(payload, dict) else {},
                )
                if code is not None:
                    return code
            if body.get("done"):
                return self._final_from_run(run_id)
            self._pause(self.poll_interval)

    def _final_from_run(self, run_id: str) -> int:
        response = self.client.get_json(f"/v1/runs/{run_id}")
        body = response.body if isinstance(response.body, dict) else {}
        if response.status != 200:
            raise Fatal(f"unexpected HTTP {response.status} from /v1/runs/{run_id}", EXIT_SERVER_ERROR)
        counts = body.get("counts") if isinstance(body.get("counts"), dict) else None
        if counts:
            self.counts = {k: int(v) for k, v in counts.items() if isinstance(v, int)}
        conclusion = body.get("conclusion") or (self.verdict[0] if self.verdict is not None else None)
        self.summary_url = body.get("summary_url") or self.summary_url
        self._summary(str(conclusion), body.get("summary"))
        return exit_code_for(conclusion, None, counts)

    def _handle(self, seq: int | None, kind: str, payload: dict[str, Any]) -> int | None:
        if seq is not None:
            if seq <= self.last_seq:
                return None
            self.last_seq = seq
        if kind == "test":
            self._on_test(payload)
        elif kind == "progress":
            self._on_progress(payload)
        elif kind == "log":
            self._print(f"[shard {payload.get('shard', '?')}] {payload.get('text', '')}")
        elif kind == "verdict":
            conclusion = payload.get("conclusion")
            reason = payload.get("reason")
            self._print(f"coverset verdict: {conclusion} ({reason or ''})")
            if conclusion == "failure":  # always exit 1: no need to wait for the rest of the run
                self._summary(str(conclusion), reason)
                return exit_code_for(conclusion)
            # Any other conclusion: the ``done`` event that follows carries the exit code (5 when
            # no tests were collected, 2 for errors and neutral runs), so keep reading until it.
            self.verdict = (str(conclusion), reason if isinstance(reason, str) else None)
        elif kind == "done":
            counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else None
            if counts:
                self.counts = {k: int(v) for k, v in counts.items() if isinstance(v, int)}
            conclusion = payload.get("conclusion")
            exit_code = payload.get("exit_code")
            reason = self.verdict[1] if self.verdict is not None else None
            self._summary(str(conclusion), reason if conclusion != "success" else None)
            return exit_code_for(conclusion, exit_code if isinstance(exit_code, int) else None, counts)
        return None

    def _on_test(self, payload: dict[str, Any]) -> None:
        self.tests.append(payload)
        outcome = str(payload.get("outcome", "?"))
        self.counts[outcome] = self.counts.get(outcome, 0) + 1
        label = OUTCOME_LABELS.get(outcome, outcome.upper())
        flaky = " (flaky)" if payload.get("flaky") else ""
        self._print(f"{label} {payload.get('nodeid', '?')}{flaky}")
        if outcome in ("failed", "error") and payload.get("longrepr"):
            for line in str(payload["longrepr"]).splitlines():
                self._print("    " + line)

    def _on_progress(self, payload: dict[str, Any]) -> None:
        now = self.clock()
        done, total = payload.get("done"), payload.get("total")
        if done != total and now - self._last_progress < PROGRESS_EVERY_S:
            return
        self._last_progress = now
        self._print(
            f"[{done}/{total}] passed {payload.get('passed', 0)}, failed {payload.get('failed', 0)}"
            f" on {payload.get('shards', '?')} shards, {float(payload.get('elapsed_s') or 0):.1f} s"
        )

    def _summary(self, conclusion: str, reason: str | None = None) -> None:
        parts = [f"{self.counts[k]} {k.replace('_', ' ')}" for k in COUNT_ORDER if self.counts.get(k)]
        elapsed = self.clock() - self.started
        line = f" {', '.join(parts) or 'no tests'} in {elapsed:.2f}s ({conclusion}) "
        self._print(line.center(78, "="))
        if reason:
            self._print(f"reason: {reason}")
        if self.summary_url:
            self._print(f"details: {self.summary_url}")


def _retry_after(headers: Mapping[str, str], default: float) -> float:
    value = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    try:
        return float(value) if value is not None else default
    except ValueError:
        return default


def _int_or_none(value: object) -> int | None:
    try:
        return int(value) if value is not None else None  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None


def _json_or_none(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def build_parser() -> argparse.ArgumentParser:
    """The ``coverset-ci`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="coverset-ci", description="Wait for a coverset verdict, or upload a bundle, from CI."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    wait = commands.add_parser("wait", help="block until the coverset run for a commit reaches a verdict")
    wait.add_argument("--server", required=True, help="coverset base URL, e.g. https://coverset.example.com")
    wait.add_argument("--repo-id", required=True, type=int, help="coverset repository id")
    wait.add_argument("--sha", required=True, help="40-hex commit SHA (the PR head)")
    wait.add_argument(
        "--timeout", type=float, default=1800.0, help="give up after this many seconds (exit 2)"
    )
    wait.add_argument("--poll-interval", type=float, default=5.0, help="seconds between polls")
    wait.add_argument("--junit", metavar="PATH", help="also write JUnit XML here")
    wait.add_argument(
        "--token-env", metavar="NAME", help="environment variable holding a bearer (OIDC) token"
    )
    from pytest_coverset.ci_upload import add_parser as add_upload_parser

    add_upload_parser(commands)
    return parser


def main(argv: Sequence[str] | None = None, *, out: TextIO | None = None) -> int:
    """Console entry point; returns the process exit code."""
    args = build_parser().parse_args(argv)
    stream = out if out is not None else sys.stdout
    if args.command == "upload":
        from pytest_coverset.ci_upload import run_upload

        return run_upload(args, out=stream)
    token = os.environ.get(args.token_env) if args.token_env else None
    client = ApiClient(args.server, token=token or None, deadline=time.monotonic() + args.timeout)
    waiter = Waiter(client, repo_id=args.repo_id, sha=args.sha, poll_interval=args.poll_interval, out=stream)
    code = waiter.run()
    if args.junit:
        write_junit(args.junit, waiter.tests)
    return code


if __name__ == "__main__":
    sys.exit(main())
