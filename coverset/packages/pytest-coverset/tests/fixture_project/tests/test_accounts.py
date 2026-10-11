import pytest
from fixturepkg.accounts import Account, Money, SavingsAccount


class TestAccount:
    def test_deposit(self, account):
        assert account.deposit(5) == 5

    def test_from_email(self):
        assert Account.from_email("ops@example.com").owner == "example.com"

    def test_savings_receiver(self):
        assert SavingsAccount("s").deposit(7) == 7


def test_money():
    assert Account("m").deposit_money(Money(250)) == 250


@pytest.mark.invariant("balance_never_negative")
def test_negative_deposit():
    assert Account("n").deposit(-3) == -3
