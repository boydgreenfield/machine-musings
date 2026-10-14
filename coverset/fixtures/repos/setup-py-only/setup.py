"""A setup.py coverset reads with ast: literals and names bound to literals, never executed."""

from setuptools import find_packages, setup

INSTALL_REQUIRES = ["attrs>=23.1"]

setup(
    name="pyapp",
    version="0.1.0",
    packages=find_packages(exclude=["tests"]),
    install_requires=INSTALL_REQUIRES,
    extras_require={"test": ["pytest>=8.0"]},
    python_requires=">=3.12",
)
