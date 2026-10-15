"""Environment sources: every way a repository describes its test environment becomes an EnvSpec.

The parsing and detection paths run offline against the repositories in ``fixtures/repos/`` and small
ones built in ``tmp_path``; resolutions go through a fake ``uv`` that records its arguments. Tests that
resolve with the real ``uv pip compile`` are marked ``network`` and skip when neither the index nor a
cached resolution of the same inputs is reachable.
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from coverset.lang.python import (
    ConfigError,
    EnvSpecError,
    PythonAdapter,
    addopts_conflicts,
    explain_config,
    pytest_config,
    read_tool_config,
    suggested_addopts,
)
from coverset.lang.python_env import (
    FALLBACK_WRITER,
    LocalPackage,
    auto_requirement_files,
    detect_env_source,
    install_needed,
    local_packages,
    pipfile_lock_requirements,
    poetry_lock_requirements,
    project_metadata,
    read_requirement_files,
    resolve_install,
    snapshot_install_commands,
    uv_lock_local_packages,
)

ROOT = Path(__file__).parents[2]
REPOS = ROOT / "fixtures" / "repos"
DEMO_APP = ROOT / "examples" / "demo-app"
PINNED = "attrs==26.1.0\npytest==9.1.1\n"


# -- helpers ------------------------------------------------------------------------------------------------


def fake_uv(tmp_path: Path, *, output: str = PINNED, fail_offline: bool = False, exit_code: int = 0) -> Path:
    """A ``uv`` that appends its argv (and whether it ran offline) to ``calls.jsonl`` and prints ``output``.

    For ``pip compile`` it also copies the requirements input it was given to ``inputs.txt``.
    """
    script = tmp_path / "fake-uv"
    log = tmp_path / "calls.jsonl"
    script.write_text(
        f"""#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
with open({str(log)!r}, "a") as fh:
    fh.write(json.dumps({{"args": args, "offline": os.environ.get("UV_OFFLINE") == "1",
                         "home": os.environ.get("HOME"), "cache": os.environ.get("UV_CACHE_DIR")}}) + "\\n")
if args[:2] == ["pip", "compile"]:
    source = next(a for a in args if a.endswith("requirements.in"))
    with open({str(tmp_path / "inputs.txt")!r}, "w") as out:
        out.write(open(source).read())
if {fail_offline!r} and os.environ.get("UV_OFFLINE") == "1":
    sys.stderr.write("network disabled\\n")
    sys.exit(2)
sys.stdout.write({output!r})
sys.exit({exit_code})
"""
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def calls(tmp_path: Path) -> list[dict]:  # type: ignore[type-arg]
    log = tmp_path / "calls.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.is_file() else []


def adapter(tmp_path: Path, **kwargs: object) -> PythonAdapter:
    return PythonAdapter(uv=str(fake_uv(tmp_path, **kwargs)), cache_dir=tmp_path / "cache")  # type: ignore[arg-type]


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def _online() -> bool:
    try:
        with socket.create_connection(("pypi.org", 443), timeout=3):
            return True
    except OSError:
        return False


network = pytest.mark.network
needs_index = pytest.mark.skipif(
    shutil.which("uv") is None or not _online(), reason="needs uv and the package index (network)"
)


# -- detection ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("repo", "kind"),
    [
        (DEMO_APP, "uv.lock"),
        (REPOS / "requirements-only", "requirements"),
        (REPOS / "pyproject-only", "pyproject"),
        (REPOS / "poetry", "poetry"),
        (REPOS / "setup-cfg-only", "setup"),
        (REPOS / "setup-py-only", "setup"),
        (REPOS / "src-entry-points", "pyproject"),
    ],
)
def test_each_fixture_has_its_source(repo: Path, kind: str) -> None:
    assert detect_env_source(repo).kind == kind
    assert PythonAdapter().detect(repo)


def test_configured_requirements_win_over_a_lock(tmp_path: Path) -> None:
    write(tmp_path, {"uv.lock": "version = 1\n", "ci/reqs.txt": "attrs\n"})
    source = detect_env_source(tmp_path, requirements=("ci/reqs.txt",))
    assert (source.kind, source.inputs, source.reason) == (
        "requirements",
        ("ci/reqs.txt",),
        "[tool.coverset] requirements",
    )


def test_a_repository_without_any_source_names_the_fixes(tmp_path: Path) -> None:
    write(tmp_path, {"pyproject.toml": "[tool.ruff]\nline-length = 100\n"})
    with pytest.raises(EnvSpecError, match=r"no uv\.lock.*\[tool\.coverset\] requirements"):
        detect_env_source(tmp_path)


def test_auto_detection_keeps_runtime_and_test_files_only(tmp_path: Path) -> None:
    names = [
        "requirements.txt",
        "requirements-dev.txt",
        "requirements_test.txt",
        "test-requirements.txt",
        "requirements-docs.txt",
        "requirements/tests.txt",
        "requirements/tests-min.txt",
        "requirements/typing.txt",
        "requirements/base.txt",
        "requirements/dev.in",
    ]
    write(tmp_path, dict.fromkeys(names, "attrs\n"))
    assert auto_requirement_files(tmp_path) == [
        "requirements-dev.txt",
        "requirements.txt",
        "requirements_test.txt",
        "test-requirements.txt",
        "requirements/base.txt",
        "requirements/tests.txt",
    ]


def test_python_detects_requirements_only_repositories(tmp_path: Path) -> None:
    write(tmp_path, {"pkg/__init__.py": "", "requirements-dev.txt": "pytest\n"})
    assert PythonAdapter().detect(tmp_path)


# -- requirement files --------------------------------------------------------------------------------------


def test_requirement_files_follow_includes_and_drop_the_project(tmp_path: Path) -> None:
    repo = REPOS / "requirements-only"
    read = read_requirement_files(repo, ["requirements-dev.txt"])
    assert read.requirements == ["attrs>=23.1", "pytest>=8.0"]
    assert read.files == ["requirements-dev.txt", "requirements.txt"]
    assert any("--index-url" in note and "ignored" in note for note in read.notes)
    assert any("project itself" in note for note in read.notes)
    assert read.local == []


@pytest.mark.security
def test_requirement_files_cannot_inject_options_or_leave_the_repo(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "requirements.txt": (
                "-r ../../etc/passwd\n"
                "--extra-index-url http://evil.invalid/simple\n"
                "-c constraints.txt\n"
                "attrs==26.1.0 --hash=sha256:abc \\\n    --hash=sha256:def\n"
                "--trusted-host evil.invalid\n"
                "requests[socks]>=2 ; python_version >= '3.8'\n"
                "not a requirement at all\n"
                "-e git+https://example.invalid/pkg.git#egg=pkg\n"
            ),
            "constraints.txt": "urllib3<3\n",
        },
    )
    read = read_requirement_files(tmp_path, ["requirements.txt"])
    assert read.requirements == [
        "attrs==26.1.0",
        'requests[socks]>=2; python_version >= "3.8"',
        "git+https://example.invalid/pkg.git#egg=pkg",
    ]
    assert read.constraints == ["urllib3<3"]
    assert not any(line.startswith("-") for line in read.requirements + read.constraints)
    assert any("leaves the repository" in note for note in read.notes)
    assert any("not a requirement" in note for note in read.notes)


@pytest.mark.security
def test_a_symlinked_requirements_file_is_refused(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("token-abc\n")
    repo = write(tmp_path / "repo", {"x.txt": ""})
    (repo / "requirements.txt").symlink_to(secret)
    with pytest.raises(ConfigError, match="regular file"):
        read_requirement_files(repo, ["requirements.txt"])
    (repo / "reqs").symlink_to(tmp_path)
    with pytest.raises(ConfigError, match="symlink"):
        read_requirement_files(repo, ["reqs/secret.txt"])


# A pull request chooses these; uv would fetch them on the control plane before any approval.
URL_REQUIREMENTS = (
    "evilpkg @ http://127.0.0.1:18977/evilpkg-1.0-py3-none-any.whl\n"
    "gitpkg @ git+https://example.invalid/gitpkg.git@v1\n"
    "git+https://example.invalid/bare.git#egg=bare\n"
)
REFUSED_REQUIREMENTS = (
    "secretthing @ git+file:///Users/someone/Code/private\n"
    "secretproj @ file:///etc/secretproj\n"
    "abspath @ /etc/secretproj\n"
    "-e git+file:///Users/someone/Code/private#egg=other\n"
    "ftppkg @ ftp://example.invalid/ftppkg.whl\n"
)


@pytest.mark.security
def test_direct_references_never_reach_the_resolver(tmp_path: Path) -> None:
    """URL, VCS and file references from a requirements file are never handed to ``uv pip compile``:
    network ones are appended verbatim after the compiled pins (a trusted build installs them, an
    untrusted one refuses them), and file:/path ones are refused outright."""
    repo = write(
        tmp_path / "repo",
        {
            "requirements.txt": "attrs>=23\n" + URL_REQUIREMENTS + REFUSED_REQUIREMENTS,
            "constraints.txt": "urllib3 @ https://example.invalid/urllib3.whl\nidna<4\n",
            "requirements-dev.txt": "-c constraints.txt\npytest\n",
            "pkg/__init__.py": "",
        },
    )
    report = adapter(tmp_path).describe_env(repo, {"python": "3.13"})
    compiled_input = (tmp_path / "inputs.txt").read_text()
    assert sorted(compiled_input.splitlines()) == ["attrs>=23", "pytest"]
    assert "://" not in compiled_input and "@" not in compiled_input
    (call,) = calls(tmp_path)
    assert any(a.endswith("constraints.txt") for a in call["args"])  # idna<4; the URL one is dropped
    text = report.spec.requirements_txt
    assert text.index(PINNED) < text.index("# coverset: direct references (not resolved by coverset)")
    for line in (
        "evilpkg @ http://127.0.0.1:18977/evilpkg-1.0-py3-none-any.whl",
        "gitpkg @ git+https://example.invalid/gitpkg.git@v1",
        "git+https://example.invalid/bare.git#egg=bare",
    ):
        assert line in text.splitlines()
    for secret in ("file:", "/etc/secretproj", "/Users/someone", "ftp://"):
        assert secret not in text
    notes = "\n".join(report.resolved.notes)
    assert notes.count("refused") >= 5 and "constraint" in notes
    from coverset.executors.env_export import direct_references

    assert len(direct_references(text)) == 3  # what an untrusted build refuses


@pytest.mark.security
def test_direct_references_in_project_metadata_and_build_requires(tmp_path: Path) -> None:
    repo = write(
        tmp_path / "repo",
        {
            "pyproject.toml": (
                '[build-system]\nrequires = ["setuptools @ file:///opt/setuptools", "wheel"]\n'
                'build-backend = "setuptools.build_meta"\n'
                '[project]\nname = "pkg"\nversion = "1"\ndependencies = ['
                '"attrs", "near @ https://example.invalid/near.whl", "far @ file:///srv/far"]\n'
                '[project.scripts]\npkg = "pkg:main"\n'
                '[project.entry-points.pytest11]\npkg = "pkg.plugin"\n'
            ),
            "pkg/__init__.py": "",
            "pkg/plugin.py": "",
        },
    )
    report = adapter(tmp_path).describe_env(repo, {"python": "3.13"})
    compiled_input = (tmp_path / "inputs.txt").read_text()
    assert "://" not in compiled_input and "attrs" in compiled_input
    text = report.spec.requirements_txt
    assert "near @ https://example.invalid/near.whl" in text.splitlines()
    assert "file:" not in text and "/srv/far" not in text and "/opt/setuptools" not in text


def test_a_direct_reference_wins_over_a_compiled_pin_of_the_same_package(tmp_path: Path) -> None:
    repo = write(
        tmp_path / "repo",
        {"requirements.txt": "pytest\nattrs @ https://example.invalid/attrs.whl\n", "pkg/__init__.py": ""},
    )
    text = adapter(tmp_path).env_spec(repo, {"python": "3.13"}).requirements_txt
    lines = text.splitlines()
    assert "attrs==26.1.0" not in lines and "pytest==9.1.1" in lines
    assert "attrs @ https://example.invalid/attrs.whl" in lines


@pytest.mark.security
def test_compile_requirements_refuses_direct_references(tmp_path: Path) -> None:
    from coverset.lang.python_env import compile_requirements

    with pytest.raises(EnvSpecError, match="never handed"):
        compile_requirements(
            ["x @ https://example.invalid/x.whl"],
            python="3.13",
            uv=str(fake_uv(tmp_path)),
            cache_dir=tmp_path,
        )
    assert calls(tmp_path) == []


@pytest.mark.security
def test_locks_drop_file_urls(tmp_path: Path) -> None:
    poetry = (
        '[[package]]\nname = "near"\nversion = "1"\n[package.source]\ntype = "url"\n'
        'url = "https://example.invalid/near.whl"\n'
        '[[package]]\nname = "far"\nversion = "1"\n[package.source]\ntype = "git"\n'
        'url = "file:///srv/far"\nreference = "main"\n'
    )
    lines, notes, _ = poetry_lock_requirements(poetry)
    assert lines == ["near @ https://example.invalid/near.whl"]
    assert any("far" in note and "refused" in note for note in notes)
    pipfile = json.dumps({"default": {"far": {"git": "file:///srv/far"}, "attrs": {"version": "==26.1.0"}}})
    lines, notes, _ = pipfile_lock_requirements(pipfile)
    assert lines == ["attrs==26.1.0"] and any("refused" in note for note in notes)


def test_local_wheels_and_directories_in_requirements_become_local_packages(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "requirements.txt": "./wheels/tool-1.0-py3-none-any.whl ; sys_platform == 'linux'\n-e libs/helper\n",
            "wheels/tool-1.0-py3-none-any.whl": "zip",
            "libs/helper/pyproject.toml": '[project]\nname = "helper"\nversion = "1"\n',
        },
    )
    read = read_requirement_files(tmp_path, ["requirements.txt"])
    assert read.local == [
        LocalPackage("file", "wheels/tool-1.0-py3-none-any.whl", "tool", 'sys_platform == "linux"'),
        LocalPackage("editable", "libs/helper", "helper"),
    ]


# -- project metadata ---------------------------------------------------------------------------------------


def test_pyproject_metadata_takes_every_extra_and_the_test_groups() -> None:
    meta = project_metadata(REPOS / "pyproject-only")
    assert meta.origin == "pyproject" and meta.name == "pyproj-app"
    assert meta.dependencies == ["attrs>=23.1"]
    assert set(meta.extras) == {"fast", "all"} and set(meta.groups) == {"test"}
    assert meta.all_requirements() == ["attrs>=23.1", "iniconfig>=2", "pytest>=8.0"]


def test_dynamic_dependencies_are_read_from_setuptools_files(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "pyproject.toml": (
                '[project]\nname = "dyn"\nversion = "1"\ndynamic = ["dependencies", "optional-dependencies"]\n'
                '[tool.setuptools.dynamic]\ndependencies = {file = ["requirements.in"]}\n'
                'optional-dependencies.test = {file = ["test.in"]}\n'
            ),
            "requirements.in": "attrs\n",
            "test.in": "pytest\n",
        },
    )
    meta = project_metadata(tmp_path)
    assert meta.dependencies == ["attrs"] and meta.extras == {"test": ["pytest"]}
    assert "requirements.in" in meta.files and "test.in" in meta.files


def test_setup_cfg_metadata() -> None:
    meta = project_metadata(REPOS / "setup-cfg-only")
    assert (meta.origin, meta.name, meta.dependencies) == ("setup.cfg", "cfgapp", ["attrs>=23.1"])
    assert meta.extras == {"test": ["pytest>=8.0"]} and meta.requires_python == ">=3.12"


@pytest.mark.security
def test_setup_py_is_read_without_running_it(tmp_path: Path) -> None:
    meta = project_metadata(REPOS / "setup-py-only")
    assert (meta.origin, meta.name) == ("setup.py", "pyapp")
    assert meta.dependencies == ["attrs>=23.1"] and meta.extras == {"test": ["pytest>=8.0"]}
    marker = tmp_path / "ran"
    write(
        tmp_path / "evil",
        {
            "setup.py": (
                "import pathlib, setuptools\n"
                f"pathlib.Path({str(marker)!r}).write_text('ran')\n"
                "deps = open('reqs').read().split()\n"
                "setuptools.setup(name='evil', install_requires=deps)\n"
            )
        },
    )
    evil = project_metadata(tmp_path / "evil")
    assert not marker.exists()
    assert evil.dynamic_unresolved and "install_requires" in " ".join(evil.notes)
    with pytest.raises(EnvSpecError, match=r"\[tool\.coverset\] requirements"):
        PythonAdapter(uv=str(fake_uv(tmp_path)), cache_dir=tmp_path / "c").env_spec(tmp_path / "evil", {})
    assert not marker.exists()


# -- locks other than uv.lock -------------------------------------------------------------------------------


def test_poetry_lock_becomes_pinned_requirements_with_markers() -> None:
    repo = REPOS / "poetry"
    lines, notes, local = poetry_lock_requirements((repo / "poetry.lock").read_text(), own_name="poetry-app")
    assert lines == [
        "attrs==26.1.0",
        'colorama==0.4.6; sys_platform == "win32"',
        "iniconfig==2.3.1",
        "packaging==26.3",
        "pluggy==1.6.0",
        "pygments==2.21.0",
        "pytest==9.1.1",
    ]
    assert (notes, local) == ([], [])


def test_old_poetry_locks_get_markers_from_their_edges() -> None:
    lock = """
[[package]]
name = "pywin32"
version = "306"
category = "main"
optional = false
python-versions = "*"

[[package]]
name = "docker"
version = "7.0.0"
category = "main"
optional = false
python-versions = ">=3.8"

[package.dependencies]
pywin32 = {version = ">=304", markers = "sys_platform == \\"win32\\""}

[[package]]
name = "internal"
version = "1.0"
category = "main"
optional = false
python-versions = "*"

[package.source]
type = "legacy"
url = "https://pypi.example.invalid/simple"
reference = "private"

[[package]]
name = "vcs-pkg"
version = "0.1"
category = "dev"
optional = false
python-versions = "*"

[package.source]
type = "git"
url = "https://example.invalid/vcs-pkg.git"
reference = "main"
resolved_reference = "abc123"

[[package]]
name = "sibling"
version = "0.1"
category = "main"
optional = false
python-versions = "*"
develop = true

[package.source]
type = "directory"
url = "libs/sibling"

[metadata]
lock-version = "1.1"
python-versions = "^3.12"
content-hash = "x"
"""
    lines, notes, local = poetry_lock_requirements(lock)
    assert lines == [
        'pywin32==306; sys_platform == "win32"',
        "docker==7.0.0",
        "internal==1.0",
        "vcs-pkg @ git+https://example.invalid/vcs-pkg.git@abc123",
    ]
    assert local == [LocalPackage("editable", "libs/sibling", "sibling")]
    assert any("private" in note for note in notes)


def test_pipfile_lock_conversion() -> None:
    lock = {
        "_meta": {},
        "default": {
            "attrs": {"version": "==26.1.0", "hashes": ["sha256:x"]},
            "requests": {"version": "==2.32.0", "extras": ["socks"], "markers": "python_version >= '3.8'"},
            "lib": {"git": "https://example.invalid/lib.git", "ref": "abc"},
            "app": {"path": ".", "editable": True},
            "vendored": {"file": "wheels/vendored-1.0-py3-none-any.whl"},
        },
        "develop": {"pytest": {"version": "==9.1.1"}},
    }
    lines, notes, local = pipfile_lock_requirements(json.dumps(lock))
    assert lines == [
        "attrs==26.1.0",
        'requests[socks]==2.32.0; python_version >= "3.8"',
        "lib @ git+https://example.invalid/lib.git@abc",
        "pytest==9.1.1",
    ]
    assert local == [LocalPackage("file", "wheels/vendored-1.0-py3-none-any.whl", "vendored")]
    assert any("project itself" in note for note in notes)


def test_uv_lock_local_packages_keep_their_markers() -> None:
    lock = """
version = 1

[[package]]
name = "root"
version = "0.1.0"
source = { virtual = "." }
dependencies = [
    { name = "member" },
    { name = "native", version = "1.0", source = { path = "wheels/native-1.0-cp313-cp313-linux.whl" }, marker = "sys_platform == 'linux'" },
    { name = "native", version = "1.0", source = { path = "wheels/native-1.0-cp313-cp313-macosx.whl" }, marker = "sys_platform == 'darwin'" },
]

[[package]]
name = "member"
version = "0.1.0"
source = { editable = "packages/member" }

[[package]]
name = "native"
version = "1.0"
source = { path = "wheels/native-1.0-cp313-cp313-linux.whl" }

[[package]]
name = "native"
version = "1.0"
source = { path = "wheels/native-1.0-cp313-cp313-macosx.whl" }

[[package]]
name = "orphan"
version = "1.0"
source = { directory = "orphan" }
"""
    local, notes = uv_lock_local_packages(lock)
    assert local == [
        LocalPackage("editable", "packages/member", "member"),
        LocalPackage("file", "wheels/native-1.0-cp313-cp313-linux.whl", "native", 'sys_platform == "linux"'),
        LocalPackage(
            "file", "wheels/native-1.0-cp313-cp313-macosx.whl", "native", 'sys_platform == "darwin"'
        ),
    ]
    assert any("orphan" in note for note in notes)


def test_local_package_lines_round_trip_and_are_validated() -> None:
    package = LocalPackage("file", "w/x-1.whl", "x", 'sys_platform == "linux"')
    text = f"attrs==1\n{package.line()}\n# coverset-local: file ../escape.whl y\n# coverset-local: dir d z ; not a marker\n"
    assert local_packages(text) == [package]
    assert package.requirement("/work/repo") == 'x @ file:///work/repo/w/x-1.whl ; sys_platform == "linux"'

    class Spec:
        install = "none"
        requirements_txt = text

    assert install_needed(Spec())
    Spec.requirements_txt = "attrs==1\n"
    assert not install_needed(Spec())
    Spec.install = "wheel"
    assert install_needed(Spec())


@pytest.mark.security
def test_lfs_pointers_are_not_installed(tmp_path: Path) -> None:
    pointer = "version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 12\n"
    write(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "app"\nversion = "1"\ndependencies = []\n',
            "requirements.txt": "./wheels/blob-1.0-py3-none-any.whl\n",
            "wheels/blob-1.0-py3-none-any.whl": pointer,
        },
    )
    spec = adapter(tmp_path).describe_env(tmp_path, {})
    assert "coverset-local" not in spec.spec.requirements_txt
    assert any("Git LFS pointer" in note for note in spec.resolved.notes)


# -- resolving through uv -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("repo", "kind", "expected"),
    [
        ("requirements-only", "requirements", ["attrs>=23.1", "pytest>=8.0"]),
        ("pyproject-only", "pyproject", ["attrs>=23.1", "iniconfig>=2", "pytest>=8.0"]),
        ("setup-cfg-only", "setup", ["attrs>=23.1", "pytest>=8.0"]),
        ("setup-py-only", "setup", ["attrs>=23.1", "pytest>=8.0"]),
        ("src-entry-points", "pyproject", ["pytest>=8.0", "setuptools>=61"]),
    ],
)
def test_compiled_sources_resolve_through_uv_pip_compile(
    tmp_path: Path, repo: str, kind: str, expected: list[str]
) -> None:
    report = adapter(tmp_path).describe_env(REPOS / repo, {"python": "3.13"})
    (call,) = calls(tmp_path)
    assert call["args"][:7] == [
        "pip",
        "compile",
        "--universal",
        "--no-build",
        "--no-header",
        "--no-annotate",
        "--quiet",
    ]
    assert call["args"][7:9] == ["--python-version", "3.13"] and call["offline"]
    assert call["cache"] == str(tmp_path / "cache" / "uv-cache") and call["home"] != os.environ.get("HOME")
    assert (tmp_path / "inputs.txt").read_text().splitlines() == expected
    spec = report.spec
    assert spec.env_source == kind and report.source.kind == kind
    assert spec.requirements_txt.startswith(f"# coverset environment: source={kind} python=3.13\n")
    assert spec.requirements_txt.endswith(PINNED)
    expected_hash = hashlib.sha256("\0".join((spec.requirements_txt, "3.13", "none")).encode()).hexdigest()
    assert spec.env_hash == expected_hash
    assert spec.install == ("wheel" if repo == "src-entry-points" else "none")


def test_resolutions_are_cached_by_their_inputs(tmp_path: Path) -> None:
    first = adapter(tmp_path).env_spec(REPOS / "requirements-only", {})
    again = adapter(tmp_path).env_spec(REPOS / "requirements-only", {})
    assert first.env_hash == again.env_hash and len(calls(tmp_path)) == 1
    # setup-cfg-only declares the same requirements: one resolution serves both
    other = adapter(tmp_path).env_spec(REPOS / "setup-cfg-only", {})
    assert len(calls(tmp_path)) == 1 and other.env_hash != first.env_hash  # the header names the source


def test_offline_first_then_online(tmp_path: Path) -> None:
    report = adapter(tmp_path, fail_offline=True).describe_env(REPOS / "pyproject-only", {})
    assert [c["offline"] for c in calls(tmp_path)] == [True, False]
    assert report.resolved.resolved_by == "uv pip compile (online)"
    offline = PythonAdapter(
        uv=str(fake_uv(tmp_path, fail_offline=True)), cache_dir=tmp_path / "c2", offline_only=True
    )
    with pytest.raises(EnvSpecError, match="offline only"):
        offline.env_spec(REPOS / "pyproject-only", {})


def test_a_comment_does_not_change_the_environment_but_a_requirement_does(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    shutil.copytree(REPOS / "requirements-only", repo)
    base = adapter(tmp_path).env_spec(repo, {})
    (repo / "requirements.txt").write_text("# a new comment\nattrs>=23.1\n")
    commented = adapter(tmp_path).env_spec(repo, {})
    assert commented.env_hash == base.env_hash and commented.lock_hash != base.lock_hash
    (repo / "requirements.txt").write_text("attrs>=24\n")
    assert adapter(tmp_path).env_spec(repo, {}).env_hash != base.env_hash


def test_poetry_resolution_needs_no_uv(tmp_path: Path) -> None:
    report = adapter(tmp_path).describe_env(REPOS / "poetry", {})
    assert calls(tmp_path) == [] and report.spec.env_source == "poetry"
    assert "pytest==9.1.1" in report.spec.requirements_txt
    assert report.resolved.resolved_by == "poetry.lock converted"


def test_uv_lock_exports_extras_only_when_declared(tmp_path: Path) -> None:
    write(
        tmp_path / "plain",
        {"pyproject.toml": '[project]\nname = "p"\nversion = "1"\n', "uv.lock": "version = 1\n"},
    )
    write(
        tmp_path / "extras",
        {
            "pyproject.toml": '[project]\nname = "p"\nversion = "1"\n[project.optional-dependencies]\nx = ["attrs"]\n',
            "uv.lock": "version = 1\n",
        },
    )
    uv = adapter(tmp_path)
    plain = uv.env_spec(tmp_path / "plain", {})
    uv.env_spec(tmp_path / "extras", {})
    first, second = calls(tmp_path)
    assert "--all-extras" not in first["args"] and second["args"][-1] == "--all-extras"
    assert plain.env_source == "uv.lock" and plain.requirements_txt == PINNED  # no header: hashes unchanged


def test_compile_failures_are_explained(tmp_path: Path) -> None:
    broken = PythonAdapter(uv=str(fake_uv(tmp_path, exit_code=1)), cache_dir=tmp_path / "c")
    with pytest.raises(EnvSpecError, match="uv pip compile failed"):
        broken.env_spec(REPOS / "pyproject-only", {})


# -- installing the project ---------------------------------------------------------------------------------


def test_auto_install_detects_what_needs_an_installed_project() -> None:
    plan = resolve_install(REPOS / "src-entry-points", "auto", packages=("entry_app",), source_root="src")
    assert plan.mode == "wheel" and plan.build_requires == ("setuptools>=61",)
    assert any("entry points" in r for r in plan.reasons)
    assert any("installed metadata" in r for r in plan.reasons)
    plain = resolve_install(REPOS / "pyproject-only", "auto", packages=("pyproj_app",), source_root="src")
    assert plain.mode == "none" and plain.build_backend == "hatchling.build"
    assert resolve_install(REPOS / "requirements-only", "auto").mode == "none"


def test_compiled_extensions_and_generated_versions_need_a_wheel(tmp_path: Path) -> None:
    write(
        tmp_path / "ext",
        {
            "setup.py": "from setuptools import Extension, setup\nsetup(name='e', ext_modules=[Extension('e._c', ['c.c'])])\n"
        },
    )
    plan = resolve_install(tmp_path / "ext", "auto")
    assert plan.mode == "wheel" and plan.compiled and plan.warnings
    assert plan.build_requires == ("setuptools>=40.8.0", "wheel")
    write(
        tmp_path / "scm",
        {
            "pyproject.toml": (
                '[project]\nname = "s"\ndynamic = ["version"]\n[build-system]\nrequires = ["setuptools", "setuptools-scm"]\n'
                'build-backend = "setuptools.build_meta"\n[tool.setuptools_scm]\nversion_file = "s/_version.py"\n'
            ),
            "s/__init__.py": "",
        },
    )
    assert resolve_install(tmp_path / "scm", "auto").mode == "wheel"


def test_explicit_install_modes(tmp_path: Path) -> None:
    with pytest.raises(EnvSpecError, match="needs a build backend"):
        resolve_install(REPOS / "requirements-only", "wheel")
    assert resolve_install(REPOS / "src-entry-points", "none").mode == "none"
    assert resolve_install(REPOS / "pyproject-only", "editable").mode == "editable"


def test_snapshot_install_commands() -> None:
    local = [
        LocalPackage("file", "w/x-1.whl", "x", 'sys_platform == "linux"'),
        LocalPackage("editable", "libs/y", "y"),
    ]
    commands = snapshot_install_commands(
        "wheel", repo_dir="/work/repo", source_dir="/work/repo/src", local=local
    )
    pip = ["python", "-m", "pip", "install", "--no-deps", "--no-build-isolation", "--no-index", "--quiet"]
    assert commands[0] == [
        *pip,
        'x @ file:///work/repo/w/x-1.whl ; sys_platform == "linux"',
        "-e",
        "/work/repo/libs/y",
    ]
    assert commands[1] == [*pip, "/work/repo"]
    assert commands[2][:2] == ["python", "-c"] and commands[2][3:] == ["/work/repo/src", "-"]
    assert snapshot_install_commands("none", repo_dir="/r", source_dir="/r") == []
    editable = snapshot_install_commands("editable", repo_dir="/r", source_dir="/r")
    assert editable[0][-2:] == ["-e", "/r"]


def test_fallback_hook_serves_only_extension_and_generated_modules(tmp_path: Path) -> None:
    source = write(
        tmp_path / "src", {"pkg/__init__.py": "", "pkg/core.py": "X = 'source'\n", "pkg/gone.py": ""}
    )
    site = write(
        tmp_path / "site",
        {
            "pkg/__init__.py": "",
            "pkg/core.py": "X = 'installed'\n",
            "pkg/_version.py": "version = '9.9'\n",
            "pkg/gone.py": "",
        },
    )
    suffix = importlib.machinery.EXTENSION_SUFFIXES[0]
    (site / "pkg" / f"_speedups{suffix}").write_bytes(b"not really a shared object")
    subprocess.run(
        [sys.executable, "-c", FALLBACK_WRITER, str(source), str(site)], check=True, capture_output=True
    )
    assert (site / "_coverset_fallback.pth").read_text() == "import _coverset_fallback\n"
    (source / "pkg" / "gone.py").unlink()  # deleted by a pull request after the install
    probe = (
        "import importlib.util, site, sys\n"
        f"sys.path.insert(0, {str(source)!r}); site.addsitedir({str(site)!r})\n"
        "import pkg.core, pkg._version\n"
        "spec = importlib.util.find_spec('pkg._speedups')\n"
        "print(pkg.core.X, pkg._version.version, spec is not None and spec.origin.startswith("
        + repr(str(site))
        + "))\n"
        "try:\n    import pkg.gone\n    print('gone imported')\nexcept ImportError:\n    print('gone missing')\n"
    )
    out = subprocess.run([sys.executable, "-I", "-c", probe], check=True, capture_output=True, text=True)
    assert out.stdout.split() == ["source", "9.9", "True", "gone", "missing"]


# -- configuration inference --------------------------------------------------------------------------------


def test_inference_across_the_fixtures() -> None:
    def explained(name: str) -> tuple:  # type: ignore[type-arg]
        root = REPOS / name
        text = (root / "pyproject.toml").read_text() if (root / "pyproject.toml").is_file() else ""
        return explain_config(text, repo_root=root)

    config, how = explained("src-entry-points")
    assert (config.source_root, config.source, config.python) == ("src", ("entry_app",), "3.13")
    assert how["source_root"].startswith("package directory") and how["python"] == "requires-python >=3.12"
    config, how = explained("setup-cfg-only")
    assert config.testpaths == ("tests",) and how["testpaths"] == "testpaths in setup.cfg"
    config, how = explained("requirements-only")
    assert config.tracked_globs == ("uv.lock", "conftest.py", "requirements-dev.txt", "requirements.txt")
    config, how = explained("poetry")
    assert config.source == ("poetry_app",) and "poetry.lock" in config.tracked_globs


def test_source_inference_for_unusual_layouts(tmp_path: Path) -> None:
    ns = write(
        tmp_path / "ns",
        {"src/acme/billing/core.py": "", "pyproject.toml": "[project]\nname='acme-billing'\n"},
    )
    assert read_tool_config((ns / "pyproject.toml").read_text(), repo_root=ns).source == ("acme",)
    single = write(tmp_path / "single", {"tinylib.py": "", "pyproject.toml": "[project]\nname = 'tinylib'\n"})
    assert read_tool_config((single / "pyproject.toml").read_text(), repo_root=single).source == ("tinylib",)
    nested = write(
        tmp_path / "nested",
        {
            "web/app/__init__.py": "",
            "web/app/tests/test_x.py": "",
            "web/worker/__init__.py": "",
            "pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["web/app/tests", "web/missing/tests"]\n',
        },
    )
    config, how = explain_config((nested / "pyproject.toml").read_text(), repo_root=nested)
    assert (config.source_root, config.source) == ("web", ("app", "worker"))
    assert config.testpaths == ("web/app/tests",) and "web/missing/tests" in how["testpaths"]
    hinted = write(
        tmp_path / "hinted",
        {"server/svc/__init__.py": "", "pyproject.toml": '[tool.ty.environment]\nroot = ["./server"]\n'},
    )
    assert read_tool_config((hinted / "pyproject.toml").read_text(), repo_root=hinted).source_root == "server"
    empty = write(tmp_path / "empty", {"scripts/run.py": ""})
    config, how = explain_config("", repo_root=empty)
    assert config.source == () and config.testpaths == () and "rootdir" in how["testpaths"]


@pytest.mark.parametrize(
    ("requires", "python"), [(">=3.9", "3.13"), ("==3.12.*", "3.12"), (">=3.14", "3.14"), ("<3.11", "3.13")]
)
def test_python_from_requires_python(tmp_path: Path, requires: str, python: str) -> None:
    write(tmp_path, {"pyproject.toml": f'[project]\nname = "x"\nrequires-python = "{requires}"\n'})
    config, how = explain_config((tmp_path / "pyproject.toml").read_text(), repo_root=tmp_path)
    assert config.python == python
    assert ("allows no 3.12+" in how["python"]) == (requires == "<3.11")


def test_new_keys_are_validated() -> None:
    config = read_tool_config(
        '[tool.coverset]\nrequirements = ["ci/req.txt"]\ninstall = "wheel"\npytest_args = ["-p", "no:randomly", "-m", "not slow", "--strict-markers", "-Werror"]\n'
    )
    assert config.requirements == ("ci/req.txt",) and config.install == "wheel"
    assert config.pytest_args == ("-p", "no:randomly", "-m", "not slow", "--strict-markers", "-Werror")
    with pytest.raises(ConfigError, match="install must be"):
        read_tool_config('[tool.coverset]\ninstall = "magic"\n')
    with pytest.raises(ConfigError):
        read_tool_config('[tool.coverset]\nrequirements = ["../outside.txt"]\n')


@pytest.mark.security
@pytest.mark.parametrize(
    "args",
    [
        ["--basetemp=/"],
        ["--rootdir=/"],
        ["-c", "evil.ini"],
        ["tests/test_x.py"],
        ["@args.txt"],
        ["-x"],
        ["-vx"],
        ["--maxfail=1"],
        ["--lf"],
        ["-k", "slow"],
        ["-n", "4"],
        ["--cov=pkg"],
        ["--coverset-out=/tmp/x"],
        ["-p", "evil_plugin"],
        ["-pevil"],
        ["--junitxml=/etc/x.xml"],
        ["-m"],
        ["-m", "--lf"],
        ["-vv"],
        ["--verbosity=2"],
        ["--log-file=/abs/path.log"],
        ["--report-log=/abs/report.jsonl"],
        ["--deselect=tests/test_a.py::test_x"],
        ["--ignore=tests/slow"],
        ["--ignore-glob=*slow*"],
        ["--pyargs"],
        ["-o", "log_file=/abs/path.log"],
        ["-olog_file=/abs/path.log"],
        ["--override-ini=cache_dir=/abs"],
        ["-o", "python_files=x.py"],
        ["-o", "verbosity_test_cases=2"],
        ["-o", "nokeyvalue"],
        ["--override-ini"],
        ["--override-ini=addopts=--lf -x"],
        ["-o", "addopts=-ra --log-file=/abs"],
        ["-o", "addopts=-o addopts=-ra"],
        ["-o", "addopts='unbalanced"],
    ],
)
def test_pytest_args_refuse_what_coverset_owns(args: list[str]) -> None:
    with pytest.raises(ConfigError, match="pytest_args"):
        read_tool_config(f"[tool.coverset]\npytest_args = {json.dumps(args)}\n")


@pytest.mark.parametrize(
    "args",
    [
        ["-o", "addopts=-ra --strict-markers"],
        ["--override-ini=addopts=-ra"],
        ["-o", "xfail_strict=true"],
        ["-m", "not slow"],
        ["-W", "error::DeprecationWarning"],
        ["--log-level=INFO", "--tb=short", "-ra"],
    ],
)
def test_pytest_args_allow_harmless_options(args: list[str]) -> None:
    config = read_tool_config(f"[tool.coverset]\npytest_args = {json.dumps(args)}\n")
    assert config.pytest_args == tuple(args)


def test_test_command_appends_pytest_args() -> None:
    cmd = PythonAdapter().test_command(
        "/w/a", None, "/w/o", {"pytest_args": ("-p", "no:randomly"), "xdist": True}
    )
    assert cmd[-4:] == ["-q", "--numprocesses=0", "-p", "no:randomly"]


# -- pytest's own configuration -----------------------------------------------------------------------------


def test_pytest_config_is_found_as_pytest_finds_it(tmp_path: Path) -> None:
    write(tmp_path, {"setup.cfg": "[tool:pytest]\naddopts = -ra --lf\n", "tox.ini": "[tox]\n"})
    ini = pytest_config(tmp_path)
    assert (ini.file, ini.addopts) == ("setup.cfg", ("-ra", "--lf"))
    write(tmp_path, {"tox.ini": "[pytest]\ntestpaths = t1 t2\n"})
    assert pytest_config(tmp_path).testpaths == ("t1", "t2")
    write(tmp_path, {"pyproject.toml": '[tool.pytest.ini_options]\naddopts = ["-x"]\n'})
    assert pytest_config(tmp_path).file == "pyproject.toml [tool.pytest.ini_options]"
    write(tmp_path, {"pytest.ini": "[pytest]\n"})
    assert pytest_config(tmp_path).file == "pytest.ini"


def test_addopts_conflicts_and_the_suggested_override() -> None:
    addopts = ("-ra", "--lf", "-n", "auto", "--cov=pkg", "-x", "--strict-markers")
    conflicts = addopts_conflicts(addopts, "pytest==9.1.1\n")
    assert [word for word, _ in conflicts] == ["--lf", "-n", "--cov=pkg", "-x"]
    assert suggested_addopts(addopts, conflicts) == "-ra --strict-markers"
    with_plugins = addopts_conflicts(addopts, "pytest-xdist==3\npytest-cov==6\n")
    assert [word for word, _ in with_plugins] == ["--lf", "-x"]


# -- the real resolver (network) ----------------------------------------------------------------------------


@network
@needs_index
@pytest.mark.parametrize(
    "repo", ["requirements-only", "pyproject-only", "setup-cfg-only", "setup-py-only", "src-entry-points"]
)
def test_real_uv_pip_compile_pins_every_fixture(tmp_path: Path, repo: str) -> None:
    spec = PythonAdapter(cache_dir=tmp_path / "cache").env_spec(REPOS / repo, {})
    pins = [line for line in spec.requirements_txt.splitlines() if line and not line.startswith("#")]
    assert any(line.startswith("pytest==") for line in pins)
    assert all("==" in line for line in pins)
    assert not any(
        line.split("==")[0] in ("reqapp", "pyproj-app", "cfgapp", "pyapp", "entry-app") for line in pins
    )


def test_entry_points_for_other_tools_do_not_need_an_install(tmp_path: Path) -> None:
    base = '[project]\nname = "x"\nversion = "1"\n[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
    write(
        tmp_path / "exporter",
        {
            "pyproject.toml": base + '[project.entry-points."nbconvert.exporters"]\nx = "x.export:X"\n',
            "x/__init__.py": "",
            "x/deps.py": 'NEEDS = "nbconvert.exporters"  # only a string here\n',
        },
    )
    plan = resolve_install(tmp_path / "exporter", "auto", packages=("x",))
    assert plan.mode == "none" and "other tools (nbconvert.exporters)" in plan.reasons[1]
    write(
        tmp_path / "plugin",
        {"pyproject.toml": base + '[project.entry-points.pytest11]\nx = "x.plugin"\n', "x/__init__.py": ""},
    )
    assert resolve_install(tmp_path / "plugin", "auto", packages=("x",)).mode == "wheel"


# -- the planner never lets a pull request choose what the control plane fetches -----------------------------


@pytest.mark.security
async def test_an_untrusted_pull_request_cannot_make_the_planner_fetch_its_urls(
    settings: object, tmp_path: Path
) -> None:
    """The planner describes an untrusted PR's environment before its approval gate: the resolver it
    runs there sees only requirements by name, and the URL references are then refused, approved or not."""
    from types import SimpleNamespace

    from coverset.db.session import Database, init_db
    from coverset.executors.env_export import build_env_spec
    from coverset.lang.python import CoversetConfig
    from coverset.orchestrator.planner import PlanRefused, _ensure_env

    repo = write(
        tmp_path / "repo",
        {"requirements.txt": "attrs\n" + URL_REQUIREMENTS + REFUSED_REQUIREMENTS, "pkg/__init__.py": ""},
    )
    resolver = adapter(tmp_path)

    class NoBuilds:
        async def cached_env(self, env_spec: object) -> None:
            return None

        async def ensure_env(self, env_spec: object) -> None:
            raise AssertionError("an untrusted environment with direct references was built")

    await init_db(settings)  # type: ignore[arg-type]
    database = Database.from_url(settings.database_url)  # type: ignore[attr-defined]
    try:
        for approved_by, refusal in ((None, "approve to build"), ("maintainer", "direct URL")):
            run = SimpleNamespace(trusted=False, head_sha="a" * 40, id="r1", approved_by=approved_by)
            with pytest.raises(PlanRefused, match=refusal):
                await _ensure_env(
                    database,
                    settings,  # type: ignore[arg-type]
                    NoBuilds(),  # type: ignore[arg-type]
                    resolver,
                    repo,
                    CoversetConfig(),
                    lambda checkout, config: build_env_spec(resolver, checkout, {"python": "3.13"}, None),
                    run,  # type: ignore[arg-type]
                )
    finally:
        await database.engine.dispose()
    compiled_input = (tmp_path / "inputs.txt").read_text()
    assert compiled_input.splitlines() == ["attrs"]
    assert all("://" not in " ".join(call["args"]) for call in calls(tmp_path))
