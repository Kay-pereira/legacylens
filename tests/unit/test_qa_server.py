"""Local Q&A service: JSON endpoints, validation and packaged static assets."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Literal

import pytest
from fastapi.testclient import TestClient

from legacylens.ir import FactStore
from legacylens.llm import Completion, LLMSettings, NullProvider
from legacylens.qa import create_app


class FakeProvider:
    """A minimal provider used to exercise the LLM endpoint."""

    name = "openai"

    def __init__(self, payload: object) -> None:
        self.settings = LLMSettings(provider="openai", model="fake-qa-model")
        self.payload = payload

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
        return Completion(
            text=json.dumps(self.payload),
            provider=self.name,
            model=self.settings.model or "",
        )


@pytest.fixture
def client(qa_store: FactStore) -> Iterator[TestClient]:
    app = create_app(store=qa_store)
    with TestClient(app) as test_client:
        yield test_client


def test_health_reports_store_index_and_provider(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["project"] == "LegacyLens"
    assert payload["index"]["passages"] > 0
    assert payload["index"]["docs_source"] == "generated"
    assert payload["provider"]["name"] == "none"
    assert payload["modes"] == ["extractive"]


def test_search_returns_ranked_passages_with_filters(client: TestClient) -> None:
    response = client.get("/search", params={"q": "users", "kind": "table", "limit": 5})

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] >= 1
    assert {item["kind"] for item in payload["passages"]} == {"table"}
    assert payload["passages"][0]["citation"].startswith("table:")


def test_search_rejects_unknown_pages_and_blank_queries(client: TestClient) -> None:
    unknown = client.get("/search", params={"q": "users", "page": "missing.md"})
    blank = client.get("/search", params={"q": "   "})

    assert unknown.status_code == 400
    assert "unknown documentation page" in unknown.json()["detail"]
    assert blank.status_code == 400


def test_ask_returns_a_cited_extractive_answer(client: TestClient) -> None:
    response = client.post(
        "/ask",
        json={"question": "Which table does User::find read?", "mode": "extractive"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "verified"
    assert payload["mode"] == "extractive"
    assert payload["answer"]
    assert payload["citations"]
    assert payload["passages"]
    assert payload["insufficient_evidence"] is False


def test_ask_rejects_llm_mode_without_a_provider(client: TestClient) -> None:
    response = client.post("/ask", json={"question": "users", "mode": "llm"})

    assert response.status_code == 400
    assert "configured provider" in response.json()["detail"]


def test_ask_validates_question_and_page(client: TestClient) -> None:
    blank = client.post("/ask", json={"question": "   "})
    missing_page = client.post("/ask", json={"question": "users", "page": "nope.md"})
    too_long = client.post("/ask", json={"question": "x" * 2_001})
    extra = client.post("/ask", json={"question": "users", "unknown": True})

    assert blank.status_code == 400
    assert missing_page.status_code == 400
    assert too_long.status_code == 422
    assert extra.status_code == 422


def test_ask_can_use_an_llm_provider(qa_store: FactStore) -> None:
    provider = FakeProvider({"answer": "User::find reads users.", "citations": ["table:users"]})
    app = create_app(store=qa_store, provider=provider)
    with TestClient(app) as client:
        response = client.post("/ask", json={"question": "users table", "mode": "llm"})

    assert response.status_code == 200
    assert response.json()["status"] == "verified"
    assert response.json()["provider"] == "openai"


def test_home_and_static_assets_are_served_from_the_package(client: TestClient) -> None:
    home = client.get("/")
    stylesheet = client.get("/static/app.css")
    script = client.get("/static/app.js")

    assert home.status_code == 200
    assert home.headers["content-type"].startswith("text/html")
    assert 'id="ask-form"' in home.text
    assert 'id="evidence-list"' in home.text

    assert stylesheet.status_code == 200
    assert stylesheet.headers["content-type"].startswith("text/css")
    assert "--accent" in stylesheet.text

    assert script.status_code == 200
    assert "text/javascript" in script.headers["content-type"]
    assert "renderEvidence" in script.text


def test_ui_has_no_remote_assets(client: TestClient) -> None:
    home = client.get("/").text

    assert "http://" not in home
    assert "https://" not in home
    assert "cdn" not in home.lower()


def test_favicon_is_empty(client: TestClient) -> None:
    assert client.get("/favicon.ico").status_code == 204


def test_docs_directory_can_replace_generated_pages(qa_store: FactStore, tmp_path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text(
        "# Runbook\n\n## Restart\n\nRestart the billing worker.\n", encoding="utf-8"
    )
    app = create_app(store=qa_store, docs_dir=docs, provider=NullProvider(LLMSettings()))
    with TestClient(app) as client:
        health = client.get("/health").json()
        search = client.get("/search", params={"q": "billing worker"}).json()

    assert health["index"]["docs_source"] == "directory"
    assert health["index"]["docs_pages"] == ["runbook.md"]
    assert search["passages"][0]["citation"] == "docs/runbook.md#restart"
