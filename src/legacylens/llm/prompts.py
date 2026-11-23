"""Prompt loading and deterministic ``{{variable}}`` rendering.

Prompt files stay as Markdown so they can be reviewed like code. Only explicit
``{{variable}}`` placeholders are substituted; JSON braces in the output schema are left
untouched.
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = [
    "DEFAULT_SYSTEM_PROMPT",
    "PROMPT_DIR",
    "load_prompt",
    "render_prompt",
]

PROMPT_DIR = Path(__file__).with_name("prompts")
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_PROMPT_NAME = re.compile(r"^[A-Za-z0-9_]+$")

DEFAULT_SYSTEM_PROMPT = (
    "You document legacy PHP and Java web applications. Use only the supplied facts. "
    "Every claim must cite at least one supplied citation. Never invent citations, "
    "behaviour or structure. Return strict JSON with no prose outside the JSON object."
)


def load_prompt(name: str) -> str:
    """Return one prompt template from ``llm/prompts``."""

    if not _PROMPT_NAME.fullmatch(name):
        raise ValueError(f"invalid prompt name: {name!r}")
    path = PROMPT_DIR / f"{name}.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FileNotFoundError(f"prompt not found: {name}") from exc


def render_prompt(name: str, **variables: object) -> str:
    """Render a prompt, replacing every ``{{variable}}`` placeholder exactly once."""

    template = load_prompt(name)

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            raise KeyError(f"prompt {name!r} requires variable {key!r}")
        return str(variables[key])

    rendered = _PLACEHOLDER.sub(replace, template)
    if "{{" in rendered or "}}" in rendered:
        raise ValueError(f"prompt {name!r} contains an unresolved placeholder")
    return rendered
