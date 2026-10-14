from reqapp.core import Money


def test_add() -> None:
    assert Money(2).add(Money(3)) == Money(5)
