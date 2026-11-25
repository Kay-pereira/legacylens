"""Local FastAPI Q&A service and its static interface.

The service is deliberately local-first: it binds to ``127.0.0.1`` by default, makes no
outbound calls unless an LLM provider is configured, and serves a self-contained HTML
interface with no CDN assets. JSON endpoints are available for scripted use.
"""

from __future__ import annotations

import importlib.resources
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from legacylens import __version__
from legacylens.ir import FactStore
from legacylens.llm import LLMSettings, NullProvider, Provider, ProviderError, build_provider
from legacylens.publisher import Documentation

from .answers import AnswerEngine
from .retrieval import PassageKind, RetrievalIndex

__all__ = [
    "AskRequest",
    "create_app",
    "serve",
]


class AskRequest(BaseModel):
    """The JSON body accepted by ``POST /ask``."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2_000)
    mode: Literal["extractive", "llm"] = "extractive"
    limit: int = Field(default=8, ge=1, le=20)
    kinds: list[PassageKind] = Field(default_factory=list)
    page: str | None = Field(default=None, max_length=200)


def create_app(
    *,
    store: FactStore,
    index: RetrievalIndex | None = None,
    provider: Provider | None = None,
    docs: Documentation | None = None,
    docs_dir: str | Path | None = None,
    project: str = "LegacyLens",
) -> FastAPI:
    """Build the local Q&A application around an open fact store.

    FastAPI dispatches synchronous endpoints from a worker pool, so a store passed here
    should be opened with ``check_same_thread=False`` (as :func:`serve` does). The index is
    read-only after construction and can be shared safely.
    """

    resolved_index = index or RetrievalIndex.build(
        store,
        docs=docs,
        docs_dir=docs_dir,
        project=project,
    )
    resolved_provider = provider or NullProvider()
    engine = AnswerEngine(store=store, index=resolved_index, provider=resolved_provider)

    app = FastAPI(
        title="LegacyLens Q&A",
        version=__version__,
        docs_url=None,
        redoc_url=None,
    )
    app.state.store = store
    app.state.index = resolved_index
    app.state.engine = engine
    app.state.provider = resolved_provider

    @app.get("/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "project": project,
            "version": __version__,
            "store": store.summary().as_dict(),
            "index": resolved_index.stats().as_dict(),
            "provider": {
                "name": resolved_provider.name,
                "model": resolved_provider.settings.model,
            },
            "modes": ["extractive"]
            + (["llm"] if resolved_provider.name != "none" else []),
        }

    @app.get("/search")
    def search(
        q: Annotated[str, Query(min_length=1, max_length=2_000)],
        limit: Annotated[int, Query(ge=1, le=50)] = 10,
        kind: Annotated[list[PassageKind] | None, Query()] = None,
        page: Annotated[str | None, Query(max_length=200)] = None,
    ) -> dict[str, object]:
        _require_query(q)
        _validate_page(page, resolved_index)
        hits = resolved_index.search(q, limit=limit, kinds=kind, page=page)
        return {
            "query": q,
            "filters": {
                "kinds": [item.value for item in kind] if kind else [],
                "page": page,
            },
            "count": len(hits),
            "passages": [hit.as_dict() for hit in hits],
        }

    @app.post("/ask")
    def ask(request: AskRequest) -> dict[str, object]:
        _require_query(request.question)
        _validate_page(request.page, resolved_index)
        if request.mode == "llm" and resolved_provider.name == "none":
            raise HTTPException(
                status_code=400,
                detail="LLM mode requires a configured provider.",
            )
        try:
            answer = engine.ask(
                request.question,
                mode=request.mode,
                limit=request.limit,
                kinds=request.kinds or None,
                page=request.page,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ProviderError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"LLM provider request failed ({type(exc).__name__}).",
            ) from exc
        return answer.as_dict()

    @app.get("/", response_class=HTMLResponse)
    def home() -> HTMLResponse:
        return HTMLResponse(
            _asset("index.html"),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/static/app.css")
    def stylesheet() -> Response:
        return Response(
            _asset("app.css"),
            media_type="text/css",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/static/app.js")
    def script() -> Response:
        return Response(
            _asset("app.js"),
            media_type="text/javascript",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return Response(status_code=204)

    return app


def serve(
    db_path: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    docs_dir: str | Path | None = None,
    settings: LLMSettings | None = None,
    project: str = "LegacyLens",
    log_level: str = "info",
) -> None:
    """Open a fact store, build the index and run the local server."""

    if not 0 < port < 65_536:
        raise ValueError("port must be between 1 and 65535")

    store = FactStore(db_path, check_same_thread=False)
    try:
        provider = build_provider(settings or LLMSettings())
        index = RetrievalIndex.build(store, docs_dir=docs_dir, project=project)
        app = create_app(store=store, index=index, provider=provider, project=project)
        print(
            f"LegacyLens Q&A: http://{host}:{port} "
            f"({index.stats().passages} passages, provider={provider.name})"
        )
        uvicorn.run(app, host=host, port=port, log_level=log_level)
    finally:
        store.close()


def _require_query(value: str) -> None:
    if not value.strip():
        raise HTTPException(status_code=400, detail="question must not be blank")


def _validate_page(page: str | None, index: RetrievalIndex) -> None:
    if page is not None and page not in index.docs_pages:
        raise HTTPException(
            status_code=400,
            detail=f"unknown documentation page: {page}",
        )


@lru_cache(maxsize=8)
def _asset(name: str) -> str:
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError(f"invalid asset name: {name!r}")
    resource = importlib.resources.files("legacylens.qa").joinpath("static", name)
    return resource.read_text(encoding="utf-8")
