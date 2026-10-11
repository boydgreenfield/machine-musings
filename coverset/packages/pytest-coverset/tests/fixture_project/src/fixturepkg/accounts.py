"""A class with methods, a subclass and a dataclass."""

from __future__ import annotations

from dataclasses import dataclass

from fixturepkg.core import validate_email


@dataclass(frozen=True)
class Money:
    """An amount in cents."""

    cents: int


class Account:
    """A bank account."""

    def __init__(self, owner: str, balance: int = 0) -> None:
        self.owner = owner
        self.balance = balance

    def deposit(self, amount: int) -> int:
        """Add ``amount`` and return the new balance."""
        self.balance += amount
        return self.balance

    def deposit_money(self, money: Money) -> int:
        """Add a :class:`Money` amount."""
        return self.deposit(money.cents)

    @classmethod
    def from_email(cls, email: str) -> Account:
        """Open an account owned by the domain of ``email``."""
        return cls(owner=validate_email(email))


class SavingsAccount(Account):
    """An account subclass, so receivers record the concrete class."""
