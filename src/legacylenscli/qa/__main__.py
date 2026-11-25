"""Run the local Q&A service: ``python -m legacylenscli.qa``."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from legacylenscli.llm import LLMConfigurationError, LLMSettings

from .server import serve


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m legacylenscli.qa",
        description="Serve the LegacyLens Q&A interface over an analysed fact store.",
    )
    parser.add_argument(
        "--db",
        default=".legacylens/legacylens.db",
        help="path to the LegacyLens SQLite fact store",
    )
    parser.add_argument(
        "--docs",
        default=None,
        help="index a published Markdown directory instead of generating pages",
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind address")
    parser.add_argument("--port", type=int, default=8000, help="bind port")
    parser.add_argument(
        "--provider",
        choices=("none", "openai", "ollama"),
        default=None,
        help="LLM provider for optional answer synthesis",
    )
    parser.add_argument("--model", default=None, help="provider model name")
    parser.add_argument("--base-url", default=None, help="OpenAI-compatible or Ollama base URL")
    parser.add_argument("--temperature", type=float, default=None, help="sampling temperature")
    parser.add_argument("--max-tokens", type=int, default=None, help="completion token limit")
    parser.add_argument("--project", default="LegacyLens", help="project name shown in the UI")
    parser.add_argument(
        "--log-level",
        choices=("critical", "error", "warning", "info", "debug", "trace"),
        default="info",
        help="uvicorn log level",
    )
    args = parser.parse_args(argv)

    overrides = {
        "provider": args.provider,
        "model": args.model,
        "base_url": args.base_url,
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
    }
    try:
        settings = LLMSettings.from_env(
            **{key: value for key, value in overrides.items() if value is not None}
        )
    except LLMConfigurationError as exc:
        parser.error(str(exc))
        return

    try:
        serve(
            args.db,
            host=args.host,
            port=args.port,
            docs_dir=args.docs,
            settings=settings,
            project=args.project,
            log_level=args.log_level,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
