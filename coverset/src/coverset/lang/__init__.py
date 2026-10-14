"""Language adapters and the language-neutral bundle contract (see :mod:`coverset.lang.base`).

:func:`read_any_bundle` reads a shard bundle whatever produced it: a neutral bundle (its
``manifest.json`` says ``coverset-neutral``, any language) through ``coverset_model.io``, anything
else as a pytest-coverset bundle through :func:`~coverset.facts.convert.read_python_bundle`.
:func:`adapter_for_language` returns the adapter that serves a language name.
"""

from __future__ import annotations

import tempfile
from collections.abc import Collection
from pathlib import Path
from typing import TYPE_CHECKING

from coverset.lang.base import (
    IMPORT_CONTEXT,
    Catalogue,
    CollectedItem,
    EnvSpec,
    FuncSampling,
    FunctionChange,
    FunctionRegion,
    FuncTriple,
    InputCov,
    LanguageAdapter,
    NeutralBundle,
    Outcome,
    PriorRun,
    ShapeObs,
    StaticFile,
    TestRow,
    TypeDescriptor,
    func_key,
)

if TYPE_CHECKING:
    from coverset.facts.untrusted import BundleLimits

__all__ = [
    "IMPORT_CONTEXT",
    "Catalogue",
    "CollectedItem",
    "EnvSpec",
    "FuncSampling",
    "FuncTriple",
    "FunctionChange",
    "FunctionRegion",
    "InputCov",
    "LanguageAdapter",
    "NeutralBundle",
    "Outcome",
    "PriorRun",
    "ShapeObs",
    "StaticFile",
    "TestRow",
    "TypeDescriptor",
    "adapter_for_language",
    "bundle_kind",
    "func_key",
    "read_any_bundle",
]


def bundle_kind(bundle_dir: Path) -> str:
    """``"neutral"`` when the directory's ``manifest.json`` says ``coverset-neutral``, else ``"python"``."""
    from coverset_model.io import is_neutral

    return "neutral" if is_neutral(bundle_dir) else "python"


def read_any_bundle(
    path: Path | str,
    *,
    limits: BundleLimits | None = None,
    source_root: Path | None = None,
    head_paths: Collection[str] | None = None,
) -> NeutralBundle:
    """Read the bundle at ``path`` (a directory, or a ``tar.gz`` that is extracted safely first).

    A neutral bundle is read by ``coverset_model.io.read_bundle`` (whatever its language), any
    other directory by :func:`~coverset.facts.convert.read_python_bundle`. ``limits`` are the
    untrusted-bundle caps of both readers, ``head_paths`` the head tree's paths (rows naming any
    other path are dropped and counted), ``source_root`` a head checkout (the Python reader takes
    ``n_lines`` from it; a neutral bundle carries ``n_lines``). Raises
    :class:`~coverset.facts.untrusted.BundleError` when a bundle breaks a hard limit.
    """
    from coverset.facts.convert import read_python_bundle
    from coverset.facts.untrusted import BundleError, extract_bundle
    from coverset.lang.neutral import read_neutral_bundle

    path = Path(path)
    if path.is_file() and not path.is_symlink():
        with tempfile.TemporaryDirectory(prefix="coverset-bundle-") as tmp:
            root = extract_bundle(path, Path(tmp) / "bundle", limits)
            entries = list(root.iterdir())
            if len(entries) == 1 and entries[0].is_dir() and not entries[0].is_symlink():
                root = entries[0]  # an archive of one top-level directory
            return read_any_bundle(root, limits=limits, source_root=source_root, head_paths=head_paths)
    if not path.is_dir():
        raise BundleError(f"{path.name} is neither a bundle directory nor a bundle archive")
    if bundle_kind(path) == "neutral":
        return read_neutral_bundle(path, limits=limits, head_paths=head_paths)
    return read_python_bundle(path, limits=limits, source_root=source_root, head_paths=head_paths)


def adapter_for_language(language: str) -> LanguageAdapter:
    """The adapter serving ``language``: Python's, or the neutral adapter for a registered name."""
    if language == "python":
        from coverset.lang.python import PythonAdapter

        return PythonAdapter()
    if language == "rust":  # a NeutralAdapter that also indexes Rust sources (lang/rust.py)
        from coverset.lang.rust import RustAdapter

        return RustAdapter()
    from coverset.lang.neutral import NEUTRAL_LANGUAGES, NeutralAdapter

    if language in NEUTRAL_LANGUAGES:
        return NeutralAdapter(language)
    from coverset.facts.untrusted import BundleError

    raise BundleError(f"no language adapter for {language!r}")


def _register_rust() -> None:
    """``rust`` bundles are neutral bundles, so code that checks ``NEUTRAL_LANGUAGES`` (the upload
    path) accepts a Rust manifest; :func:`adapter_for_language` still answers ``RustAdapter``."""
    from coverset.lang.neutral import register_language

    register_language("rust")


_register_rust()
