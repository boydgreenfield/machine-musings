import pytest
from fixturepkg import core
from fixturepkg.full import hot_full
from hypothesis import given, settings
from hypothesis import strategies as st


@pytest.mark.invariant("emails_have_domain")
@pytest.mark.parametrize(
    "email", ["alice@example.com", "bob@eng.acme.com", "carol@example.co.uk", "d+tag@x.io"]
)
def test_validate_email(email):
    assert "." in core.validate_email(email)


def test_signup():
    assert core.signup("eve@example.com") == "example.com"


@pytest.mark.parametrize("text", ["1h30m", "100ms", "1.5h", "-5m", "1hx"])
def test_parse_duration(text):
    assert core.parse_duration(text) >= 0


def test_total():
    assert core.total([3, -1, 2]) == 4
    assert core.total([0.0, 0.0]) == 0


def test_merge_intervals():
    assert core.merge_intervals([(1, 3), (8, 10), (2, 6)]) == [(1, 6), (8, 10)]
    assert core.merge_intervals([(5, 1)]) == [(5, 1)]


@settings(derandomize=True, deadline=None, database=None)
@given(st.floats())
def test_scale_floats(x):
    assert isinstance(core.scale(x), float)


def test_describe():
    assert core.describe(1, "a", k=2) == "2 args, 1 kwargs"


def test_double():
    assert core.double(3) == 6


def test_countdown():
    assert list(core.countdown(3)) == [3, 2, 1]


def test_hot():
    for i in range(300):
        core.hot(i % 3)


def test_hot_full():
    for i in range(300):
        hot_full(i % 3)


def test_login():
    assert core.login("alice", "hunter2hunter2")
