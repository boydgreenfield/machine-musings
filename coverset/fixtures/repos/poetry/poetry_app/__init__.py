"""A Poetry-managed package."""


def greet(name: str) -> str:
    """A greeting; blank names are refused."""
    if not name.strip():
        raise ValueError("name must not be blank")
    return f"hello, {name}"
