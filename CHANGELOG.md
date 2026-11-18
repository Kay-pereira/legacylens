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
- Tree-sitter Java thin slice using the same parser contract and IR: package-qualified
  class/interface/enum/record declarations, methods, constructors, visibility and
  static/abstract flags. Overloaded methods are disambiguated by declared parameter types
  so unique function keys are preserved without changing the IR.
- End-to-end test covering scanner -> Java parser -> SQLite store for a fixture file.
- Project inventory: file/LOC/byte totals, language mix, class-kind and function counts, and
  parse-error/generated/vendored counters, exposed as JSON-friendly dataclasses.
- Function metrics: parser-measured cyclomatic complexity and maximum nesting, plus
  span-derived function length, summarised as distributions and deterministic top-N
  rankings. Metric definitions are documented in `docs/architecture.md`.
- End-to-end analysis test over the PHP and Java fixture apps.
- Relationship edges from the PHP and Java parsers: imports, includes/requires,
  `extends`/`implements`/`uses_trait` and calls, with same-file resolution and stable
  references for dynamic or cross-file targets.
- Project-level edge resolution and NetworkX include, inheritance and call graphs with
  aggregated parallel edges, explicit `unresolved:<kind>:<reference>` nodes, dangling
  target counts, cycle groups and deterministic fan-in/fan-out summaries.
- Entry points and request inputs across PHP pages/CLI scripts, servlets, filters, JSP
  pages and Struts 1/2 actions, including `web.xml`, `struts-config.xml`, `struts.xml` and
  TLD ingestion. Parsers record a pre-resolution `handler_ref`; `analysis/entrypoints.py`
  follows class lineage, calls and static JSP includes to attribute request inputs to
  handlers, reports unassigned reads and emits a deterministic JSON-ready summary.
- Request-input IR: `RequestInputKind`, `RequestInputRecord`, the `request_inputs` table and
  `FileFacts.request_inputs`. Static keys are `verified`, dynamic keys stay explicit as
  `<dynamic>`, and form-bean-derived fields are `inferred`.
- JSP parser (pages, EL, scriptlets, form fields, page imports, static/dynamic includes) and
  a namespace-tolerant XML descriptor parser built on stdlib Expat with exact line numbers.
- End-to-end web fixture app (`tests/fixtures/legacy_web`) covering PHP, servlets, filters,
  Struts, JSP, `WEB-INF` exclusions, fragments and malformed/namespace-prefixed descriptors.
- SQL extraction shared by the PHP and Java parsers: string and concatenation collection,
  statement splitting, SQLGlot classification with lexical dialect selection, conservative
  regex fallback, table/placeholder normalisation and innermost-function attribution.
  Interpolated or concatenated dynamic fragments stay explicit as `<dynamic>` and are never
  expanded into a table name; drivers are recorded only from PHP/Java file evidence.
- CRUD matrix (`analysis/crud.py`) pivoting `data_access` statement/table rows into a
  deterministic table → operations → functions report with observed CRUD letters, missing
  operations, read-only/write-only tables, unknown-table statements and JSON-ready output.

### Changed

- IR schema version bumped from `1` to `2`; existing databases must be rebuilt.
- `EntryPointRecord` gained `handler_ref`, and `FileFacts` gained `request_inputs`.
- The fixture-tree scanner test now includes the web fixture app (25 files).

## [0.1.0] - 2026-11-14

### Added

- Repository scaffold for the LegacyLens analysis pipeline: ingestion, IR, parsers,
  static analyses, LLM integration, verification, publishing, Q&A and evaluation packages.
- Project configuration, dependency set, test fixtures and documentation skeletons.
- Packaging for PyPI under the distribution name `legacylenscli`, with the `legacylens`
  import package and console command.
