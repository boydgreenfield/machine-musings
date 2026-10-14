"""Areas."""


def area(width: float, height: float) -> float:
    """Width times height; negative sides are an error."""
    if width < 0 or height < 0:
        raise ValueError("sides must not be negative")
    return width * height
