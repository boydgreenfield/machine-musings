"""Review bundles: redaction, path exclusion, size caps, hashing and what goes in per function."""

from __future__ import annotations

import pytest
from ai_helpers import (
    ALLOCATE_PATH,
    EMAIL_PATH,
    EMAIL_TESTS,
    REFUND_PATH,
    BundleFactory,
    demo_invariants,
    demo_source,
    obs,
    region_of,
)

from coverset.ai.bundle import build_review_bundle, bundle_hash, canonical_json, is_excluded_path
from coverset.ai.schema import ReviewBundle
from coverset.analysis.shapes import Catalogue
from coverset.config import Settings
from coverset.lang.base import FunctionChange

GITHUB_TOKEN = "ghp_" + "A1b2C3d4" * 5
PLAIN_SECRET = "plain-secret-value-0451"


def reviewer_view_size(bundle: ReviewBundle) -> int:
    """Characters of canonical JSON the cap applies to (bookkeeping fields excluded)."""
    return len(canonical_json(bundle.model_dump(mode="json", exclude={"bundle_hash", "dropped"})))


# --- redaction --------------------------------------------------------------------------------


def test_token_in_a_test_body_is_redacted(email_bundle: BundleFactory) -> None:
    body = f'def test_with_token():\n    client = Client(token="{GITHUB_TOKEN}")\n    assert client\n'
    bundle = email_bundle(tests=[("tests/test_emailcheck.py::test_with_token", body)])
    text = bundle.model_dump_json()
    assert GITHUB_TOKEN not in text
    assert "ghp_[REDACTED]" in bundle.functions[0].covering_tests[0].body


@pytest.mark.security
def test_live_settings_secrets_are_redacted_everywhere(email_bundle: BundleFactory, tmp_path) -> None:
    settings = Settings.for_tests(data_dir=tmp_path / "d", github_token=PLAIN_SECRET)
    source = demo_source(EMAIL_PATH).replace(
        "    match = _VENDOR_PATTERN", f'    debug = "{PLAIN_SECRET}"\n    match = _VENDOR_PATTERN'
    )
    bundle = email_bundle(
        source=source,
        settings=settings,
        tests=[("tests/test_emailcheck.py::test_x", f"def test_x():\n    assert '{PLAIN_SECRET}'\n")],
    )
    text = bundle.model_dump_json()
    assert PLAIN_SECRET not in text
    assert "[REDACTED]" in bundle.functions[0].source


@pytest.mark.security
def test_secret_in_an_example_value_is_redacted(email_bundle: BundleFactory) -> None:
    region = region_of(EMAIL_PATH, demo_source(EMAIL_PATH), "validate_email")
    shapes = [
        obs(region, "email", "str|ascii|len:11-100", tests=[EMAIL_TESTS[0][0]], example=repr(GITHUB_TOKEN))
    ]
    bundle = email_bundle(shapes=shapes)
    assert GITHUB_TOKEN not in bundle.model_dump_json()


# --- exclusion ---------------------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.parametrize(
    ("path", "excluded"),
    [
        (".env", True),
        (".env.production", True),
        ("config/.env.local", True),
        ("deploy/github-app.pem", True),
        ("keys/server.key", True),
        ("secrets/tokens.py", True),
        ("src/app/secrets/vault.py", True),
        ("src/app/secrets.py", False),
        ("src/ledger/accounts/emailcheck.py", False),
        ("./secrets/x.py", True),
    ],
)
def test_always_excluded_paths(path: str, excluded: bool) -> None:
    assert is_excluded_path(path) is excluded


@pytest.mark.security
def test_user_exclude_globs_match_path_or_basename() -> None:
    assert is_excluded_path("src/vendor/lib.py", ["src/vendor/*"])
    assert is_excluded_path("src/a/b/generated_pb2.py", ["*_pb2.py"])
    assert is_excluded_path("third_party/x/y.py", ["third_party/"])
    assert not is_excluded_path("src/ledger/x.py", ["src/vendor/*"])


def test_function_under_an_excluded_glob_is_left_out(email_bundle: BundleFactory, tmp_path) -> None:
    settings = Settings.for_tests(data_dir=tmp_path / "d", ai_exclude_globs=["src/ledger/accounts/*"])
    bundle = email_bundle(settings=settings)
    assert bundle.functions == []
    assert bundle.dropped == {"excluded_paths": 1}
    assert "validate_email" not in bundle.model_dump_json()


@pytest.mark.security
def test_tests_in_excluded_paths_are_left_out(email_bundle: BundleFactory) -> None:
    tests = [*EMAIL_TESTS, ("secrets/test_leak.py::test_leak", "def test_leak():\n    assert True\n")]
    bundle = email_bundle(tests=tests)
    nodeids = [t.nodeid for t in bundle.functions[0].covering_tests]
    assert "secrets/test_leak.py::test_leak" not in nodeids
    assert bundle.functions[0].covering_test_count == 2
    assert bundle.dropped["excluded_paths"] >= 1


@pytest.mark.security
def test_function_in_a_secrets_directory_is_never_bundled(ai_settings: Settings) -> None:
    source = "def load():\n    return 1\n"
    region = region_of("secrets/loader.py", source, "load")
    bundle = build_review_bundle(
        repo_name="acme/app",
        head_sha=None,
        pr_number=None,
        changes=[FunctionChange(region, None, "added")],
        sources={"secrets/loader.py": source},
        diff_hunks={},
        patch_rows=None,
        covering_tests={},
        input_covs={},
        shapes={},
        invariants=[],
        settings=ai_settings,
    )
    assert bundle.functions == []
    assert "secrets/loader.py" not in bundle.model_dump_json()


# --- size cap ----------------------------------------------------------------------------------


def _assertion_free_tests(n: int, pad: int) -> list[tuple[str, str]]:
    filler = "    # " + "x" * pad + "\n"
    return [
        (
            f"tests/test_emailcheck.py::test_smoke_{i}",
            f"def test_smoke_{i}():\n{filler}    validate_email('a@b.co')\n",
        )
        for i in range(n)
    ]


def _capped(email_bundle: BundleFactory, tmp_path, cap: int) -> ReviewBundle:
    settings = Settings.for_tests(data_dir=tmp_path / f"cap{cap}", ai_max_bundle_chars=cap)
    return email_bundle(settings=settings, tests=_assertion_free_tests(6, 600))


@pytest.mark.security
def test_size_cap_drops_oracle_notes_first(email_bundle: BundleFactory, tmp_path) -> None:
    full = _capped(email_bundle, tmp_path, 10_000_000)
    assert len(full.functions[0].oracle_notes) >= 6  # docstring "Raises" + six assertion-free tests
    cap = reviewer_view_size(full) - 20
    bundle = _capped(email_bundle, tmp_path, cap)
    assert reviewer_view_size(bundle) <= cap
    assert bundle.functions[0].oracle_notes == []
    assert len(bundle.functions[0].covering_tests) == 6
    assert set(bundle.dropped) == {"oracle_notes"}


def test_size_cap_then_drops_extra_tests_before_touching_sources(
    email_bundle: BundleFactory, tmp_path
) -> None:
    full = _capped(email_bundle, tmp_path, 10_000_000)
    without_notes = full.model_copy(deep=True)
    without_notes.functions[0].oracle_notes = []
    cap = reviewer_view_size(without_notes) - 1_000
    bundle = _capped(email_bundle, tmp_path, cap)
    fn = bundle.functions[0]
    assert reviewer_view_size(bundle) <= cap
    assert 1 <= len(fn.covering_tests) < 6
    assert bundle.dropped["tests"] >= 1
    assert "source_chars" not in bundle.dropped
    assert fn.source == full.functions[0].source
    assert fn.covering_test_count == 6  # the count still says how many tests ran the body


def test_size_cap_truncates_long_sources_last(email_bundle: BundleFactory, tmp_path) -> None:
    filler = "".join(f'    _note_{i} = "{"z" * 60}"\n' for i in range(150))
    long_source = demo_source(EMAIL_PATH).replace(
        "    match = _VENDOR_PATTERN", filler + "    match = _VENDOR_PATTERN"
    )
    settings = Settings.for_tests(data_dir=tmp_path / "small", ai_max_bundle_chars=6_000)
    bundle = email_bundle(source=long_source, settings=settings, tests=_assertion_free_tests(6, 600))
    fn = bundle.functions[0]
    assert reviewer_view_size(bundle) <= 6_000
    assert len(fn.covering_tests) == 1
    assert bundle.dropped["source_chars"] > 0
    assert "[truncated" in fn.source
    assert fn.source.startswith("def validate_email")


def test_size_cap_drops_whole_functions_as_a_last_resort(email_bundle: BundleFactory, tmp_path) -> None:
    settings = Settings.for_tests(data_dir=tmp_path / "tiny", ai_max_bundle_chars=300)
    bundle = email_bundle(settings=settings)
    assert bundle.functions == []
    assert bundle.dropped["functions"] == 1


# --- hashing -----------------------------------------------------------------------------------


def test_bundle_hash_is_stable_and_ignores_run_identifiers(email_bundle: BundleFactory) -> None:
    first = email_bundle()
    again = email_bundle()
    other_run = email_bundle(head_sha="c" * 40, pr_number=99)
    assert first.bundle_hash == again.bundle_hash == bundle_hash(first)
    assert len(first.bundle_hash) == 64
    assert other_run.bundle_hash == first.bundle_hash


def test_bundle_hash_changes_when_the_function_or_its_tests_change(email_bundle: BundleFactory) -> None:
    base = email_bundle()
    edited = email_bundle(
        source=demo_source(EMAIL_PATH).replace("return match.group(1).lower()", "return match.group(1)")
    )
    new_test = email_bundle(
        tests=[*EMAIL_TESTS, ("tests/test_emailcheck.py::test_new", "def test_new(): pass")]
    )
    assert len({base.bundle_hash, edited.bundle_hash, new_test.bundle_hash}) == 3


def test_for_function_gives_a_single_function_bundle_with_its_own_hash(email_bundle: BundleFactory) -> None:
    bundle = email_bundle()
    key = bundle.functions[0].func_key
    sub = bundle.for_function(key)
    assert [fn.func_key for fn in sub.functions] == [key]
    assert sub.bundle_hash == bundle_hash(sub)
    assert sub.context == bundle.context
    with pytest.raises(KeyError):
        bundle.for_function("nope")


# --- contents ----------------------------------------------------------------------------------


def test_function_entry_has_signature_source_tests_shapes_and_input_cov(email_bundle: BundleFactory) -> None:
    fn = email_bundle().functions[0]
    assert fn.qualname == "validate_email"
    assert fn.module == "ledger.accounts.emailcheck"
    assert fn.signature_context == "def validate_email(email: str) -> str:"
    assert fn.source.startswith("def validate_email(email: str) -> str:")
    assert fn.source.rstrip().endswith("return match.group(1).lower()")
    assert [t.nodeid for t in fn.covering_tests] == [nodeid for nodeid, _ in EMAIL_TESTS]
    assert fn.observed_shapes[0].shape_key == "str|ascii|kind:email/plain|len:11-100"
    assert fn.input_cov is not None
    assert "email:multi-label-domain" in [m.name for m in fn.input_cov.missing]
    hint = next(m for m in fn.input_cov.missing if m.name == "email:multi-label-domain")
    assert hint.hypothesis_hint.startswith("st.")
    assert fn.input_cov.missing_reason == "never exercised"


def test_only_diff_hunks_touching_the_function_are_kept(email_bundle: BundleFactory) -> None:
    diff = (
        "--- a/src/ledger/accounts/emailcheck.py\n+++ b/src/ledger/accounts/emailcheck.py\n"
        "@@ -5 +5 @@\n-_VENDOR_PATTERN = re.compile(r'old')\n+_VENDOR_PATTERN = re.compile(r'new')\n"
        "@@ -18 +18 @@\n-    return match.group(1)\n+    return match.group(1).lower()\n"
    )
    fn = email_bundle(diff=diff).functions[0]
    assert "@@ -18 +18 @@" in fn.diff_hunk
    assert "_VENDOR_PATTERN = re.compile" not in fn.diff_hunk


def test_deleted_trivial_and_class_level_changes_are_skipped(ai_settings: Settings) -> None:
    source = "class Box:\n    def size(self):\n        return 1\n\ndef helper():\n    return 2\n"
    from coverset.analysis.functions import index_functions

    regions = {r.qualname: r for r in index_functions("src/pkg/box.py", source)}
    changes = [
        FunctionChange(regions["Box"], regions["Box"], "modified"),
        FunctionChange(regions["helper"], regions["helper"], "trivial"),
        FunctionChange(None, regions["Box.size"], "deleted"),
        FunctionChange(regions["Box.size"], regions["Box.size"], "modified"),
    ]
    bundle = build_review_bundle(
        repo_name="acme/box",
        head_sha=None,
        pr_number=None,
        changes=changes,
        sources={"src/pkg/box.py": source},
        diff_hunks={},
        patch_rows=None,
        covering_tests={},
        input_covs={},
        shapes={},
        invariants=[],
        settings=ai_settings,
    )
    assert [fn.qualname for fn in bundle.functions] == ["Box.size"]
    assert bundle.dropped["skipped_functions"] == 3
    method = bundle.functions[0]
    assert method.signature_context.splitlines()[0] == "class Box:"
    assert method.module == "pkg.box"


def _single(path: str, qualname: str, settings: Settings, **kwargs) -> ReviewBundle:
    source = demo_source(path)
    region = region_of(path, source, qualname)
    return build_review_bundle(
        repo_name="demo/ledger",
        head_sha="d" * 40,
        pr_number=1,
        changes=[FunctionChange(region, None, "added")],
        sources={path: source},
        diff_hunks={},
        patch_rows=None,
        covering_tests=kwargs.pop("covering_tests", {}),
        input_covs={},
        shapes={},
        invariants=demo_invariants(),
        settings=settings,
        **kwargs,
    )


def test_invariants_in_scope_include_the_target_and_called_targets(ai_settings: Settings) -> None:
    allocate = _single(
        ALLOCATE_PATH,
        "allocate_cents",
        ai_settings,
        covering_tests={"allocate_cents": [("tests/test_allocate.py::test_allocate_cents_examples", "")]},
        invariant_links={"cents_conserved": ["tests/test_allocate.py::test_allocate_cents_examples"]},
    ).functions[0]
    refund = _single(REFUND_PATH, "refund", ai_settings).functions[0]
    assert [(i.id, i.relation, i.severity) for i in allocate.invariants] == [
        ("cents_conserved", "target", "error")
    ]
    assert allocate.invariants[0].linked_tests == ["tests/test_allocate.py::test_allocate_cents_examples"]
    assert allocate.invariants[0].verify == ["property", "test"]
    assert [(i.id, i.relation) for i in refund.invariants] == [("cents_conserved", "calls_target")]
    assert refund.error_invariants
    assert any("counterexample" in note for note in refund.oracle_notes)


def test_patch_rows_add_line_pct_and_tests_without_bodies(ai_settings: Settings) -> None:
    from coverset.analysis.patch import FunctionRow

    source = demo_source(EMAIL_PATH)
    region = region_of(EMAIL_PATH, source, "validate_email")
    change = FunctionChange(region, region, "modified")
    row = FunctionRow(change, ["tests/test_signup.py::test_signup_allowed"], 100.0, False)
    cov = Catalogue().function_input_cov(region, [], None)
    bundle = build_review_bundle(
        repo_name="demo/ledger",
        head_sha=None,
        pr_number=None,
        changes=[change],
        sources={EMAIL_PATH: source},
        diff_hunks={},
        patch_rows=[row],
        covering_tests={},
        input_covs={region.func_key: cov},
        shapes={},
        invariants=[],
        settings=ai_settings,
    )
    fn = bundle.functions[0]
    assert fn.line_pct == 100.0
    assert fn.covering_test_count == 1
    assert fn.covering_tests[0].nodeid == "tests/test_signup.py::test_signup_allowed"
    assert fn.covering_tests[0].body == ""
