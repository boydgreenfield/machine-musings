"""Generate ``ledger-prod.jsonl``: three windows of synthetic production traffic through the demo app.

The demo app (``examples/demo-app``) runs under the real probe (``packages/coverset-probe``, put on
``sys.path`` from the source tree) with a seeded traffic mix shaped like a small SaaS's signup,
billing and scheduling endpoints, so the export exercises the whole path: head sampling by trace id,
subjects as k-anonymity witnesses, k = 5, call vectors, flows, a function no test calls. Every
random choice is seeded and the clock is fixed, so the rows are reproducible (the probe's own health
numbers under ``probe``, such as ``overhead`` and ``fast_call_ns``, are measured and vary):

    uv run python fixtures/flows/generate.py            # rewrites fixtures/flows/ledger-prod.jsonl

The mix (per request kind, per window): signups (validate_email through
check_if_new_user_signup_allowed) with 70 % plain addresses, 15 % on a subdomain
(``bob@eng.acme.com``, the demo's PR #2 bug: the vendor pattern rejects them), 8 % plus-tagged,
5 % upper-case, 2 % internationalised; durations (parse_duration) 50 % ``5m``, 20 % ``100ms``,
15 % ``1h30m``, 15 % ``1.5h``; invoices (allocate_cents) with 10 % zero weights; calendar merges
(merge_intervals) with 30 % unsorted input; and initials(), which no demo test calls.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT / "packages" / "coverset-probe" / "src"),
    str(ROOT / "examples" / "demo-app" / "src"),
]

import coverset_probe  # noqa: E402
from coverset_probe import JsonlExporter  # noqa: E402

OUT = Path(__file__).with_name("ledger-prod.jsonl")
START = 1_791_885_600.0  # 2026-10-13T10:00:00Z
WINDOW_S = 60.0
REQUESTS_PER_WINDOW = 600
USERS = [f"user-{n}" for n in range(300)]
NAMES = ["alice", "bob", "carol", "dave", "erin", "frank", "grace", "heidi"]
DOMAINS = ["acme.com", "example.org", "initech.io", "globex.net"]


def _email(rng: random.Random) -> str:
    name, domain = rng.choice(NAMES), rng.choice(DOMAINS)
    roll = rng.random()
    if roll < 0.70:
        return f"{name}@{domain}"
    if roll < 0.85:
        return f"{name}@{rng.choice(['eng', 'ops', 'eu'])}.{domain}"
    if roll < 0.93:
        return f"{name}+{rng.choice(['news', 'billing'])}@{domain}"
    if roll < 0.98:
        return f"{name.title()}@{domain.upper()}"
    return f"{name}@bücher.de"


def _duration(rng: random.Random) -> str:
    return rng.choices(["5m", "100ms", "1h30m", "1.5h"], weights=[50, 20, 15, 15])[0]


def _request(rng: random.Random) -> None:
    from ledger.accounts.signup import check_if_new_user_signup_allowed
    from ledger.billing.allocate import allocate_cents
    from ledger.durations.parse import parse_duration
    from ledger.intervals.merge import merge_intervals
    from ledger.util.text import initials

    kind = rng.choices(["signup", "duration", "invoice", "calendar"], weights=[45, 25, 20, 10])[0]
    try:
        if kind == "signup":
            email = _email(rng)
            check_if_new_user_signup_allowed(email, {"initech.io"})
            initials(f"{rng.choice(NAMES).title()} {rng.choice(NAMES).title()}")
        elif kind == "duration":
            parse_duration(_duration(rng))
        elif kind == "invoice":
            weights = [float(rng.randint(1, 5)) for _ in range(rng.randint(2, 4))]
            if rng.random() < 0.10:
                weights[0] = 0.0
            allocate_cents(rng.randint(100, 50_000), weights)
        else:
            intervals = sorted((s, s + rng.randint(1, 5)) for s in rng.sample(range(0, 40), 4))
            if rng.random() < 0.30:
                rng.shuffle(intervals)
            merge_intervals(intervals)
    except ValueError:
        pass  # production code raises too; the probe recorded the arguments on entry


def main() -> int:
    """Run three windows of traffic under the probe and write the export."""
    rng = random.Random(20261013)
    now = [START]
    OUT.unlink(missing_ok=True)
    probe = coverset_probe.start(
        modules=["ledger"],
        service="ledger-api",
        env="production",
        release="demo-main",
        source_root=ROOT / "examples" / "demo-app",
        sample_rate=1.0,
        trace_sample_rate=0.5,
        k_anonymity=5,
        window_s=0,
        export=JsonlExporter(OUT),
        seed=7,
        clock=lambda: now[0],
        max_overhead=1.0,
    )
    assert probe.active, probe.disabled_reason
    for _ in range(3):
        for _ in range(REQUESTS_PER_WINDOW):
            trace_id = f"{rng.getrandbits(128):032x}"
            with coverset_probe.trace(
                f"00-{trace_id}-{rng.getrandbits(64) or 1:016x}-01", subject=rng.choice(USERS)
            ):
                _request(rng)
        now[0] += WINDOW_S
        probe.flush()
    coverset_probe.stop(flush=False)
    OUT.chmod(0o644)  # the exporter writes 0600, right for production logs; this is a public fixture
    print(f"wrote {OUT.relative_to(ROOT)}: {len(OUT.read_text().splitlines())} windows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
