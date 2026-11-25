"""Claim verification: citation parsing, IR/source resolution and status labels."""

from .citations import Citation, CitationError, CitationKind, parse_citation
from .verifier import (
    CitationResolution,
    ClaimVerification,
    VerificationReport,
    resolve_citation,
    verify_claim,
    verify_claims,
)

__all__ = [
    "Citation",
    "CitationError",
    "CitationKind",
    "ClaimVerification",
    "CitationResolution",
    "VerificationReport",
    "parse_citation",
    "resolve_citation",
    "verify_claim",
    "verify_claims",
]
