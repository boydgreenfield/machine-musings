"""Split an amount of cents across weighted shares without losing a cent."""


def allocate_cents(total_cents: int, weights: list[float]) -> list[int]:
    """Split ``total_cents`` in proportion to ``weights`` using the largest-remainder method.

    Each share is floored to whole cents, then the cents left over go one at a time to the
    shares with the largest fractional parts, so ``allocate_cents(100, [1, 1, 1])`` is
    ``[34, 33, 33]``.
    """
    total_weight = sum(weights)
    exact = [total_cents * weight / total_weight for weight in weights]
    shares = [int(value) for value in exact]
    remainder = total_cents - sum(shares)
    by_fraction = sorted(range(len(exact)), key=lambda i: exact[i] - shares[i], reverse=True)
    for i in by_fraction[:remainder]:
        shares[i] += 1
    return shares


def split_evenly(total_cents: int, parts: int) -> list[int]:
    """Split ``total_cents`` into ``parts`` shares that differ by at most one cent."""
    return allocate_cents(total_cents, [1.0] * parts)
