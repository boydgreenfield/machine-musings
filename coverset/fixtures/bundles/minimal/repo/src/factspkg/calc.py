"""A tiny throwaway module measured by the facts fixture bundle."""


def add(a, b):
    return a + b


def classify(n):
    """Return the sign of ``n`` as a word."""
    if n < 0:
        return "neg"
    if n == 0:
        return "zero"
    return "pos"


def never_called(x):
    return x * 2


def excluded():  # pragma: no cover
    return "excluded"


class Box:
    def __init__(self, items):
        self.items = list(items)

    def total(self):
        total = 0
        for item in self.items:
            total += classify(item) and item
        return total
