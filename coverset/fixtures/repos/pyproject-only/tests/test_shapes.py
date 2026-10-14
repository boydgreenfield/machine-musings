import pytest
from pyproj_app import area


def test_area() -> None:
    assert area(2, 3) == 6


def test_negative() -> None:
    with pytest.raises(ValueError):
        area(-1, 3)
