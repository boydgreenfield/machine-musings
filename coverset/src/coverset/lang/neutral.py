"""The neutral language adapter: any language whose reporter writes a neutral bundle.

A TypeScript (or JavaScript, Go, ...) suite is run by the repository's own test runner with a
reporter that writes the files of ``docs/neutral-bundle.md``; coverset only ingests them. So this
adapter cannot build an environment or a test command (:meth:`NeutralAdapter.env_spec` and
:meth:`~NeutralAdapter.test_command` raise :class:`NotRunnableError`), and it does not parse the
language's sources: function regions, their hashes and declared types come from the bundle's
``functions.jsonl``.

* :meth:`~NeutralAdapter.read_bundle` reads a bundle with ``coverset_model.io.read_bundle`` under the
  same limits as the Python reader (``BundleLimits``), raising
  :class:`~coverset.facts.untrusted.BundleError` when a hard limit is broken.
* :meth:`~NeutralAdapter.index_functions` returns the shipped regions of one path (the ``source``
  argument is ignored), from the bundle the adapter was built with.
* :meth:`~NeutralAdapter.changed_functions` compares the shipped regions of the base and the head
  run when the adapter has both (:meth:`NeutralAdapter.with_base`), by qualname and ordinal and by
  body and signature hash; without base regions every head region the diff touches is
  ``modified``.
* :meth:`~NeutralAdapter.shape_catalogue` is a catalogue over the bundle's type descriptors.

Languages are registered by name (:func:`register_language`); ``typescript`` and ``javascript`` are
registered by default.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from coverset.facts.untrusted import BundleError, BundleLimits
from coverset.lang.base import (
    EnvSpec,
    FunctionChange,
    FunctionRegion,
    FuncTriple,
    NeutralBundle,
    TypeDescriptor,
)
from coverset_model import io as model_io
from coverset_model.catalogue import Catalogue

__all__ = [
    "NEUTRAL_LANGUAGES",
    "NeutralAdapter",
    "NotRunnableError",
    "model_limits",
    "read_neutral_bundle",
    "register_language",
]

NEUTRAL_LANGUAGES: set[str] = {"typescript", "javascript"}
"""Languages served by :class:`NeutralAdapter` (see :func:`register_language`)."""


class NotRunnableError(RuntimeError):
    """coverset cannot run this language's tests; the repository's own reporter does."""


def register_language(name: str) -> None:
    """Serve ``name`` (a manifest ``language``) with :class:`NeutralAdapter`."""
    if not name or not name.isascii() or name != name.lower() or not name.replace("_", "").isalnum():
        raise ValueError(f"a language name is a lowercase identifier, got {name!r}")
    NEUTRAL_LANGUAGES.add(name)


def model_limits(limits: BundleLimits | None) -> model_io.Limits:
    """The neutral reader's limits with every field ``BundleLimits`` shares copied over."""
    return model_io.Limits.from_object(limits) if limits is not None else model_io.Limits()


def read_neutral_bundle(
    bundle_dir: Path,
    *,
    limits: BundleLimits | None = None,
    head_paths: Collection[str] | None = None,
) -> NeutralBundle:
    """Read a neutral bundle (directory or ``tar.gz``); a hard-limit breach raises :class:`BundleError`."""
    try:
        return model_io.read_bundle(bundle_dir, limits=model_limits(limits), allowed_paths=head_paths)
    except model_io.BundleRejected as exc:
        raise BundleError(f"neutral bundle refused: {exc}") from exc


class NeutralAdapter:
    """:class:`~coverset.lang.base.LanguageAdapter` for languages that ship a neutral bundle."""

    def __init__(
        self,
        language: str = "typescript",
        *,
        functions: Iterable[FunctionRegion] = (),
        types: Mapping[FuncTriple, Mapping[str, TypeDescriptor | None]] | None = None,
        base_functions: Iterable[FunctionRegion] | None = None,
    ) -> None:
        self.name = language
        self._functions = tuple(functions)
        self._types = dict(types or {})
        self._base = tuple(base_functions) if base_functions is not None else None

    @classmethod
    def from_bundle(cls, bundle: NeutralBundle, *, language: str | None = None) -> NeutralAdapter:
        """An adapter serving the regions and descriptors ``bundle`` ships."""
        recorded = bundle.meta.get("bundle", {}) if isinstance(bundle.meta, dict) else {}
        name = language or (recorded.get("language") if isinstance(recorded, dict) else None) or "typescript"
        return cls(str(name), functions=bundle.functions, types=bundle.types)

    def with_base(self, base: NeutralBundle | Iterable[FunctionRegion]) -> NeutralAdapter:
        """A copy that also knows the base run's regions, for :meth:`changed_functions`."""
        regions = base.functions if isinstance(base, NeutralBundle) else tuple(base)
        return NeutralAdapter(self.name, functions=self._functions, types=self._types, base_functions=regions)

    # -- LanguageAdapter ----------------------------------------------------------------------

    def detect(self, repo_root: Path) -> bool:
        """True when ``repo_root`` holds a neutral bundle whose manifest names this language."""
        manifest = model_io.read_manifest(repo_root)
        return manifest is not None and manifest.get("language") == self.name

    def env_spec(self, repo_root: Path, cfg: Mapping) -> EnvSpec:
        raise NotRunnableError(
            f"{self.name} tests are not runnable by coverset: the repository's own test runner "
            "and reporter run them and write a neutral bundle, which coverset ingests"
        )

    def test_command(
        self, args_file: str, scores_file: str | None, out_dir: str, extra: Mapping
    ) -> list[str]:
        raise NotRunnableError(
            f"{self.name} tests are not runnable by coverset (no test command): run them with the "
            "repository's reporter and ingest its neutral bundle"
        )

    def read_bundle(
        self,
        bundle_dir: Path,
        *,
        limits: BundleLimits | None = None,
        source_root: Path | None = None,
        head_paths: Collection[str] | None = None,
    ) -> NeutralBundle:
        """Read a neutral bundle; ``source_root`` is accepted for parity and unused (``n_lines``
        comes from ``files.jsonl``)."""
        return read_neutral_bundle(bundle_dir, limits=limits, head_paths=head_paths)

    def index_functions(self, path: str, source: str) -> list[FunctionRegion]:
        """The shipped regions of ``path`` (``source`` is not parsed)."""
        return [r for r in self._functions if r.path == path]

    def changed_functions(
        self, path: str, old_src: str | None, new_src: str | None, added: set[int], removed: set[int]
    ) -> list[FunctionChange]:
        """Classify the shipped regions of ``path`` touched by a diff (see the module docstring)."""
        head = (
            [] if new_src is None and old_src is not None else [r for r in self._functions if r.path == path]
        )
        if self._base is None:
            return [FunctionChange(r, None, "modified") for r in head if _touches(r, added)]
        base = [] if old_src is None and new_src is not None else [r for r in self._base if r.path == path]
        before = {(r.qualname, r.ordinal): r for r in base}
        after = {(r.qualname, r.ordinal): r for r in head}
        changes: list[FunctionChange] = []
        for identity, region in after.items():
            prev = before.get(identity)
            if prev is None:
                changes.append(FunctionChange(region, None, "added"))
            elif (prev.body_hash, prev.sig_hash) != (region.body_hash, region.sig_hash):
                changes.append(FunctionChange(region, prev, "modified"))
            elif _touches(region, added) or _touches(prev, removed):
                changes.append(FunctionChange(region, prev, "trivial"))
        changes += [FunctionChange(None, r, "deleted") for key, r in before.items() if key not in after]
        changes.sort(key=lambda c: (c.region or c.prev).def_line)  # type: ignore[union-attr]
        return changes

    def shape_catalogue(self, sources: Mapping[str, str] | None = None) -> Catalogue:
        """A catalogue over the bundle's declared types (``sources`` are not parsed)."""
        return Catalogue(annotations=self._types)


def _touches(region: FunctionRegion, lines: Collection[int]) -> bool:
    return any(region.def_line <= line <= region.end_line for line in lines)


def adapter_language(bundle: NeutralBundle) -> str | None:
    """The ``language`` a neutral bundle's manifest declared (None for a converted Python bundle)."""
    recorded: Any = bundle.meta.get("bundle") if isinstance(bundle.meta, dict) else None
    language = recorded.get("language") if isinstance(recorded, dict) else None
    return language if isinstance(language, str) else None


def merge_regions(
    indexed: Sequence[FunctionRegion], shipped: Sequence[FunctionRegion]
) -> list[FunctionRegion]:
    """The run's function regions: the adapter's own index per path, else the bundle's.

    A path the adapter indexed from the head sources keeps those regions (Python's index is the
    reference for Python files); a path it could not index (another language, or no source) takes
    the regions the bundle shipped in ``functions.jsonl``.
    """
    indexed_paths = {r.path for r in indexed}
    return [*indexed, *(r for r in shipped if r.path not in indexed_paths)]
