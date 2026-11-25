"""Smoke test so the scaffold's test command is green from day one."""

import legacylenscli


def test_package_imports() -> None:
    assert legacylenscli.__version__ == "0.1.0"
