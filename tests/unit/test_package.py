"""Smoke test so the scaffold's test command is green from day one."""

import legacylens


def test_package_imports() -> None:
    assert legacylens.__version__ == "0.1.0"
