"""Fixtures for the probe tests: throwaway target modules and a guaranteed probe shutdown.

Every test gets modules compiled fresh under a unique name, so code objects never carry
``sys.monitoring`` state (a ``DISABLE`` from an earlier probe) from one test into the next.
"""

from __future__ import annotations

import itertools
import sys
import textwrap
import types
from collections.abc import Callable, Iterator

import coverset_probe
import pytest

_COUNTER = itertools.count()

type ModuleFactory = Callable[[str], types.ModuleType]


@pytest.fixture(autouse=True)
def _no_probe_left_running() -> Iterator[None]:
    """Stop whatever probe a test left running (a failed assertion must not leak a tool id)."""
    yield
    coverset_probe.stop(flush=False)


@pytest.fixture
def make_module() -> Iterator[ModuleFactory]:
    """Compile ``source`` as a new module ``probeapp_<n>`` and register it in ``sys.modules``."""
    created: list[str] = []

    def factory(source: str) -> types.ModuleType:
        name = f"probeapp_{next(_COUNTER)}"
        module = types.ModuleType(name)
        module.__file__ = f"/srv/app/{name}.py"
        sys.modules[name] = module
        exec(compile(textwrap.dedent(source), module.__file__, "exec"), module.__dict__)
        created.append(name)
        return module

    yield factory
    for name in created:
        sys.modules.pop(name, None)
