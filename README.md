# LegacyLens

AI-assisted static analysis and documentation tool for legacy **PHP** and **Java** web apps.

> Status: repository scaffold. Modules are placeholders with their intended responsibilities
> documented; implementation comes next.

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

## Setup

```bash
uv sync
uv run legacylens --help
```

Planned commands: `legacylens analyze`, `legacylens build`, `legacylens serve`.

## Docs

- [`docs/ir-spec.md`](docs/ir-spec.md) — IR specification (project deliverable)
- [`docs/architecture.md`](docs/architecture.md) — pipeline and module design

## License

MIT
