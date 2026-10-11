"""Format second counts back into compact or human-readable durations."""

_COMPACT_UNITS = (("d", 86_400), ("h", 3_600), ("m", 60), ("s", 1))
_HUMAN_UNITS = (("day", 86_400), ("hour", 3_600), ("minute", 60))


def format_duration(seconds: int) -> str:
    """Format ``seconds`` as ``1d2h3m4s``, omitting zero parts; zero is ``"0s"``."""
    if seconds < 0:
        raise ValueError(f"durations are non-negative: {seconds}")
    if seconds == 0:
        return "0s"
    parts = []
    remaining = seconds
    for unit, size in _COMPACT_UNITS:
        amount, remaining = divmod(remaining, size)
        if amount:
            parts.append(f"{amount}{unit}")
    return "".join(parts)


def humanize(seconds: int) -> str:
    """Describe ``seconds`` in its largest whole unit, e.g. ``"2 hours"`` or ``"1 minute"``."""
    for name, size in _HUMAN_UNITS:
        if seconds >= size:
            amount = seconds // size
            return f"{amount} {name}{'' if amount == 1 else 's'}"
    return f"{seconds} second{'' if seconds == 1 else 's'}"
