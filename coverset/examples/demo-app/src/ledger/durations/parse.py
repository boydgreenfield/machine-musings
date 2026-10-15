"""Parse compact duration strings such as ``90s``, ``15m`` or ``1h30m`` into seconds."""

import re

_TOKEN = re.compile(r"(\d+)([smhd])")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3_600, "d": 86_400}


def parse_duration(text: str) -> int:
    """Return the number of seconds in ``text``, a run of ``<amount><unit>`` tokens.

    Units are ``s``, ``m``, ``h`` and ``d``; tokens add up, so ``"1h30m"`` is 5,400 seconds.
    Raises ValueError when ``text`` contains no token at all.
    """
    tokens = _TOKEN.findall(text)
    if not tokens:
        raise ValueError(f"not a duration: {text!r}")
    return sum(int(amount) * _UNIT_SECONDS[unit] for amount, unit in tokens)
