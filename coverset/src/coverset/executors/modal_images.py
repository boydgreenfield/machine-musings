"""Modal images for coverset: the environment image recipe, building it, and looking it up by id.

One image per :class:`~coverset.lang.base.EnvSpec` (``env_hash``)::

    debian_slim(python_version=<repo python>)
      .apt_install("git")
      .env({"UV_COMPILE_BYTECODE": "1", "PYTHONUNBUFFERED": "1"})
      .uv_pip_install(requirements=[<requirements.txt>])      # the repo's locked third-party deps
      .add_local_file(<wheel>, "/opt/wheels/<name>", copy=True)
      .run_commands("pip install --no-deps /opt/wheels/<name>")  # pytest-coverset, no dependencies

Everything lands in the system interpreter, so a worker runs ``python -m pytest ...`` with
``PYTHONPATH`` pointing at the checkout; the project itself is never installed. The plugin wheel is
installed with ``--no-deps``, exactly as the LocalExecutor does: its runtime dependencies are already
in the requirements, because :func:`coverset.executors.env_export.with_plugin_pins` appends an exact
``coverage`` pin (and ``pytest`` when the lock has none) to a target lockfile that lacks them.

**Image builder version.** Modal workspaces default to Image Builder Version 2023.12, whose
``debian_slim`` supports Python up to 3.12 only; coverset's demo targets 3.13. The client honours a
per-process override (config key ``image_builder_version`` / ``MODAL_IMAGE_BUILDER_VERSION``), and
2025.06 supports Python 3.10 to 3.14, so :func:`configure_builder` sets it before any image is
defined or built (verified live on 2026-10-10; without it the 3.13 build is refused). An explicit
``MODAL_IMAGE_BUILDER_VERSION`` in the environment wins.

**Lookups.** :func:`image_from_id` returns the lazy ``modal.Image.from_id(...)``. It must never be
hydrated by hand (``Image.from_id(id).hydrate()`` raises "Images cannot currently be hydrated on
demand"): pass it straight to ``Sandbox.create(image=...)``, which hydrates it and raises
``modal.exception.NotFoundError`` for a missing id, the executor's cue to rebuild.
"""

from __future__ import annotations

import contextlib
import importlib
import os
from pathlib import Path
from types import ModuleType
from typing import Any

from coverset.executors.base import ExecutorError, cache_root
from coverset.lang.base import EnvSpec

__all__ = [
    "IMAGE_BUILDER_VERSION",
    "WHEEL_DIR",
    "build_and_record",
    "configure_builder",
    "env_image",
    "image_from_id",
    "import_modal",
    "requirements_file",
]

IMAGE_BUILDER_VERSION = "2025.06"
"""The Modal Image Builder Version coverset needs (Python 3.13 images; see the module docstring)."""

WHEEL_DIR = "/opt/wheels"
"""Where the pytest-coverset wheel is copied inside the image."""

_BUILDER_ENV = "MODAL_IMAGE_BUILDER_VERSION"
_IMAGE_ENV = {"UV_COMPILE_BYTECODE": "1", "PYTHONUNBUFFERED": "1"}


def import_modal() -> ModuleType:
    """Import the Modal client with the image builder version configured (a clear error if absent)."""
    try:
        modal = importlib.import_module("modal")
    except ImportError as exc:
        raise ExecutorError(f"the modal client is not installed: {exc}") from exc
    configure_builder(modal)
    return modal


def configure_builder(modal: ModuleType | None = None) -> str:
    """Make this process build images with :data:`IMAGE_BUILDER_VERSION`; return the version in effect.

    Must run before any image is defined or built. Uses ``modal.config.config.override_locally`` when
    available (it sets the same environment variable); a value already in the environment is kept.
    """
    current = os.environ.get(_BUILDER_ENV)
    if current:
        return current
    config = getattr(getattr(modal, "config", None), "config", None)
    override = getattr(config, "override_locally", None)
    if callable(override):
        with contextlib.suppress(Exception):
            override("image_builder_version", IMAGE_BUILDER_VERSION)
    os.environ.setdefault(_BUILDER_ENV, IMAGE_BUILDER_VERSION)
    return os.environ[_BUILDER_ENV]


def requirements_file(spec: EnvSpec, directory: Path | None = None) -> Path:
    """Write ``spec.requirements_txt`` to ``<cache>/modal/requirements/<env_hash>.txt`` and return it.

    The file is content-addressed (named by the environment hash) and kept, because Modal reads it
    lazily when the image is built, which may be after this function returns.
    """
    folder = directory or cache_root() / "modal" / "requirements"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{spec.env_hash}.txt"
    if not path.is_file() or path.read_text(encoding="utf-8") != spec.requirements_txt:
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(spec.requirements_txt, encoding="utf-8")
        os.replace(tmp, path)
    return path


def _has_requirements(text: str) -> bool:
    return any(line.strip() and not line.lstrip().startswith(("#", "-")) for line in text.splitlines())


def env_image(
    spec: EnvSpec,
    plugin_wheel: Path | None,
    *,
    modal: ModuleType | None = None,
    requirements_dir: Path | None = None,
) -> Any:
    """The (lazy) ``modal.Image`` for ``spec``: the verified recipe in the module docstring.

    ``plugin_wheel`` defaults to ``spec.plugin_wheel``; without one the image has no plugin (only
    useful for smoke tests). Nothing is built until the image is used or :func:`build_and_record`.
    """
    modal = modal or import_modal()
    configure_builder(modal)
    image = modal.Image.debian_slim(python_version=spec.python).apt_install("git").env(dict(_IMAGE_ENV))
    if _has_requirements(spec.requirements_txt):
        requirements = requirements_file(spec, requirements_dir)
        image = image.uv_pip_install(requirements=[str(requirements)])
    wheel = plugin_wheel or spec.plugin_wheel
    if wheel is not None:
        wheel = Path(wheel)
        if not wheel.is_file():
            raise ExecutorError(f"plugin wheel not found: {wheel}")
        remote = f"{WHEEL_DIR}/{wheel.name}"
        image = image.add_local_file(str(wheel), remote, copy=True).run_commands(
            f"pip install --no-deps {remote}"
        )
    return image


async def build_and_record(
    app: Any, image: Any, *, verbose: bool = False, modal: ModuleType | None = None
) -> str:
    """Build ``image`` in ``app`` (a no-op when Modal has it cached) and return its object id.

    With ``verbose`` the build log streams to the terminal (``modal.enable_output()``).
    """
    modal = modal or import_modal()
    output = modal.enable_output() if verbose else contextlib.nullcontext()
    try:
        with output:
            built = await image.build.aio(app)
    except Exception as exc:
        raise ExecutorError(f"building the Modal image failed: {type(exc).__name__}: {exc}") from exc
    image_id = getattr(built, "object_id", None) or getattr(image, "object_id", None)
    if not image_id:
        raise ExecutorError("the Modal image build returned no object id")
    return str(image_id)


def image_from_id(image_id: str, *, modal: ModuleType | None = None) -> Any:
    """The lazy ``modal.Image.from_id(image_id)``; never hydrate it (see the module docstring)."""
    modal = modal or import_modal()
    return modal.Image.from_id(image_id)
