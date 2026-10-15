"""The console script."""

from importlib.resources import files


def greeting() -> str:
    """The packaged greeting."""
    return files("entry_app").joinpath("resources/greeting.txt").read_text(encoding="utf-8").strip()


def main() -> None:
    """Print the greeting."""
    print(greeting())
