"""What the report reads: one run's facts, from a bundle or from the facts store.

The pages read a run through :class:`~coverset.web.facts_view.RunFacts` (line states, partial
branches, covering tests, function views with the input-class catalogue). The report reuses it:

* ``--run`` uses ``RunFacts`` itself over the facts store, with sources from the repo's mirror at the
  run's head commit, exactly like the pages;
* a bundle (a ``--coverset-out`` directory, a ``bundle.tar.gz`` or a neutral bundle) becomes a
  :class:`BundleFacts`, a ``RunFacts`` whose tables are built in memory from the
  :class:`~coverset.lang.base.NeutralBundle` instead of read from Parquet, so every derived number
  (line state, partial note, covering tests, input classes, ``cap_hits``) comes from the same code.

Function regions come from the sources (``index_functions``, as the ingest derives them for Python)
or, for a file whose source is not available, from the bundle's own ``functions`` rows when the
neutral model carries them (read with ``getattr``: older ``NeutralBundle`` builds have no such field).
"""

from __future__ import annotations

import codecs
import dataclasses
import io
import tokenize
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

import numpy as np

from coverset.analysis.functions import index_functions
from coverset.analysis.shapes import Catalogue, FunctionCatalogue
from coverset.facts.untrusted import BundleError, open_regular, safe_rel_path
from coverset.lang.base import (
    IMPORT_CONTEXT,
    FuncSampling,
    FunctionRegion,
    NeutralBundle,
    ShapeObs,
    StaticFile,
)
from coverset.web.facts_view import FunctionView, RunFacts

__all__ = [
    "BundleFacts",
    "ReportInput",
    "TestInfo",
    "decode_source",
    "descriptor_annotation",
    "read_sources",
]

MAX_SOURCE_BYTES = 4 * 1024 * 1024
"""Larger source files are shown as "source not available" (the report embeds every line)."""
NOT_RUN = "not run"
"""The label of a test that was collected (or planned) but never ran."""
NO_RESULT = "no result"
"""The label of a test that ran measured lines but has no result row (its outcome is unknown)."""


@dataclass(frozen=True, slots=True)
class TestInfo:
    """One test as the report shows it."""

    __test__ = False

    nodeid: str
    file: str
    outcome: str | None
    """``passed``, ``failed``, ..., :data:`NO_RESULT`, or None (:data:`NOT_RUN`) for a test that was
    collected but never ran."""
    duration_s: float | None = None
    failed_phase: str | None = None
    message: str | None = None


@dataclass(slots=True)
class ReportInput:
    """Everything one report is built from."""

    facts: RunFacts
    tests: list[TestInfo]
    coverage: dict[str, dict[str, frozenset[int]]]
    """nodeid → path → executed lines (:data:`~coverset.lang.base.IMPORT_CONTEXT` included)."""
    sources: dict[str, str | None]
    """Measured path → source text (None: not available)."""
    meta: dict[str, Any]
    """Producer, tracer, caps and the bundle's own meta, shown on the about page."""
    title: str
    origin: str
    """One line saying where the facts came from (a bundle path or a run id)."""
    types: dict[str, dict[str, Any]] = field(default_factory=dict)
    """func_key → parameter (and ``return``) → type descriptor, from a neutral bundle."""
    notice: str | None = None
    """Why no source file was read (shown on every page), or None when sources were looked up."""
    commit: str | None = None
    """The commit the sources were read at (a facts-store run's head, from the repository's
    mirror), or None when they came from the local tree (``--repo-root`` or the current directory).
    ``--compare`` uses it to tell a base's own sources from borrowed ones."""
    own_regions: frozenset[str] = frozenset()
    """Paths whose function index is the bundle's own (its shipped ``functions`` rows), whatever
    local text was read for them: ``--compare`` trusts these boundaries in a changed file."""


class _Ids:
    """The two lookups ``RunFacts`` makes on the repo dictionary, over in-memory ids."""

    def __init__(self, nodeids: list[str]) -> None:
        self._nodeids = nodeids

    def nodeid(self, test_id: int) -> str:
        return self._nodeids[int(test_id)]


class BundleFacts(RunFacts):
    """A :class:`RunFacts` over an in-memory :class:`NeutralBundle` (no store, no Parquet)."""

    def __init__(
        self,
        bundle: NeutralBundle,
        regions: list[FunctionRegion],
        *,
        catalogues: Mapping[str, FunctionCatalogue] | None = None,
    ) -> None:  # noqa: D107 - RunFacts' store-backed __init__ is replaced on purpose
        self.store = None  # type: ignore[assignment]
        self.repo_id = 0
        self.run_id = "bundle"
        self.bundle = bundle
        self._regions = regions
        self._catalogues = dict(catalogues or {})

    # -- the tables RunFacts reads, built from the bundle -----------------------------------------

    @cached_property
    def _function_rows(self) -> list[dict[str, Any]]:
        """No store ``functions`` table: the declared types a bundle ships reach the report as
        ``catalogues`` (see :meth:`function_view`), so the store's descriptors are empty here."""
        return []

    @cached_property
    def _nodeids(self) -> list[str]:
        names = [IMPORT_CONTEXT]
        names += sorted(n for n in self.bundle.coverage if n != IMPORT_CONTEXT)
        return names

    @cached_property
    def ids(self) -> _Ids:  # type: ignore[override]
        return _Ids(self._nodeids)

    @cached_property
    def paths(self) -> dict[str, int]:
        found: set[str] = set(self.bundle.static)
        for by_path in self.bundle.coverage.values():
            found.update(by_path)
        found.update(t.file for t in self.bundle.tests)
        return {path: index for index, path in enumerate(sorted(found), start=1)}

    @cached_property
    def statics(self) -> dict[str, StaticFile]:
        return dict(sorted(self.bundle.static.items()))

    @cached_property
    def branch_lines(self) -> dict[str, frozenset[int]]:
        # As the ingest records file_static.branch_lines: every source line of a possible arc.
        return {p: frozenset(a for a, _ in s.arcs_possible if a > 0) for p, s in self.statics.items()}

    @cached_property
    def _lines(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        test_ids = {name: index for index, name in enumerate(self._nodeids)}
        files: list[int] = []
        lines: list[int] = []
        tests: list[int] = []
        for nodeid, by_path in self.bundle.coverage.items():
            tid = test_ids[nodeid]
            for path, executed in by_path.items():
                fid = self.paths[path]
                for line in executed:
                    files.append(fid)
                    lines.append(line)
                    tests.append(tid)
        file_id = np.asarray(files, dtype=np.int64)
        line = np.asarray(lines, dtype=np.int64)
        test_id = np.asarray(tests, dtype=np.int64)
        order = np.lexsort((test_id, line, file_id))
        return file_id[order], line[order], test_id[order]

    @cached_property
    def run_arcs(self) -> dict[str, frozenset[tuple[int, int]]]:
        return {path: frozenset(arcs) for path, arcs in self.bundle.run_arcs.items()}

    @cached_property
    def regions(self) -> list[FunctionRegion]:
        return list(self._regions)

    @cached_property
    def _observations(self) -> dict[str, list[ShapeObs]]:
        by_triple = {r.triple: r.func_key for r in self._regions}
        by_name: dict[tuple[str, str], list[str]] = defaultdict(list)
        for r in self._regions:
            by_name[(r.path, r.qualname)].append(r.func_key)
        out: dict[str, list[ShapeObs]] = defaultdict(list)
        for obs in self.bundle.shapes:
            key = by_triple.get(obs.func)
            if key is None:
                named = by_name.get((obs.func[0], obs.func[1]), [])
                key = named[0] if len(named) == 1 else None
            if key is not None:
                out[key].append(obs)
        return out

    def sampling(self, region: FunctionRegion) -> FuncSampling | None:
        rows = [s for s in self.bundle.sampling if s.func == region.triple]
        if not rows:
            rows = [s for s in self.bundle.sampling if s.func[:2] == region.triple[:2]]
            if len({s.func for s in rows}) > 1:
                rows = []
        if not rows:
            return None
        return FuncSampling(
            func=region.triple,
            calls=sum(r.calls for r in rows),
            tests=tuple(sorted({t for r in rows for t in r.tests})),
            truncated=any(r.truncated for r in rows),
        )

    @cached_property
    def input_cov_rows(self) -> dict[str, dict[str, Any]]:
        return {}

    def treemap_nodes(self) -> list[Any]:  # pragma: no cover - the report builds its own
        raise NotImplementedError("the report computes treemap nodes from function views")

    def function_view(self, region: FunctionRegion, source: str | None) -> FunctionView:
        """The pages' function view; when the bundle ships type descriptors for the function they
        stand in for the source annotations (a TypeScript file has none the Python parser could
        read, and a converted Python bundle's descriptors came from those annotations)."""
        found = self._catalogues.get(region.func_key)
        if found is None:
            return super().function_view(region, source)
        return view_with_catalogue(self, region, found)

    def catalogue_for(self, region: FunctionRegion, source: str | None) -> FunctionCatalogue:
        """The catalogue :meth:`function_view` uses: the shipped descriptors, else the source's."""
        found = self._catalogues.get(region.func_key)
        return found if found is not None else super().catalogue_for(region, source)


def view_with_catalogue(facts: RunFacts, region: FunctionRegion, found: FunctionCatalogue) -> FunctionView:
    """:meth:`RunFacts.function_view` with a given catalogue (annotations from type descriptors).

    Mirrors ``RunFacts.function_view`` line for line except where the catalogue comes from; it is
    used for every function whose bundle ships type descriptors.
    """
    from coverset.analysis.shapes import ParamInfo
    from coverset.web.facts_view import _cell

    catalogue = found.catalogue
    observations = facts.observations(region.func_key)
    sampling = facts.sampling(region)
    capped = (sampling is not None and sampling.truncated) or any(o.truncated for o in observations)
    cov = found.input_cov(
        region,
        observations,
        sampling,
        tests_called=facts.tests_in_range(region.path, region.body_start, region.end_line)
        if capped
        else None,
    )
    expected = {c.name for c in cov.expected}
    samples: dict[str, int] = defaultdict(int)
    for obs in observations:
        for name in catalogue.observed_classes([obs]) & expected:
            samples[name] += obs.samples
    returned = [dataclasses.replace(o, param="returns") for o in observations if o.param == "return"]
    return_cells: tuple[Any, ...] = ()
    if returned:
        info = ParamInfo("returns", return_annotation(found))
        seen = catalogue.observed_classes(returned)
        ret_samples: dict[str, int] = defaultdict(int)
        for obs in returned:
            for name in catalogue.observed_classes([obs]):
                ret_samples[name] += obs.samples
        return_cells = tuple(
            dataclasses.replace(
                _cell(cls, True, ret_samples.get(cls.name, 0)),
                name="return:" + cls.name.split(":", 1)[1],
                role="return",
            )
            for cls in catalogue.expected_classes(info, returned)
            if cls.name in seen
        )
    line_pct, branch_pct = facts.body_coverage(region)
    return FunctionView(
        region=region,
        cov=cov,
        observations=tuple(observations),
        samples=dict(samples),
        annotations=found.annotations,
        returns=found.returns,
        return_cells=return_cells,
        line_pct=line_pct,
        branch_pct=branch_pct,
        catalogue=catalogue,
    )


# -- type descriptors -------------------------------------------------------------------------------

_SCALARS = {"int": "int", "float": "float", "str": "str", "bool": "bool", "bytes": "bytes", "none": "None"}


def descriptor_annotation(desc: object, enums: dict[str, tuple[str, ...]] | None = None) -> str | None:
    """A Python annotation text equivalent to a neutral type descriptor (None for ``any``/unknown).

    The input-class catalogue reads annotation text; this gives a TypeScript (or any) bundle's
    declared types the same treatment as Python annotations. ``enums`` collects enum members.
    """
    if not isinstance(desc, Mapping):
        return None
    kind = desc.get("kind")
    if kind in _SCALARS:
        return _SCALARS[str(kind)]
    if kind in ("list", "set"):
        elem = descriptor_annotation(desc.get("elem"), enums) or "object"
        return f"{kind}[{elem}]"
    if kind == "tuple":
        elems = desc.get("elems")
        if not isinstance(elems, list) or not elems:
            return "tuple"
        return "tuple[" + ", ".join(descriptor_annotation(e, enums) or "object" for e in elems[:16]) + "]"
    if kind == "dict":
        key = descriptor_annotation(desc.get("key"), enums) or "object"
        value = descriptor_annotation(desc.get("value"), enums) or "object"
        return f"dict[{key}, {value}]"
    if kind == "union":
        arms = desc.get("arms")
        if not isinstance(arms, list):
            return None
        texts = [descriptor_annotation(a, enums) for a in arms[:16]]
        if any(t is None for t in texts):
            return None
        return " | ".join(dict.fromkeys(t for t in texts if t))
    if kind == "literal":
        values = desc.get("values")
        if not isinstance(values, list) or not values:
            return None
        parts = [repr(v) for v in values[:32] if isinstance(v, str | int | float | bool) or v is None]
        return f"Literal[{', '.join(parts)}]" if parts else None
    if kind == "enum":
        name, members = desc.get("name"), desc.get("members")
        if not isinstance(name, str) or not name.isidentifier():
            return None
        if enums is not None and isinstance(members, list):
            enums[name] = tuple(str(m) for m in members if isinstance(m, str) and m.isidentifier())
        return name
    if kind == "object":
        name = desc.get("name")
        if isinstance(name, str) and all(part.isidentifier() for part in name.split(".")):
            return name.rsplit(".", 1)[-1]
    return None


def catalogue_from_descriptors(region: FunctionRegion, types: Mapping[str, Any]) -> FunctionCatalogue:
    """The :class:`FunctionCatalogue` of a function whose declared types came as descriptors.

    With the neutral model (``coverset_model``) this is its ``function_catalogue_from_types``, so a
    TypeScript bundle's declared types count exactly as they do at ingest; descriptors given as
    JSON objects are parsed with ``coverset_model.types.from_json`` first (a malformed one counts as
    undeclared). Without the model, the descriptors become annotation text for the catalogue.
    """
    try:
        from coverset.analysis.shapes import function_catalogue_from_types  # type: ignore[attr-defined]
        from coverset_model.types import DescriptorError, from_json  # type: ignore[import-not-found]
    except ImportError:
        enums: dict[str, tuple[str, ...]] = {}
        annotations = {
            p.lstrip("*"): descriptor_annotation(types.get(p.lstrip("*"), types.get(p)), enums)
            for p in region.params
        }
        returns = descriptor_annotation(types.get("return"), enums)
        return FunctionCatalogue(Catalogue(enums.get if enums else None), annotations, returns)
    parsed: dict[str, Any] = {}
    for name, desc in types.items():
        if isinstance(desc, Mapping):
            try:
                desc = from_json(dict(desc))
            except DescriptorError:
                desc = None
        parsed[str(name)] = desc
    return function_catalogue_from_types(region, parsed)  # type: ignore[no-any-return]


def return_annotation(found: FunctionCatalogue) -> Any:
    """The declared return type the catalogue reads (a descriptor with the neutral model)."""
    getter = getattr(found, "return_type", None)
    return getter() if callable(getter) else found.returns


# -- sources ------------------------------------------------------------------------------------------


def read_sources(
    root: Path, paths: Iterable[str], *, tracked: frozenset[str] | None = None
) -> dict[str, str | None]:
    """Each path's text under ``root`` (None when missing, unsafe, too large or outside ``root``).

    Only plain relative paths are read, symlinks are resolved and must stay inside ``root``, and only
    regular files up to :data:`MAX_SOURCE_BYTES` are read (:func:`decode_source`). With ``tracked``
    (the paths git tracks under ``root``), a path is read only when it and the file it resolves to
    are both in it.
    """
    base = Path(root).resolve()
    out: dict[str, str | None] = {}
    for path in paths:
        out[path] = None
        if safe_rel_path(path) is None or (tracked is not None and path not in tracked):
            continue
        try:
            candidate = (base / path).resolve(strict=True)
            if not candidate.is_relative_to(base):
                continue
            if tracked is not None and candidate.relative_to(base).as_posix() not in tracked:
                continue
            with open_regular(candidate) as fh:
                data = fh.read(MAX_SOURCE_BYTES + 1)
        except (OSError, BundleError, ValueError):
            continue
        if len(data) > MAX_SOURCE_BYTES:
            continue
        out[path] = decode_source(data, path)
    return out


_COOKIE_FAMILIES = (
    "utf-8",
    "ascii",
    "latin-1",
    "iso8859",
    "cp",
    "mac-",
    "koi8",
    "euc",
    "shift_jis",
    "gb",
    "big5",
)


def decode_source(data: bytes, path: str = "") -> str:
    """Source bytes as text: a UTF-8 byte-order mark is dropped, a Python file's coding cookie is
    honoured (as :func:`tokenize.detect_encoding` reads it) when it names an ordinary text encoding,
    and anything else is UTF-8 with replacement characters."""
    if path.endswith((".py", ".pyi", ".pyw")):
        try:
            encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
            name = codecs.lookup(encoding).name
        except (SyntaxError, LookupError):
            name = ""
        if name.startswith(_COOKIE_FAMILIES) and name not in ("utf-8", "utf-8-sig"):
            text = data.decode(name, "replace")
            return text[1:] if text.startswith("\ufeff") else text
    if data.startswith(codecs.BOM_UTF8):
        data = data[len(codecs.BOM_UTF8) :]
    return data.decode("utf-8", "replace")


def regions_for(
    paths: Iterable[str], sources: Mapping[str, str | None], bundle: NeutralBundle | None = None
) -> list[FunctionRegion]:
    """Every region of the measured files: indexed from the source when there is one, else the
    bundle's own ``functions`` rows for that path (neutral bundles)."""
    return regions_with_owners(paths, sources, bundle)[0]


def regions_with_owners(
    paths: Iterable[str],
    sources: Mapping[str, str | None],
    bundle: NeutralBundle | None = None,
    *,
    prefer_shipped: bool = False,
) -> tuple[list[FunctionRegion], frozenset[str]]:
    """:func:`regions_for`, plus the paths whose regions are the bundle's own ``functions`` rows
    (not indexed from a local text that may be another commit's). With ``prefer_shipped`` (a
    ``--compare`` base) a path's shipped rows win over indexing its local text."""
    shipped: dict[str, list[FunctionRegion]] = defaultdict(list)
    for region in bundle_regions(bundle):
        shipped[region.path].append(region)
    out: list[FunctionRegion] = []
    own: set[str] = set()
    for path in sorted(set(paths)):
        if prefer_shipped and shipped.get(path):
            out.extend(shipped[path])
            own.add(path)
            continue
        text = sources.get(path)
        if text is not None and path.endswith(".py"):
            try:
                out.extend(index_functions(path, text))
                continue
            except (SyntaxError, ValueError, RecursionError):
                pass
        if shipped.get(path):
            out.extend(shipped[path])
            own.add(path)
    return out, frozenset(own)


def borrowed_signatures(
    regions: Iterable[FunctionRegion], sources: Mapping[str, str | None], own: frozenset[str]
) -> dict[str, FunctionCatalogue]:
    """For shipped Python regions whose local text is another commit's: the functions whose
    signature there differs (``sig_hash``) or is missing, each with a catalogue of no declared
    types, so the local annotations are never read for them (their classes then come from the
    observations alone)."""
    out: dict[str, FunctionCatalogue] = {}
    local: dict[str, dict[str, FunctionRegion]] = {}
    for region in regions:
        text = sources.get(region.path)
        if region.path not in own or text is None or not region.path.endswith(".py"):
            continue
        if region.path not in local:
            try:
                local[region.path] = {r.func_key: r for r in index_functions(region.path, text)}
            except (SyntaxError, ValueError, RecursionError):
                local[region.path] = {}
        found = local[region.path].get(region.func_key)
        if found is None or found.sig_hash != region.sig_hash:
            out[region.func_key] = FunctionCatalogue(Catalogue(), {}, None)
    return out


def bundle_regions(bundle: object) -> list[FunctionRegion]:
    """``bundle.functions`` as regions, when the bundle carries them (neutral model), else []."""
    rows = getattr(bundle, "functions", None) or ()
    out: list[FunctionRegion] = []
    for row in rows:
        if isinstance(row, FunctionRegion):
            out.append(row)
            continue
        try:
            out.append(
                FunctionRegion(
                    func_key=str(_get(row, "func_key", _get(row, "key"))),
                    path=str(_get(row, "path")),
                    qualname=str(_get(row, "qualname")),
                    ordinal=int(_get(row, "ordinal", 0)),
                    def_line=int(_get(row, "def_line")),
                    body_start=int(_get(row, "body_start")),
                    end_line=int(_get(row, "end_line")),
                    body_hash=str(_get(row, "body_hash", "")),
                    sig_hash=str(_get(row, "sig_hash", "")),
                    params=tuple(str(p) for p in (_get(row, "params", ()) or ())),
                    kind=str(_get(row, "kind", "function")),  # type: ignore[arg-type]
                )
            )
        except (TypeError, ValueError):
            continue
    return out


def bundle_types(bundle: object, regions: Iterable[FunctionRegion]) -> dict[str, dict[str, Any]]:
    """func_key → parameter → type descriptor, from whatever form the neutral model ships them in.

    Accepted: ``bundle.types`` keyed by func_key, by ``(path, qualname, def_line)`` or by
    ``(path, qualname)``; or a ``types`` attribute (or key) on each ``bundle.functions`` row.
    """
    regions = list(regions)
    out: dict[str, dict[str, Any]] = {}
    table = getattr(bundle, "types", None)
    if isinstance(table, Mapping):
        for region in regions:
            for key in (region.func_key, region.triple, region.triple[:2]):
                found = table.get(key)
                if isinstance(found, Mapping):
                    out[region.func_key] = dict(found)
                    break
    by_key = {r.func_key: r for r in regions}
    for row in getattr(bundle, "functions", None) or ():
        types = _get(row, "types", None)
        key = _get(row, "func_key", _get(row, "key", None))
        if isinstance(types, Mapping) and key in by_key:
            out.setdefault(str(key), dict(types))
    return out


def _get(row: object, name: str, default: object = ...) -> Any:
    if isinstance(row, Mapping):
        if name in row:
            return row[name]
    elif hasattr(row, name):
        return getattr(row, name)
    if default is ...:
        raise ValueError(f"missing {name}")
    return default
