import pytest
from fixturepkg.accounts import Account


@pytest.fixture
def account() -> Account:
    return Account("fixture")
