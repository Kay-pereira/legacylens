"""Structured claim parsing: citations are bounded to the supplied context."""

from __future__ import annotations

import json

from legacylenscli.ir import ClaimKind, ClaimOrigin, Confidence
from legacylenscli.llm import parse_claims

ALLOWED = {"src/User.php:12", "fn:src/User.php#User::find", "table:users"}


def _response(*claims: object, **extra: object) -> str:
    return json.dumps({"summary": "summary", "claims": list(claims), **extra})


def test_claims_are_parsed_and_citations_filtered() -> None:
    response = _response(
        {
            "kind": "behaviour",
            "text": "Reads a user by id.",
            "citations": ["src/User.php:12", "made/up:99"],
        },
        {
            "kind": "not-a-kind",
            "text": "Uses the users table.",
            "citations": ["table:users"],
        },
    )

    claims = parse_claims(
        response,
        subject="fn:src/User.php#User::find",
        model="test-model",
        prompt_hash="abc123",
        allowed_citations=ALLOWED,
    )

    assert len(claims) == 2
    assert claims[0].kind is ClaimKind.BEHAVIOUR
    assert claims[0].text == "Reads a user by id."
    assert claims[0].citations == ("src/User.php:12",)
    assert claims[1].kind is ClaimKind.OTHER
    assert all(claim.status is Confidence.INFERRED for claim in claims)
    assert all(claim.origin is ClaimOrigin.LLM for claim in claims)
    assert claims[0].model == "test-model"
    assert claims[0].prompt_hash == "abc123"


def test_fenced_json_and_citation_string_are_accepted() -> None:
    response = (
        "```json\n"
        + json.dumps(
            {
                "claims": [
                    {
                        "text": "Finds a user.",
                        "citation": "fn:src/User.php#User::find",
                    }
                ]
            }
        )
        + "\n```"
    )

    claims = parse_claims(
        response,
        subject="fn:src/User.php#User::find",
        model=None,
        prompt_hash=None,
        allowed_citations=ALLOWED,
    )

    assert len(claims) == 1
    assert claims[0].kind is ClaimKind.OTHER
    assert claims[0].citations == ("fn:src/User.php#User::find",)


def test_claims_without_allowed_citations_are_dropped() -> None:
    response = _response(
        {"text": "Unsupported.", "citations": ["made/up:99"]},
        {"text": "No citations."},
        {"text": "", "citations": ["table:users"]},
    )

    claims = parse_claims(
        response,
        subject="fn:src/User.php#User::find",
        model=None,
        prompt_hash=None,
        allowed_citations=ALLOWED,
    )

    assert claims == ()


def test_duplicate_claims_are_removed_and_limit_is_respected() -> None:
    item = {"text": "Finds a user.", "citations": ["table:users"]}
    response = _response(item, item, item)

    claims = parse_claims(
        response,
        subject="fn:src/User.php#User::find",
        model=None,
        prompt_hash=None,
        allowed_citations=ALLOWED,
        max_claims=1,
    )

    assert len(claims) == 1


def test_malformed_response_returns_no_claims() -> None:
    claims = parse_claims(
        "not json",
        subject="subject",
        model=None,
        prompt_hash=None,
        allowed_citations=ALLOWED,
    )

    assert claims == ()
