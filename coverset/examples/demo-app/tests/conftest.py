"""Shared test configuration: the Hypothesis profile coverset runs the property tests under.

``derandomize=True`` draws the same examples on every run, so the argument shapes the property
tests record (and the example values ``coverset shapes`` prints) are the same in every seeded demo.
"""

from hypothesis import settings

settings.register_profile("coverset", deadline=None, print_blob=True, derandomize=True)
settings.load_profile("coverset")
