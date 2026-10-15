"""coverset's semantic-coverage model: the neutral bundle, type descriptors, shape keys, input classes.

No dependencies, Python 3.12+. The pieces:

* :mod:`coverset_model.neutral`: the in-memory :class:`NeutralBundle` and its rows.
* :mod:`coverset_model.io`: :func:`read_bundle`, :func:`write_bundle`, :func:`validate` for the
  on-disk format of ``docs/neutral-bundle.md``, under untrusted-input limits.
* :mod:`coverset_model.schema`: the JSON Schemas of the bundle's files and a small validator.
* :mod:`coverset_model.types`: :class:`TypeDescriptor`, the declared type of a parameter.
* :mod:`coverset_model.python_types`: Python annotation text → descriptor.
* :mod:`coverset_model.shape_keys`: the shape-key grammar.
* :mod:`coverset_model.catalogue`: the input-class catalogue (expected and observed classes).
"""

from coverset_model.catalogue import (
    NEVER_EXERCISED,
    Catalogue,
    FunctionCatalogue,
    FunctionInputCov,
    InputClass,
    ParamInfo,
    cap_hits,
    describe,
    function_catalogue_from_types,
    function_input_cov,
    render_markdown,
    suggest_annotation,
)
from coverset_model.io import (
    BundleRejected,
    Limits,
    Problem,
    conform,
    inspect,
    is_neutral,
    read_bundle,
    validate,
    write_bundle,
)
from coverset_model.neutral import (
    IMPORT_CONTEXT,
    CollectedItem,
    FrozenMap,
    FuncSampling,
    FunctionChange,
    FunctionRegion,
    FuncTriple,
    InputCov,
    NeutralBundle,
    ShapeObs,
    StaticFile,
    TestRow,
    func_key,
)
from coverset_model.shape_keys import ParsedShape, ShapeKeyError, parse_shape_key
from coverset_model.types import TypeDescriptor, expected_families

__version__ = "0.1.0"

__all__ = [
    "IMPORT_CONTEXT",
    "NEVER_EXERCISED",
    "BundleRejected",
    "Catalogue",
    "CollectedItem",
    "FrozenMap",
    "FuncSampling",
    "FuncTriple",
    "FunctionCatalogue",
    "FunctionChange",
    "FunctionInputCov",
    "FunctionRegion",
    "InputClass",
    "InputCov",
    "Limits",
    "NeutralBundle",
    "ParamInfo",
    "ParsedShape",
    "Problem",
    "ShapeKeyError",
    "ShapeObs",
    "StaticFile",
    "TestRow",
    "TypeDescriptor",
    "__version__",
    "cap_hits",
    "conform",
    "describe",
    "expected_families",
    "func_key",
    "function_catalogue_from_types",
    "function_input_cov",
    "inspect",
    "is_neutral",
    "parse_shape_key",
    "read_bundle",
    "render_markdown",
    "suggest_annotation",
    "validate",
    "write_bundle",
]
