"""Claim verification: resolve citations against the IR and, optionally, source.

The verifier never judges prose. It checks the evidence a claim cites:

* ``<path>:<line>`` — the file exists in the IR and the line is within the stored line
  count. When ``source_root`` is supplied, the physical file must exist, the line must be
  within the file and the path must stay inside the root.
* ``<path>`` — the file key exists (and, with ``source_root``, the file is readable).
* ``fn:<path>#<name>`` / ``class:<path>#<name>`` — the derived key exists.
* ``table:<name>`` — at least one stored SQL statement names the table; the citation
  inherits the strongest confidence of those rows.
* ``entry:<kind>:<method-or-dash>:<path>`` — an exact entry-point row exists; the citation
  inherits that row's confidence.

Status policy follows the IR specification §7: all citations resolving to verified evidence
is ``verified``; any inferred evidence or a partially resolved citation set is ``inferred``;
no resolvable citation, a malformed citation or an unresolvable subject is ``unsupported``.
A claim never strengthens beyond the weakest evidence it cites. ``verified`` means *the
citations resolve*, not that the prose is true; unsupported claims stay in the store for
evaluation and are never published.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from legacylenscli.ir import (
    ClaimRecord,
    Confidence,
    FactStore,
    class_id,
    function_id,
    weakest_confidence,
)

from .citations import Citation, CitationError, CitationKind, parse_citation

__all__ = [
    "ClaimVerification",
    "CitationResolution",
    "VerificationReport",
    "resolve_citation",
    "verify_claim",
    "verify_claims",
]

_VIRTUAL_SUBJECTS = frozenset({"project", "*"})


@dataclass(frozen=True, slots=True)
class CitationResolution:
    """The outcome of resolving one citation."""

    citation: str
    parsed: Citation | None
    status: Confidence
    reason: str
    resolved_key: str | None = None
    source_checked: bool = False

    @property
    def resolved(self) -> bool:
        """Whether the citation points at an existing, non-unsupported fact."""

        return self.status is not Confidence.UNSUPPORTED

    @property
    def kind(self) -> CitationKind | None:
        return None if self.parsed is None else self.parsed.kind

    def as_dict(self) -> dict[str, object]:
        return {
            "citation": self.citation,
            "kind": self.kind.value if self.kind else None,
            "status": self.status.value,
            "resolved": self.resolved,
            "reason": self.reason,
            "resolved_key": self.resolved_key,
            "source_checked": self.source_checked,
        }


@dataclass(frozen=True, slots=True)
class ClaimVerification:
    """One claim with its evidence resolutions and final label."""

    claim: ClaimRecord
    status: Confidence
    citations: tuple[CitationResolution, ...]
    subject_resolution: CitationResolution | None = None
    reasons: tuple[str, ...] = ()

    @property
    def previous_status(self) -> Confidence:
        return self.claim.status

    @property
    def changed(self) -> bool:
        return self.status is not self.claim.status

    @property
    def publishable(self) -> bool:
        return self.status is not Confidence.UNSUPPORTED

    def updated(self) -> ClaimRecord:
        """Return the claim with its verified status, ready to persist."""

        return replace(self.claim, status=self.status)

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.claim.id,
            "subject": self.claim.subject,
            "kind": self.claim.kind.value,
            "text": self.claim.text,
            "origin": self.claim.origin.value,
            "model": self.claim.model,
            "previous_status": self.previous_status.value,
            "status": self.status.value,
            "changed": self.changed,
            "publishable": self.publishable,
            "reasons": list(self.reasons),
            "subject_resolution": (
                None if self.subject_resolution is None else self.subject_resolution.as_dict()
            ),
            "citations": [item.as_dict() for item in self.citations],
        }


@dataclass(frozen=True, slots=True)
class VerificationReport:
    """Every stored claim's verification result plus aggregate rates."""

    claims: tuple[ClaimVerification, ...]
    persisted_subjects: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return len(self.claims)

    @property
    def counts(self) -> dict[Confidence, int]:
        result = {label: 0 for label in Confidence}
        for item in self.claims:
            result[item.status] += 1
        return result

    @property
    def publishable(self) -> tuple[ClaimVerification, ...]:
        return tuple(item for item in self.claims if item.publishable)

    @property
    def resolved(self) -> int:
        return sum(1 for item in self.claims if item.publishable)

    @property
    def verification_rate(self) -> float:
        """Share of claims with at least one resolvable citation."""

        return 0.0 if not self.claims else self.resolved / self.total

    @property
    def verified_rate(self) -> float:
        """Share of claims whose evidence is fully verified."""

        if not self.claims:
            return 0.0
        return self.counts[Confidence.VERIFIED] / self.total

    @property
    def source_checked(self) -> bool:
        for item in self.claims:
            if item.subject_resolution is not None and item.subject_resolution.source_checked:
                return True
            if any(citation.source_checked for citation in item.citations):
                return True
        return False

    def as_dict(self) -> dict[str, object]:
        counts = self.counts
        return {
            "total": self.total,
            "counts": {label.value: counts[label] for label in Confidence},
            "resolved": self.resolved,
            "verification_rate": round(self.verification_rate, 4),
            "verified_rate": round(self.verified_rate, 4),
            "source_checked": self.source_checked,
            "persisted_subjects": list(self.persisted_subjects),
            "claims": [item.as_dict() for item in self.claims],
        }


def verify_claim(
    store: FactStore,
    claim: ClaimRecord,
    *,
    source_root: str | Path | None = None,
) -> ClaimVerification:
    """Verify one claim without writing to ``store``."""

    resolver = _Resolver(store, _prepare_source_root(source_root))
    return _verify_claim(claim, resolver)


def resolve_citation(
    store: FactStore,
    citation: str,
    *,
    source_root: str | Path | None = None,
) -> CitationResolution:
    """Resolve one citation without judging or storing a claim.

    This is the same resolution policy used by :func:`verify_claim`, exposed for callers
    such as the Q&A index that need to validate evidence references directly.
    """

    resolver = _Resolver(store, _prepare_source_root(source_root))
    return resolver.resolve(citation)


def verify_claims(
    store: FactStore,
    *,
    source_root: str | Path | None = None,
    persist: bool = True,
) -> VerificationReport:
    """Verify every stored claim and, by default, persist the resolved statuses.

    Only subjects whose status changed are rewritten, through
    :meth:`FactStore.replace_claims`. Rewriting a subject reinserts all of its claims, so
    their texts, citations and timestamps are preserved but their row ids may change.
    """

    resolver = _Resolver(store, _prepare_source_root(source_root))
    claims = store.list_claims()
    verifications = tuple(_verify_claim(claim, resolver) for claim in claims)

    persisted: list[str] = []
    if persist:
        updated_by_subject: dict[str, list[ClaimRecord]] = {}
        changed_subjects: set[str] = set()
        for item in verifications:
            updated_by_subject.setdefault(item.claim.subject, []).append(item.updated())
            if item.changed:
                changed_subjects.add(item.claim.subject)
        for subject in sorted(changed_subjects):
            store.replace_claims(updated_by_subject[subject], subject=subject)
            persisted.append(subject)

    return VerificationReport(claims=verifications, persisted_subjects=tuple(persisted))


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


class _Resolver:
    """Resolve citations against one store, caching source line counts."""

    def __init__(self, store: FactStore, source_root: Path | None) -> None:
        self.store = store
        self.source_root = source_root
        self._source_lines: dict[str, tuple[int | None, str | None]] = {}

    def resolve(self, text: str) -> CitationResolution:
        try:
            citation = parse_citation(text)
        except CitationError as exc:
            return CitationResolution(
                citation=text,
                parsed=None,
                status=Confidence.UNSUPPORTED,
                reason=str(exc),
            )

        if citation.kind is CitationKind.FILE_LINE:
            return self._resolve_file_line(citation)
        if citation.kind is CitationKind.FILE:
            return self._resolve_file(citation)
        if citation.kind is CitationKind.FUNCTION:
            return self._resolve_function(citation)
        if citation.kind is CitationKind.CLASS:
            return self._resolve_class(citation)
        if citation.kind is CitationKind.TABLE:
            return self._resolve_table(citation)
        return self._resolve_entry(citation)

    def _resolve_file_line(self, citation: Citation) -> CitationResolution:
        path = citation.path or ""
        line = citation.line or 0
        file = self.store.get_file(path)
        if file is None:
            return self._failure(citation, f"file {path!r} is not in the IR")
        if line > file.loc:
            return self._failure(
                citation,
                f"line {line} is beyond the stored {file.loc} line(s) of {path!r}",
            )

        source_checked = False
        if self.source_root is not None:
            count, error = self._source_line_count(path)
            if count is None:
                return self._failure(citation, error or "source file is unavailable")
            if line > count:
                return self._failure(
                    citation,
                    f"line {line} is beyond the {count} source line(s) of {path!r}",
                )
            source_checked = True

        where = "the IR and source" if source_checked else "the IR"
        return CitationResolution(
            citation=citation.raw,
            parsed=citation,
            status=Confidence.VERIFIED,
            reason=f"file line resolves against {where}",
            resolved_key=f"{path}:{line}",
            source_checked=source_checked,
        )

    def _resolve_file(self, citation: Citation) -> CitationResolution:
        path = citation.path or ""
        file = self.store.get_file(path)
        if file is None:
            return self._failure(citation, f"file {path!r} is not in the IR")

        source_checked = False
        if self.source_root is not None:
            count, error = self._source_line_count(path)
            if count is None:
                return self._failure(citation, error or "source file is unavailable")
            source_checked = True

        return CitationResolution(
            citation=citation.raw,
            parsed=citation,
            status=Confidence.VERIFIED,
            reason="file key exists in the IR",
            resolved_key=path,
            source_checked=source_checked,
        )

    def _resolve_function(self, citation: Citation) -> CitationResolution:
        key = function_id(citation.path or "", citation.qualified_name or "")
        if self.store.get_function(key) is None:
            return self._failure(citation, f"function {key!r} is not in the IR")
        return CitationResolution(
            citation=citation.raw,
            parsed=citation,
            status=Confidence.VERIFIED,
            reason="function key exists in the IR",
            resolved_key=key,
        )

    def _resolve_class(self, citation: Citation) -> CitationResolution:
        key = class_id(citation.path or "", citation.qualified_name or "")
        if self.store.get_class(key) is None:
            return self._failure(citation, f"class {key!r} is not in the IR")
        return CitationResolution(
            citation=citation.raw,
            parsed=citation,
            status=Confidence.VERIFIED,
            reason="class key exists in the IR",
            resolved_key=key,
        )

    def _resolve_table(self, citation: Citation) -> CitationResolution:
        name = citation.table_name or ""
        rows = self.store.list_data_access(table_name=name)
        if not rows:
            return self._failure(citation, f"no stored SQL statement names {name!r}")
        status = _strongest(row.confidence for row in rows)
        return CitationResolution(
            citation=citation.raw,
            parsed=citation,
            status=status,
            reason=f"{len(rows)} stored statement(s) name {name!r}",
            resolved_key=f"table:{name}",
        )

    def _resolve_entry(self, citation: Citation) -> CitationResolution:
        rows = self.store.list_entry_points(
            kind=citation.entry_kind,
            path=citation.entry_path,
        )
        matches = [row for row in rows if row.http_method == citation.http_method]
        if not matches:
            method = citation.http_method.value if citation.http_method else "-"
            return self._failure(
                citation,
                f"no entry point matches {method} {citation.entry_path!r}",
            )
        status = _strongest(row.confidence for row in matches)
        return CitationResolution(
            citation=citation.raw,
            parsed=citation,
            status=status,
            reason=f"{len(matches)} entry point(s) match",
            resolved_key=matches[0].citation,
        )

    def _failure(self, citation: Citation, reason: str) -> CitationResolution:
        return CitationResolution(
            citation=citation.raw,
            parsed=citation,
            status=Confidence.UNSUPPORTED,
            reason=reason,
        )

    def _source_line_count(self, path: str) -> tuple[int | None, str | None]:
        cached = self._source_lines.get(path)
        if cached is not None:
            return cached

        assert self.source_root is not None
        root = self.source_root
        try:
            target = (root / path).resolve()
            target.relative_to(root)
        except (OSError, ValueError):
            result: tuple[int | None, str | None] = (
                None,
                f"source path {path!r} escapes the source root",
            )
            self._source_lines[path] = result
            return result

        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            result = (None, f"source file {path!r} is missing or unreadable")
            self._source_lines[path] = result
            return result

        result = (len(text.splitlines()), None)
        self._source_lines[path] = result
        return result


def _verify_claim(claim: ClaimRecord, resolver: _Resolver) -> ClaimVerification:
    subject_resolution = (
        None
        if claim.subject.strip() in _VIRTUAL_SUBJECTS
        else resolver.resolve(claim.subject)
    )
    citations = tuple(_unique(claim.citations))
    resolutions = tuple(resolver.resolve(citation) for citation in citations)
    reasons: list[str] = []

    if not resolutions:
        status = Confidence.UNSUPPORTED
        reasons.append("claim has no citations")
    elif any(item.parsed is None for item in resolutions):
        status = Confidence.UNSUPPORTED
        reasons.extend(
            f"malformed citation {item.citation!r}: {item.reason}"
            for item in resolutions
            if item.parsed is None
        )
    else:
        resolved = [item for item in resolutions if item.resolved]
        if not resolved:
            status = Confidence.UNSUPPORTED
            reasons.append("no citation resolves against the IR")
        elif len(resolved) < len(resolutions):
            status = Confidence.INFERRED
            reasons.append("some citations do not resolve against the IR")
        else:
            status = weakest_confidence(*(item.status for item in resolved))
            reasons.append("all citations resolve")

        reasons.extend(
            f"unresolved citation {item.citation!r}: {item.reason}"
            for item in resolutions
            if not item.resolved
        )

    if subject_resolution is not None:
        if not subject_resolution.resolved:
            status = Confidence.UNSUPPORTED
            reasons.append(f"subject does not resolve: {subject_resolution.reason}")
        elif subject_resolution.status is Confidence.INFERRED and status is Confidence.VERIFIED:
            status = Confidence.INFERRED
            reasons.append("subject evidence is inferred")

    return ClaimVerification(
        claim=claim,
        status=status,
        citations=resolutions,
        subject_resolution=subject_resolution,
        reasons=tuple(reasons),
    )


def _strongest(labels: Iterable[Confidence]) -> Confidence:
    values = set(labels)
    if Confidence.VERIFIED in values:
        return Confidence.VERIFIED
    if Confidence.INFERRED in values:
        return Confidence.INFERRED
    return Confidence.UNSUPPORTED


def _unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _prepare_source_root(value: str | Path | None) -> Path | None:
    if value is None:
        return None
    root = Path(value)
    if not root.is_dir():
        raise ValueError(f"source_root is not a directory: {root}")
    return root.resolve()
