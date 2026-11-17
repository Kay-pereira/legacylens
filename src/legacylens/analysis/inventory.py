"""Codebase inventory: sizes, counts, language mix and declaration totals.

The inventory is a JSON-friendly snapshot of what the IR currently knows. It is built
from stored facts only, so it is cheap to regenerate and reflects exactly the files that
were successfully scanned and parsed.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from legacylens.ir.models import Language
from legacylens.ir.store import FactStore

__all__ = [
    "ClassKindCount",
    "Inventory",
    "LanguageInventory",
    "build_inventory",
]


@dataclass(frozen=True, slots=True)
class LanguageInventory:
    """Per-language totals."""

    language: Language
    files: int
    size_bytes: int
    loc: int

    def as_dict(self) -> dict[str, str | int]:
        return {
            "language": self.language.value,
            "files": self.files,
            "size_bytes": self.size_bytes,
            "loc": self.loc,
        }


@dataclass(frozen=True, slots=True)
class ClassKindCount:
    """Number of classes of one declared kind."""

    kind: str
    count: int

    def as_dict(self) -> dict[str, str | int]:
        return {"kind": self.kind, "count": self.count}


@dataclass(frozen=True, slots=True)
class Inventory:
    """Whole-project counts derived from the fact store."""

    files: int
    size_bytes: int
    loc: int
    classes: int
    functions: int
    methods: int
    top_level_functions: int
    parse_errors: int
    generated_files: int
    vendored_files: int
    languages: tuple[LanguageInventory, ...]
    class_kinds: tuple[ClassKindCount, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "files": self.files,
            "size_bytes": self.size_bytes,
            "loc": self.loc,
            "classes": self.classes,
            "functions": self.functions,
            "methods": self.methods,
            "top_level_functions": self.top_level_functions,
            "parse_errors": self.parse_errors,
            "generated_files": self.generated_files,
            "vendored_files": self.vendored_files,
            "languages": [item.as_dict() for item in self.languages],
            "class_kinds": [item.as_dict() for item in self.class_kinds],
        }


def build_inventory(store: FactStore) -> Inventory:
    """Aggregate file, class and function counts from ``store``."""

    files = store.list_files()
    classes = store.list_classes()
    functions = store.list_functions()

    language_counts = Counter(item.language for item in files)
    language_bytes = Counter({item.language: 0 for item in files})
    language_loc = Counter({item.language: 0 for item in files})
    for item in files:
        language_bytes[item.language] += item.size_bytes
        language_loc[item.language] += item.loc

    class_kind_counts = Counter(item.kind.value for item in classes)
    methods = sum(1 for item in functions if item.class_id is not None)

    return Inventory(
        files=len(files),
        size_bytes=sum(item.size_bytes for item in files),
        loc=sum(item.loc for item in files),
        classes=len(classes),
        functions=len(functions),
        methods=methods,
        top_level_functions=len(functions) - methods,
        parse_errors=sum(1 for item in files if not item.parse_ok),
        generated_files=sum(1 for item in files if item.is_generated),
        vendored_files=sum(1 for item in files if item.is_vendored),
        languages=tuple(
            LanguageInventory(
                language=language,
                files=language_counts[language],
                size_bytes=language_bytes[language],
                loc=language_loc[language],
            )
            for language in sorted(language_counts, key=lambda value: value.value)
        ),
        class_kinds=tuple(
            ClassKindCount(kind=kind, count=count)
            for kind, count in sorted(class_kind_counts.items())
        ),
    )
