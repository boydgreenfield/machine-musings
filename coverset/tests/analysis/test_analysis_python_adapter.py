"""PythonAdapter: [tool.coverset] parsing, environment spec and the pytest command."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from analysis_helpers import obs

from coverset.analysis.functions import index_functions
from coverset.analysis.shapes import Catalogue
from coverset.facts.untrusted import BundleLimits
from coverset.lang import python as python_lang
from coverset.lang.base import LanguageAdapter
from coverset.lang.python import ConfigError, CoversetConfig, EnvSpecError, PythonAdapter, read_tool_config

DEMO_APP = Path(__file__).parents[2] / "examples" / "demo-app"
FAKE_REQUIREMENTS = "pytest==9.1.1\nhypothesis==6.168.5\n"


def test_adapter_satisfies_protocol() -> None:
    assert isinstance(PythonAdapter(), LanguageAdapter)
    assert PythonAdapter().name == "python"


def test_defaults_without_a_checkout() -> None:
    assert read_tool_config("") == CoversetConfig()
    cfg = read_tool_config('[tool.pytest.ini_options]\ntestpaths = ["checks"]\n')
    assert cfg.testpaths == ("checks",)
    assert (cfg.max_shards, cfg.target_shard_seconds, cfg.fail_fast) == (16, 20.0, "finish_in_background")
    assert (cfg.invariants, cfg.tracked_globs, cfg.reorder) == (
        "invariants.yaml",
        ("uv.lock", "conftest.py"),
        True,
    )


def test_explicit_values_and_unknown_keys() -> None:
    text = """
    [tool.coverset]
    source = ["ledger", "ledger_extras"]
    source_root = "src"
    testpaths = "tests"
    python = "3.12"
    max_shards = 4
    target_shard_seconds = 7
    fail_fast = "cancel_remaining"
    invariants = "rules/invariants.yaml"
    trace_modules = ["vendor.client"]
    tracked_globs = ["uv.lock"]
    reorder = false
    future_option = 1
    """
    cfg = read_tool_config(text.replace("\n    ", "\n"))
    assert cfg == CoversetConfig(
        source=("ledger", "ledger_extras"),
        source_root="src",
        testpaths=("tests",),
        python="3.12",
        max_shards=4,
        target_shard_seconds=7.0,
        fail_fast="cancel_remaining",
        invariants="rules/invariants.yaml",
        trace_modules=("vendor.client",),
        tracked_globs=("uv.lock",),
        reorder=False,
        unknown_keys=("future_option",),
    )


@pytest.mark.parametrize(
    "body",
    [
        'source_root = "../outside"',
        'source_root = "/etc"',
        'invariants = "a/../../b.yaml"',
        'source = ["not a module"]',
        "source = [1]",
        'python = "3.11"',
        'python = "three"',
        "max_shards = 0",
        "max_shards = true",
        "target_shard_seconds = -1",
        'fail_fast = "panic"',
        'reorder = "yes"',
        'tracked_globs = ["C:\\\\x"]',
    ],
)
def test_invalid_config_is_rejected(body: str) -> None:
    with pytest.raises(ConfigError):
        read_tool_config(f"[tool.coverset]\n{body}\n")


def test_unparseable_toml() -> None:
    with pytest.raises(ConfigError, match="does not parse"):
        read_tool_config("[tool.coverset\n")


def test_inference_from_checkout(tmp_path: Path) -> None:
    (tmp_path / "src" / "ledger").mkdir(parents=True)
    (tmp_path / "src" / "ledger" / "__init__.py").write_text("")
    (tmp_path / "src" / "tests").mkdir()
    (tmp_path / "src" / "tests" / "__init__.py").write_text("")
    (tmp_path / "src" / "notapkg").mkdir()
    (tmp_path / ".python-version").write_text("3.12.7\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    cfg = PythonAdapter().load_config(tmp_path)
    assert (cfg.source_root, cfg.source, cfg.python) == ("src", ("ledger",), "3.12")
    assert PythonAdapter().detect(tmp_path)


def test_detect(tmp_path: Path) -> None:
    assert not PythonAdapter().detect(tmp_path)
    (tmp_path / "main.py").write_text("print(1)\n")
    assert PythonAdapter().detect(tmp_path)


def _fake_uv(tmp_path: Path, *, exit_code: int = 0) -> Path:
    log = tmp_path / "uv-args.txt"
    script = tmp_path / "fake-uv"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$PWD" "$@" > "{log}"\n'
        f'env > "{log}.env"\n'
        f"printf '{FAKE_REQUIREMENTS.replace(chr(10), '\\n')}'\n"
        "echo 'uv: boom' >&2\n"
        f"exit {exit_code}\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        '[tool.coverset]\nsource = ["ledger"]\nsource_root = "src"\npython = "3.13"\n'
    )
    (repo / "uv.lock").write_text("version = 1\n")
    return repo


def test_env_spec_hashes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("COVERSET_GITHUB_TOKEN", "ghp_test")
    repo = _repo(tmp_path)
    wheel = tmp_path / "pytest_coverset-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel-bytes")
    adapter = PythonAdapter(uv=str(_fake_uv(tmp_path)))
    spec = adapter.env_spec(repo, {"plugin_wheel": str(wheel)})
    args = (tmp_path / "uv-args.txt").read_text().splitlines()
    assert Path(args[0]).resolve() == repo.resolve()
    assert args[1:] == [
        "export",
        "--frozen",
        "--offline",
        "--no-config",
        "--no-hashes",
        "--no-emit-project",
        "--no-emit-workspace",
        "--no-emit-local",
        "--all-groups",
    ]
    child_env = dict(
        line.split("=", 1) for line in (tmp_path / "uv-args.txt.env").read_text().splitlines() if "=" in line
    )
    assert "ANTHROPIC_API_KEY" not in child_env and "COVERSET_GITHUB_TOKEN" not in child_env
    assert child_env["HOME"] != os.environ.get("HOME") and child_env["UV_NO_CONFIG"] == "1"
    assert spec.requirements_txt == FAKE_REQUIREMENTS
    assert spec.python == "3.13" and spec.source_root == "src" and spec.plugin_wheel == wheel
    assert spec.lock_hash == hashlib.sha256(b"version = 1\n").hexdigest()
    wheel_hash = hashlib.sha256(b"wheel-bytes").hexdigest()
    expected = hashlib.sha256(f"{FAKE_REQUIREMENTS}\x003.13\x00{wheel_hash}".encode()).hexdigest()
    assert spec.env_hash == expected
    without_wheel = adapter.env_spec(repo, {"python": "3.12"})
    assert without_wheel.python == "3.12"
    assert (
        without_wheel.env_hash == hashlib.sha256(f"{FAKE_REQUIREMENTS}\x003.12\x00none".encode()).hexdigest()
    )


def test_env_spec_errors(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(EnvSpecError, match="boom"):
        PythonAdapter(uv=str(_fake_uv(tmp_path, exit_code=2))).env_spec(repo, {})
    (repo / "uv.lock").unlink()
    with pytest.raises(EnvSpecError, match=r"no uv\.lock"):
        PythonAdapter(uv=str(_fake_uv(tmp_path))).env_spec(repo, {})


@pytest.mark.skipif(
    shutil.which("uv") is None or not (DEMO_APP / "uv.lock").is_file(), reason="needs uv and demo app"
)
def test_env_spec_on_the_demo_app_with_real_uv() -> None:
    spec = PythonAdapter().env_spec(DEMO_APP, {})
    assert "hypothesis==6.168.5" in spec.requirements_txt
    assert "ledger" not in spec.requirements_txt.split("\n", 2)[-1]
    assert spec.env_hash == PythonAdapter().env_spec(DEMO_APP, {}).env_hash


def test_test_command() -> None:
    adapter = PythonAdapter()
    assert adapter.test_command("/w/shard-0.args", "/w/scores.json", "/w/out", {"sources": ["ledger"]}) == [
        "python",
        "-m",
        "pytest",
        "@/w/shard-0.args",
        "--coverset-out",
        "/w/out",
        "--coverset-scores",
        "/w/scores.json",
        "--coverset-source",
        "ledger",
        "--continue-on-collection-errors",
        "-p",
        "no:cacheprovider",
        "-q",
    ]
    extra = {
        "python": "/venv/bin/python",
        "sources": ["a", "b"],
        "trace_modules": ["vendor"],
        "full_trace_file": "/w/full.txt",
        "shapes": "off",
        "stop_file": "/w/STOP",
        "xdist": True,
        "pytest_cov": True,
    }
    cmd = adapter.test_command("/w/a", None, "/w/o", extra)
    assert cmd[:6] == ["/venv/bin/python", "-m", "pytest", "@/w/a", "--coverset-out", "/w/o"]
    assert "--coverset-scores" not in cmd
    assert cmd[6:] == [
        "--coverset-source",
        "a",
        "--coverset-source",
        "b",
        "--coverset-trace-module",
        "vendor",
        "--coverset-full-trace",
        "/w/full.txt",
        "--coverset-shapes",
        "off",
        "--coverset-stop-file",
        "/w/STOP",
        "--continue-on-collection-errors",
        "-p",
        "no:cacheprovider",
        "-q",
        "-n",
        "0",
        "--no-cov",
    ]


def test_delegations(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    adapter = PythonAdapter()
    sentinel = object()
    monkeypatch.setattr(python_lang, "read_python_bundle", lambda path, **kwargs: (sentinel, path, kwargs))
    assert adapter.read_bundle(tmp_path) == (
        sentinel,
        tmp_path,
        {"limits": None, "source_root": None, "head_paths": None},
    )
    limits = BundleLimits(max_bundle_bytes=1024)
    passed = adapter.read_bundle(tmp_path, limits=limits, source_root=tmp_path, head_paths={"a.py"})
    assert passed[2] == {"limits": limits, "source_root": tmp_path, "head_paths": {"a.py"}}
    regions = adapter.index_functions("m.py", "def f():\n    return 1\n")
    assert [r.qualname for r in regions] == ["<module>", "f"]
    assert isinstance(adapter.shape_catalogue(), Catalogue)


def _workspace(root: Path) -> Path:
    """A uv workspace whose root depends on a workspace member and an editable path dependency."""
    build = '\n[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
    (root / "packages" / "alpha" / "alpha").mkdir(parents=True)
    (root / "libs" / "beta" / "beta").mkdir(parents=True)
    for rel, name in (("packages/alpha", "alpha"), ("libs/beta", "beta")):
        (root / rel / name / "__init__.py").write_text("")
        (root / rel / "pyproject.toml").write_text(
            f'[project]\nname = "{name}"\nversion = "0.1.0"\nrequires-python = ">=3.12"\n{build}'
        )
    (root / "pyproject.toml").write_text(
        '[project]\nname = "root"\nversion = "0.1.0"\nrequires-python = ">=3.12"\n'
        'dependencies = ["alpha", "beta"]\n\n[tool.uv.workspace]\nmembers = ["packages/*"]\n\n'
        '[tool.uv.sources]\nalpha = { workspace = true }\nbeta = { path = "libs/beta", editable = true }\n'
        + build
    )
    return root


@pytest.mark.security
@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")
def test_env_spec_never_emits_workspace_or_local_packages(tmp_path: Path) -> None:
    repo = _workspace(tmp_path / "ws")
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "UV_CACHE_DIR": str(tmp_path / "cache")}
    lock = subprocess.run(
        ["uv", "lock", "--offline", "--python", sys.executable],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if lock.returncode != 0:
        pytest.skip(f"uv lock failed offline: {lock.stderr[-300:]}")
    requirements = PythonAdapter().env_spec(repo, {"python": "3.13"}).requirements_txt
    lines = [line.strip() for line in requirements.splitlines() if line.strip() and not line.startswith("#")]
    assert not [line for line in lines if line.startswith(("-e", ".", "/")) or "file:" in line]
    assert not [line for line in lines if line.split("=")[0] in ("alpha", "beta", "root")]


def test_symlinked_python_version_is_refused_without_echo(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("secret-token-abc rest\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".python-version").symlink_to(secret)
    with pytest.raises(ConfigError) as info:
        PythonAdapter().load_config(repo)
    assert "secret" not in str(info.value) and "regular file" in str(info.value)


@pytest.mark.security
def test_symlinked_pyproject_and_lock_are_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside.toml"
    outside.write_text("[tool.coverset]\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").symlink_to(outside)
    with pytest.raises(ConfigError, match="regular file"):
        PythonAdapter().load_config(repo)
    (repo / "pyproject.toml").unlink()
    (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (repo / "uv.lock").symlink_to(outside)
    with pytest.raises(EnvSpecError, match="regular file"):
        PythonAdapter(uv=str(_fake_uv(tmp_path))).env_spec(repo, {})


@pytest.mark.security
def test_fifo_python_version_does_not_hang(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / ".python-version")
    with pytest.raises(ConfigError, match="regular file"):
        PythonAdapter().load_config(tmp_path)


@pytest.mark.parametrize(
    ("content", "python"), [("", "3.13"), ("\n  \n", "3.13"), ("3.12.4 # pinned\n", "3.12")]
)
def test_python_version_file_contents(tmp_path: Path, content: str, python: str) -> None:
    (tmp_path / ".python-version").write_text(content)
    assert PythonAdapter().load_config(tmp_path).python == python


@pytest.mark.security
def test_bad_python_version_file_is_not_echoed(tmp_path: Path) -> None:
    (tmp_path / ".python-version").write_text("secret-token-abc\n")
    with pytest.raises(ConfigError) as info:
        PythonAdapter().load_config(tmp_path)
    assert "secret" not in str(info.value)


@pytest.mark.security
def test_oversized_pyproject_is_refused(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("# pad\n" * (python_lang.MAX_PYPROJECT_BYTES // 6 + 1))
    with pytest.raises(ConfigError, match="larger than"):
        PythonAdapter().load_config(tmp_path)


@pytest.mark.security
def test_symlinked_source_root_is_not_inferred(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    (outside / "hostpkg").mkdir(parents=True)
    (outside / "hostpkg" / "__init__.py").write_text("")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").symlink_to(outside)
    assert PythonAdapter().load_config(repo).source == ()


def test_shape_catalogue_applies_annotations_from_sources() -> None:
    path, source = "src/m.py", "def f(x: int | None):\n    return x\n"
    region = index_functions(path, source)[1]
    observations = [obs("x", "int|pos", func=region.triple)]
    with_sources = PythonAdapter().shape_catalogue({path: source})(observations)
    assert {"x:none", "x:int"} <= set(with_sources["x"].missing) | set(with_sources["x"].observed)
    assert "x:none" in with_sources["x"].missing
    assert "x:none" not in PythonAdapter().shape_catalogue()(observations)["x"].expected


def test_shape_catalogue_resolves_enum_members_from_sources() -> None:
    roles = "from enum import IntEnum\n\n\nclass Role(IntEnum):\n    VIEWER = 1\n    ADMIN = 2\n"
    path, source = "src/m.py", "from roles import Role\n\n\ndef f(role: Role):\n    return role\n"
    region = index_functions(path, source)[1]
    sources = {path: source, "src/roles.py": roles}
    catalogue = PythonAdapter().shape_catalogue(sources)
    named = [obs("role", "obj:roles.Role", func=region.triple, example="<Role.ADMIN: 2>")]
    result = catalogue(named)["role"]
    assert result.expected == ("role:Role.VIEWER", "role:Role.ADMIN")
    assert result.missing == ("role:Role.VIEWER",) and "role:Role.ADMIN" in result.observed
    # Without an example naming a member (what the shipped tracer records for Enum values), the
    # members are not expected: annotation alone would report every member as never exercised.
    assert catalogue([obs("role", "obj:roles.Role", func=region.triple)])["role"].expected == ()
