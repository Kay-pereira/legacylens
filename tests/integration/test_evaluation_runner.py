"""End-to-end evaluation: ground truth, three configurations, usage and JSON output."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import pytest

from legacylenscli.evaluation import (
    Configuration,
    GroundTruth,
    GroundTruthError,
    UsageMetrics,
    load_ground_truth,
    run_configuration,
    run_evaluation,
)
from legacylenscli.llm import Completion, LLMSettings

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "legacy_web"
GROUND_TRUTH = (
    Path(__file__).resolve().parents[2] / "evaluation" / "ground_truth" / "legacy_web.json"
)
TARGET = "fn:public/inc/helpers.php#search"


class FakeProvider:
    name = "openai"

    def __init__(self, settings: LLMSettings) -> None:
        self.settings = settings
        self.calls = 0

    def complete(
        self,
        system: str,
        prompt: str,
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        top_p: float | None = None,
        seed: int | None = None,
        response_format: Literal["text", "json"] = "text",
        timeout: float | None = None,
    ) -> Completion:
        self.calls += 1
        return Completion(
            text=json.dumps(
                {
                    "summary": "Searches articles.",
                    "claims": [
                        {
                            "kind": "behaviour",
                            "text": "Searches the articles table by title.",
                            "citations": ["public/inc/helpers.php:10"],
                        }
                    ],
                }
            ),
            provider=self.name,
            model="test-model",
            prompt_tokens=100,
            completion_tokens=20,
            cost_usd=0.001,
        )


def _provider() -> tuple[FakeProvider, LLMSettings]:
    settings = LLMSettings(provider="openai", model="test-model", retries=0)
    return FakeProvider(settings), settings


def test_static_configuration_scores_the_fixture_ground_truth(tmp_path: Path) -> None:
    ground_truth = load_ground_truth(GROUND_TRUTH)

    result = run_configuration(
        FIXTURE,
        ground_truth,
        Configuration.STATIC_ONLY,
        db_path=tmp_path / "static.db",
    )

    assert result.metrics.entry_points.missing == ()
    assert result.metrics.entry_points.unexpected == ()
    assert result.metrics.entry_points.precision == 1.0
    assert result.metrics.entry_points.recall == 1.0
    assert result.metrics.tables.missing == ()
    assert result.metrics.tables.unexpected == ()
    assert result.metrics.tables.f1 == 1.0
    assert result.metrics.business_rules.expected == 0
    assert result.metrics.business_rules.actual == 0
    assert result.claims.generated == 0
    assert result.usage.requests == 0
    assert result.facts["entry_points"] == 10
    json.dumps(result.as_dict())


def test_llm_configurations_generate_claims_and_track_cost(tmp_path: Path) -> None:
    ground_truth = load_ground_truth(GROUND_TRUTH)
    provider, _settings = _provider()

    unverified = run_configuration(
        FIXTURE,
        ground_truth,
        Configuration.LLM_NO_VERIFICATION,
        db_path=tmp_path / "llm.db",
        provider=provider,
        targets=[TARGET],
    )

    assert provider.calls == 1
    assert unverified.claims.generated == 1
    assert unverified.claims.inferred == 1
    assert unverified.claims.verification_rate is None
    assert unverified.claims.verified_rate is None
    assert unverified.claims.publishable == 1
    assert unverified.usage.requests == 1
    assert unverified.usage.prompt_tokens == 100
    assert unverified.usage.completion_tokens == 20
    assert unverified.usage.estimated_cost_usd == 0.001
    assert unverified.usage.billable_cost_usd == 0.001

    verified = run_configuration(
        FIXTURE,
        ground_truth,
        Configuration.FULL_PIPELINE,
        db_path=tmp_path / "full.db",
        provider=provider,
        targets=[TARGET],
    )

    assert provider.calls == 2
    assert verified.claims.generated == 1
    assert verified.claims.verified == 1
    assert verified.claims.verification_rate == 1.0
    assert verified.claims.verified_rate == 1.0
    assert verified.claims.publishable == 1
    assert verified.usage.requests == 1
    assert verified.usage.estimated_cost_usd == 0.001


def test_run_evaluation_writes_json_and_isolates_stores(tmp_path: Path) -> None:
    ground_truth = load_ground_truth(GROUND_TRUTH)
    work = tmp_path / "work"
    output = tmp_path / "results" / "legacy_web.json"

    suite = run_evaluation(
        FIXTURE,
        ground_truth,
        configurations=[Configuration.STATIC_ONLY],
        work_dir=work,
        output_path=output,
    )

    assert [result.configuration for result in suite.results] == [
        Configuration.STATIC_ONLY
    ]
    assert (work / "static_only.db").is_file()
    assert suite.usage == UsageMetrics()
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["app"] == "legacy_web"
    assert written["results"][0]["configuration"] == "static_only"
    assert written["results"][0]["metrics"]["entry_points"]["f1"] == 1.0


def test_run_evaluation_runs_all_three_configurations(tmp_path: Path) -> None:
    ground_truth = load_ground_truth(GROUND_TRUTH)
    provider, _settings = _provider()

    suite = run_evaluation(
        FIXTURE,
        ground_truth,
        provider=provider,
        targets=[TARGET],
        work_dir=tmp_path / "work",
    )

    assert [result.configuration for result in suite.results] == [
        Configuration.STATIC_ONLY,
        Configuration.LLM_NO_VERIFICATION,
        Configuration.FULL_PIPELINE,
    ]
    assert suite.usage.requests == 2
    assert provider.calls == 2
    assert (tmp_path / "work" / "llm_no_verification.db").is_file()
    assert (tmp_path / "work" / "full_pipeline.db").is_file()


def test_llm_configuration_requires_a_provider(tmp_path: Path) -> None:
    ground_truth = load_ground_truth(GROUND_TRUTH)

    with pytest.raises(ValueError, match="requires a configured LLM provider"):
        run_configuration(
            FIXTURE,
            ground_truth,
            Configuration.FULL_PIPELINE,
            db_path=tmp_path / "no-provider.db",
        )


def test_static_configuration_rejects_a_provider(tmp_path: Path) -> None:
    ground_truth = load_ground_truth(GROUND_TRUTH)
    provider, _settings = _provider()

    with pytest.raises(ValueError, match="does not accept an LLM provider"):
        run_configuration(
            FIXTURE,
            ground_truth,
            Configuration.STATIC_ONLY,
            db_path=tmp_path / "static-provider.db",
            provider=provider,
        )


def test_ground_truth_rejects_unknown_or_invalid_fields() -> None:
    with pytest.raises(GroundTruthError, match="unknown ground-truth fields"):
        GroundTruth.from_dict({"app": "app", "claims": []})

    with pytest.raises(GroundTruthError, match="unknown operations"):
        GroundTruth.from_dict(
            {"app": "app", "tables": [{"name": "users", "operations": ["SELECT", "READ"]}]}
        )

    with pytest.raises(GroundTruthError, match="duplicate name"):
        GroundTruth.from_dict(
            {
                "app": "app",
                "tables": [
                    {"name": "users", "operations": ["SELECT"]},
                    {"name": "Users", "operations": ["INSERT"]},
                ],
            }
        )
