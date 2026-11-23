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
- Side-effect detection across PHP and Java: file, email, network, process and session sinks
  classified by the analysis module, attributed to the innermost function and reported grouped
  by kind, with dynamic/ambiguous receivers left unreported. JSP scriptlets detect session
  writes and process execution.
  Interpolated or concatenated dynamic fragments stay explicit as `<dynamic>` and are never
  expanded into a table name; drivers are recorded only from PHP/Java file evidence.
- CRUD matrix (`analysis/crud.py`) pivoting `data_access` statement/table rows into a
  deterministic table → operations → functions report with observed CRUD letters, missing
  operations, read-only/write-only tables, unknown-table statements and JSON-ready output.
- Static publisher: deterministic Markdown pages for inventory, architecture, entry points,
  CRUD coverage, side effects, hotspots and files, with bounded Mermaid diagrams rendered
  from the resolved analyses.
- `publisher/site.py` writes the docs tree, Mermaid loader, `mkdocs.yml` and a page manifest,
  removes stale pages and optionally runs `mkdocs build` when the `site` extra is installed.
  Pages contain no AI-generated content, timestamps or absolute paths.
- Dead-code detection (`analysis/rules.py`) following entry points through project-resolved
  include and call graphs, with conservative exclusions for generated/vendored files, API
  surface, constructors, magic methods, overrides and unresolved callers, plus aggregated
  exclusion reasons and a `skipped_reason` when no entry point exists.
- Deterministic business-rule candidates from PHP/Java conditions, validation and
  authorisation calls, IR annotations and explicit entry-point auth, each with exact
  evidence, citations and confidence labels.
- Aggregate health report (`analysis/health.py`) combining every deterministic analysis into
  stable findings with configurable complexity, length, nesting and parse-error thresholds,
  an overall `ok`/`attention`/`critical` status and JSON-ready output. Heuristics and
  thresholds are documented in `docs/architecture.md`.
- Claim verification (`verification/verifier.py`) parsing the citation grammar, resolving
  citations against the IR and source, assigning `verified`, `inferred` or `unsupported`,
  persisting statuses and reporting verification rates.
- Published `claims.md` page with verification badges and an index verification summary.
  Unsupported claims are retained in the store but excluded from publication.
- Provider-agnostic LLM layer: a shared `Provider`/`Completion` interface, OpenAI-compatible
  hosted adapter, Ollama adapter, no-op provider, environment-backed `LLMSettings` with
  redacted API keys, typed errors, retries and per-model cost accounting.
- Budgeted, citation-first context builder for functions, classes, files and projects,
  deterministic `{{variable}}` prompt rendering and strict JSON claim parsing that drops
  citations not present in the supplied context.
- Persistent completion cache keyed by provider/model/endpoint/sampling/prompt, with hit,
  token and cost accounting, plus an explanation service that stores accepted claims as
  `origin=llm` and `status=inferred`.

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
