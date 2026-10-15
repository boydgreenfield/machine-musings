import entry_app
from entry_app.cli import greeting


def test_version_comes_from_metadata() -> None:
    assert entry_app.__version__ == "0.3.0"


def test_plugins_are_discovered() -> None:
    assert entry_app.plugins()["upper"]("ab") == "AB"


def test_package_data() -> None:
    assert greeting() == "hello from package data"
