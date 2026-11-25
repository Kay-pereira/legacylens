"""Local Q&A: BM25 retrieval, cited answers and a local FastAPI interface."""

from .answers import Answer, AnswerCitation, AnswerEngine
from .retrieval import IndexStats, Passage, PassageKind, RetrievalIndex, SearchHit
from .server import AskRequest, create_app, serve

__all__ = [
    "Answer",
    "AnswerCitation",
    "AnswerEngine",
    "AskRequest",
    "IndexStats",
    "Passage",
    "PassageKind",
    "RetrievalIndex",
    "SearchHit",
    "create_app",
    "serve",
]
