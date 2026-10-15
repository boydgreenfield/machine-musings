from cfgapp import clamp


def test_clamp() -> None:
    assert clamp(5, 0, 3) == 3
