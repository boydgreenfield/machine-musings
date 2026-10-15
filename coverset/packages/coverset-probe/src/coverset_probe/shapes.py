"""Which implementation computes shape keys: the plugin's when it is importable, else the vendored copy.

Both produce the same keys (the parity tests hold them to it); preferring the plugin's module means
a process that has pytest-coverset installed computes production keys with exactly the code that
computed the tests' keys. Importing ``pytest_coverset.features`` loads only ``math``, ``re`` and
``itertools`` (the package ``__init__`` holds a version string), never pytest.

:func:`valid_key` checks a key against the version-1 grammar of :mod:`coverset_model.shape_keys`
before it leaves the process, so a producer bug can never smuggle free text into an export.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from coverset_model.shape_keys import is_valid

__all__ = ["FEATURES_SOURCE", "object_shape_key", "shape_key", "valid_key"]

shape_key: Callable[[object], str]
object_shape_key: Callable[[type], str]

try:  # pragma: no cover - which branch runs depends on the environment
    from pytest_coverset.features import object_shape_key, shape_key

    FEATURES_SOURCE: Final = "pytest_coverset"
except ImportError:  # pragma: no cover
    from coverset_probe._features import object_shape_key, shape_key

    FEATURES_SOURCE: Final = "vendored"  # type: ignore[misc]


def valid_key(key: str) -> bool:
    """Whether ``key`` follows the shape-key grammar (exported keys always do)."""
    return is_valid(key)
