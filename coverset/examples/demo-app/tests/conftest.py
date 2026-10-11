"""Shared test configuration: the Hypothesis profile coverset runs the property tests under."""

from hypothesis import settings

settings.register_profile("coverset", deadline=None, print_blob=True)
settings.load_profile("coverset")
