"""A setup.cfg-only project (no pyproject.toml, no setup.py)."""


def clamp(value: int, low: int, high: int) -> int:
    """``value`` limited to ``[low, high]``."""
    return max(low, min(high, value))
