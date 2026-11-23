"""End-to-end explanation service: context -> prompt -> cached completion -> claims."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from legacylens.ir import ClaimRecord, FactStore

from .cache import complete_cached, prompt_hash
from .claims import parse_claims
from .context_builder import ContextBundle, build_context
from .prompts import DEFAULT_SYSTEM_PROMPT, render_prompt
from .providers.base import Completion, Provider

__all__ = [
    "ExplanationResult",
    "explain_function",
    "explain_target",
]


@dataclass(frozen=True, slots=True)
class ExplanationResult:
    """A generated explanation plus the context and completion that produced it."""

    subject: str
    context: ContextBundle
    completion: Completion
    claims: tuple[ClaimRecord, ...]
    stored: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "subject": self.subject,
            "context": self.context.as_dict(),
            "completion": self.completion.as_dict(),
            "claims": [
                {
                    "kind": claim.kind.value,
                    "text": claim.text,
                    "status": claim.status.value,
                    "citations": list(claim.citations),
                    "origin": claim.origin.value,
                    "model": claim.model,
                    "prompt_hash": claim.prompt_hash,
                }
                for claim in self.claims
            ],
            "stored": self.stored,
        }


def explain_target(
    store: FactStore,
    target: str,
    provider: Provider,
    *,
    source_root: str | Path | None = None,
    prompt_name: str | None = None,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    budget_chars: int = 12_000,
    max_source_lines: int = 120,
    max_related: int = 8,
    include_analysis: bool = True,
    replace_claims: bool = True,
) -> ExplanationResult:
    """Explain a function, class, file or project and store the accepted claims."""

    context = build_context(
        store,
        target,
        source_root=source_root,
        budget_chars=budget_chars,
        max_source_lines=max_source_lines,
        max_related=max_related,
        include_analysis=include_analysis,
    )
    prompt = render_prompt(
        prompt_name or _default_prompt(context.target.kind),
        target=context.target.key,
        context=context.text,
    )
    completion = complete_cached(store, provider, system_prompt, prompt)
    claims = parse_claims(
        completion.text,
        subject=context.target.key,
        model=completion.model or provider.settings.model,
        prompt_hash=prompt_hash(system_prompt, prompt),
        allowed_citations=context.citations,
    )

    stored = False
    if replace_claims and provider.name != "none":
        store.replace_claims(claims, subject=context.target.key)
        stored = True

    return ExplanationResult(
        subject=context.target.key,
        context=context,
        completion=completion,
        claims=claims,
        stored=stored,
    )


def explain_function(
    store: FactStore,
    function_id: str,
    provider: Provider,
    **kwargs: object,
) -> ExplanationResult:
    """Convenience wrapper for function targets."""

    if store.get_function(function_id) is None:
        raise ValueError(f"function not found: {function_id}")
    return explain_target(store, function_id, provider, **kwargs)  # type: ignore[arg-type]


def _default_prompt(kind: str) -> str:
    if kind == "function":
        return "explain_function"
    return "summarize_module"
