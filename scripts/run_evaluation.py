"""Run the evaluation configurations against a hand-prepared ground-truth file.

Each configuration uses its own SQLite store under a temporary work directory, so cached
completions and generated claims never leak between runs. With no provider configured only
the static-only configuration is allowed; live LLM calls are opt-in.

Usage:
    uv run python scripts/run_evaluation.py tests/fixtures/legacy_web
    uv run python scripts/run_evaluation.py path/to/app --ground-truth evaluation/ground_truth/app.json \
        --provider openai --model gpt-4o-mini --configurations llm full
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from legacylenscli.evaluation import (
    Configuration,
    GroundTruthError,
    load_ground_truth,
    run_evaluation,
)
from legacylenscli.llm import LLMConfigurationError, LLMSettings, build_provider

CONFIGURATIONS = {
    "static": Configuration.STATIC_ONLY,
    "llm": Configuration.LLM_NO_VERIFICATION,
    "full": Configuration.FULL_PIPELINE,
}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="repository root to evaluate")
    parser.add_argument(
        "--ground-truth",
        type=Path,
        help="ground-truth JSON (default: evaluation/ground_truth/<root name>.json)",
    )
    parser.add_argument(
        "--configurations",
        nargs="+",
        choices=["all", *CONFIGURATIONS],
        default=["all"],
        help="configurations to run (default: all; static only without a provider)",
    )
    parser.add_argument(
        "--provider",
        choices=["none", "openai", "ollama"],
        help="LLM provider; defaults to LEGACYLENS_* environment variables",
    )
    parser.add_argument("--model", help="LLM model name")
    parser.add_argument("--base-url", help="OpenAI-compatible or Ollama base URL")
    parser.add_argument(
        "--target",
        action="append",
        dest="targets",
        help="unit to explain for the LLM configurations (repeatable)",
    )
    parser.add_argument(
        "--max-targets",
        type=int,
        default=20,
        help="maximum functions to explain when --target is omitted (default: 20)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="JSON result path (default: evaluation/results/<app>.json)",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        help="directory for per-configuration SQLite stores (default: temporary)",
    )
    args = parser.parse_args(argv)

    root = args.root.resolve()
    if not root.is_dir():
        parser.error(f"not a directory: {root}")
    if args.max_targets < 0:
        parser.error("--max-targets must be >= 0")

    ground_truth_path = args.ground_truth or (
        Path("evaluation") / "ground_truth" / f"{root.name}.json"
    )
    try:
        ground_truth = load_ground_truth(ground_truth_path)
    except GroundTruthError as exc:
        parser.error(str(exc))

    overrides = {
        key: value
        for key, value in (
            ("provider", args.provider),
            ("model", args.model),
            ("base_url", args.base_url),
        )
        if value is not None
    }
    try:
        settings = LLMSettings.from_env(**overrides)
    except LLMConfigurationError as exc:
        parser.error(str(exc))

    requested = (
        list(CONFIGURATIONS.values())
        if "all" in args.configurations
        else [CONFIGURATIONS[name] for name in args.configurations]
    )
    llm_requested = any(item is not Configuration.STATIC_ONLY for item in requested)
    if settings.provider == "none":
        if llm_requested and "all" not in args.configurations:
            parser.error(
                "LLM configurations need a provider; pass --provider/--model or set "
                "LEGACYLENS_LLM_PROVIDER and LEGACYLENS_LLM_MODEL"
            )
        requested = [Configuration.STATIC_ONLY]

    provider = build_provider(settings)
    output_path = args.output or Path("evaluation") / "results" / f"{ground_truth.app}.json"
    try:
        suite = run_evaluation(
            root,
            ground_truth,
            configurations=requested,
            provider=provider,
            targets=args.targets,
            max_targets=args.max_targets,
            work_dir=args.work_dir,
            output_path=output_path,
        )
    except (LLMConfigurationError, ValueError) as exc:
        parser.error(str(exc))

    print(f"Evaluation results written to {output_path}")
    for result in suite.results:
        metrics = result.metrics
        print(
            f"  {result.configuration.value:22} "
            f"entries P/R/F1={metrics.entry_points.precision:.2f}/"
            f"{metrics.entry_points.recall:.2f}/{metrics.entry_points.f1:.2f}  "
            f"tables P/R/F1={metrics.tables.precision:.2f}/"
            f"{metrics.tables.recall:.2f}/{metrics.tables.f1:.2f}  "
            f"rules P/R/F1={metrics.business_rules.precision:.2f}/"
            f"{metrics.business_rules.recall:.2f}/{metrics.business_rules.f1:.2f}  "
            f"claims={result.claims.generated}  "
            f"cost=${result.usage.estimated_cost_usd:.6f}"
        )
    print(json.dumps(suite.usage.as_dict(), indent=2))


if __name__ == "__main__":
    main()
