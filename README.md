# LegacyLens

AI-assisted static analysis and documentation tool for legacy **PHP** and **Java** web apps.

> Status: early implementation. The IR, SQLite fact store, ingestion scanner, hashing,
> language detection and thin-slice PHP and Java parsers (classes, functions, methods and
> constructors with spans) are in place; the wider parser set and pipeline wiring come next.

## Install

```bash
pip install legacylenscli
# or
uv add legacylenscli
```

The PyPI distribution is named **`legacylenscli`** because `legacylens` is already taken on
PyPI. The import package and the installed command are both `legacylens`:

```python
import legacylens
```

Requires Python 3.12 or 3.13. The console command is installed, but the pipeline behind it is
not wired yet.

## What it will do

1. **Ingest** a legacy repository, detecting PHP, Java, JSP and deployment descriptors.
2. **Extract facts** with Tree-sitter into a flat intermediate representation (IR) stored in SQLite:
   files, classes, functions, edges, data access, entry points, side effects.
3. **Analyse** the IR: inventory, dependency/call graphs, entry points, SQL and CRUD coverage,
   side effects, complexity metrics, business-rule candidates and dead code.
4. **Explain** selected units with an LLM using a budgeted context builder, with responses cached
   locally. Hosted and local (Ollama) providers are supported.
5. **Verify** every generated claim, labelling it `verified`, `inferred` or `unsupported`, with
   citations back to source.
6. **Publish** Markdown documentation, Mermaid/Graphviz diagrams and an MkDocs site.
7. **Answer questions** over the generated docs through a local FastAPI service.
8. **Evaluate** the three pipeline configurations (static only, static + LLM, static + LLM +
   verification) against manually prepared ground truth.

## Planned commands

```bash
legacylens analyze --root path/to/legacy-app
legacylens build   --root path/to/legacy-app
legacylens serve   --root path/to/legacy-app --port 8000
```

## Layout

```
docs/          IR specification and architecture notes
src/legacylens/
  ingestion/   repository walker, language detection, content hashing
  ir/          dataclass models, SQLite schema, fact store
  parsers/     Tree-sitter PHP / Java / JSP / XML front-ends
  analysis/    inventory, graphs, entry points, SQL, CRUD, side effects, metrics, rules
  llm/         provider interface, context builder, prompts, response cache
  verification/ claim verification and citation resolution
  publisher/   Markdown, diagrams, templates, MkDocs site
  qa/          retrieval and local FastAPI server
  evaluation/  metrics and pipeline runner
tests/         unit, integration and hand-written PHP/Java fixtures
corpus/        gitignored; scripts fetch sample legacy apps
evaluation/    ground truth and result artefacts
```

## Development

```bash
uv sync
uv run pytest
uv run ruff check src tests
```

Build and validate the distribution locally:

```bash
uv build
uv run twine check dist/*
```

## Releasing

1. Bump `__version__` in `src/legacylens/__init__.py` (the single source of truth) and add a
   `CHANGELOG.md` entry.
2. Merge to `main`, then publish a GitHub release tagged with the same version (for example
   `v0.1.0`).
3. The `release` workflow builds the sdist and wheel, checks them with Twine and publishes to
   PyPI through trusted publishing. Configure the PyPI trusted publisher for repository
   `Kay-pereira/legacylens`, workflow `release.yml`, environment `pypi`.

Do not store PyPI tokens in the repository.

## Docs

- [`docs/ir-spec.md`](docs/ir-spec.md) — IR specification (project deliverable)
- [`docs/architecture.md`](docs/architecture.md) — pipeline and module design

## License

MIT
