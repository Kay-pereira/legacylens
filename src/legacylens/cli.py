"""Thin CLI wrapper exposing ``analyze``, ``build`` and ``serve``.

TODO: Typer app with the three commands, delegating to the pipeline, publisher and QA server.
Keep argument parsing here; no analysis logic belongs in this module.
"""

from __future__ import annotations


def main() -> None:
    """Console-script placeholder until the pipeline exists."""
    raise SystemExit("LegacyLens is a scaffold: the analysis pipeline is not implemented yet.")
