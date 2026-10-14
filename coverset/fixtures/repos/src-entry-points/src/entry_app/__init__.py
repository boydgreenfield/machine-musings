"""A src-layout project that only works installed: it reads its version and its plugins from metadata."""

from importlib.metadata import entry_points, version

__version__ = version("entry-app")


def plugins() -> dict[str, object]:
    """Plugins registered under the ``entry_app.plugins`` entry-point group, by name."""
    return {ep.name: ep.load() for ep in entry_points(group="entry_app.plugins")}
