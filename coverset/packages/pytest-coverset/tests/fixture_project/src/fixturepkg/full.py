"""Functions in this file are traced without a sample cap when it is listed in --coverset-full-trace."""


def hot_full(i: int) -> int:
    """Called as often as ``core.hot``."""
    return i + 1
