# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the project is pre-1.0, minor versions may include breaking changes.

## [Unreleased]

### Added

- Deterministic, streaming repository scanner with legacy PHP/Java/JSP/XML detection,
  binary and size filtering, include/exclude globs, symlink handling and skip diagnostics.
- SHA-256 content hashing with an extractor version folded into the file digest, plus
  physical line counting.
- `scripts/bench_scan.py` for measuring scan throughput and peak memory on real repositories.
- Parser contract (`parsers/base.py`) and a Tree-sitter PHP thin slice that extracts
  class-like declarations and named functions/methods with qualified names, spans,
  visibility and static/abstract flags, while recording recoverable syntax errors on the
  file instead of discarding partial facts.
- End-to-end test covering scanner -> PHP parser -> SQLite store for a fixture file.

## [0.1.0] - 2026-11-14

### Added

- Repository scaffold for the LegacyLens analysis pipeline: ingestion, IR, parsers,
  static analyses, LLM integration, verification, publishing, Q&A and evaluation packages.
- Project configuration, dependency set, test fixtures and documentation skeletons.
- Packaging for PyPI under the distribution name `legacylenscli`, with the `legacylens`
  import package and console command.
