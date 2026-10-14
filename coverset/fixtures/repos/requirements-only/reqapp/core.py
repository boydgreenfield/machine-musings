"""Money helpers."""

import attrs


@attrs.frozen
class Money:
    """An amount in cents."""

    cents: int

    def add(self, other: "Money") -> "Money":
        """The sum of two amounts."""
        return Money(self.cents + other.cents)
