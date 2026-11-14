"""Packaging invariants: version single-sourcing and shipped package data."""

import re
from importlib import resources
from importlib.metadata import PackageNotFoundError, version

import pytest

import legacylens


def test_version_is_declared() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:[.\-+].+)?", legacylens.__version__)


def test_installed_metadata_matches_module_version() -> None:
    try:
        installed = version("legacylenscli")
    except PackageNotFoundError:  # running from a bare source tree
        pytest.skip("distribution is not installed")
    assert installed == legacylens.__version__


@pytest.mark.parametrize(
    "relative_path",
    [
        "ir/schema.sql",
        "llm/prompts/explain_function.md",
        "llm/prompts/qa.md",
        "llm/prompts/summarize_module.md",
        "publisher/templates/index.md.j2",
        "publisher/templates/page.md.j2",
        "py.typed",
    ],
)
def test_package_data_is_shipped(relative_path: str) -> None:
    assert (resources.files("legacylens") / relative_path).is_file()
