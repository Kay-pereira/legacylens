# LegacyLens

AI-assisted static analysis and documentation tool for legacy **PHP** and **Java** web apps.

> Status: early implementation. The IR, SQLite fact store, ingestion scanner, hashing,
> language detection, PHP/Java/JSP/XML parsers, inventory/function metrics (counts, length,
> complexity, nesting), include/import/inheritance/call graphs, entry points/request inputs,
> SQL extraction, the CRUD matrix, side effects, the static publisher (Markdown, Mermaid,
> MkDocs), dead-code detection, business-rule candidates, the aggregate health report, the
> provider-agnostic LLM layer, claim verification, the local Q&A service and the evaluation
> harness are in place. The `legacylenscli` command exposes `analyze`, `build` and `serve`.

## Install

```bash
pip install legacylenscli
# with the optional MkDocs build
pip install "legacylenscli[site]"
# or
uv add legacylenscli
uv add "legacylenscli[site]"
```

The PyPI distribution, import package and installed command are all **`legacylenscli`**.
The name avoids the existing `legacylens` project on PyPI:

```python
import legacylenscli
```

Requires Python 3.12 or 3.13. The installed `legacylenscli` command provides `analyze`, `build`
and `serve`; the local Q&A interface is also available through `python -m legacylenscli.qa`.

## What it does

1. **Ingest** a legacy repository, detecting PHP, Java, JSP and deployment descriptors.
2. **Extract facts** with Tree-sitter into a flat intermediate representation (IR) stored in SQLite:
   files, classes, functions, edges, data access, entry points, request inputs, side effects.
3. **Analyse** the IR: inventory, dependency/call graphs, entry points and their request
   inputs, SQL and CRUD coverage, side effects, complexity metrics, business-rule candidates
   and dead code.
4. **Explain** selected units with an LLM using a budgeted context builder, with responses cached
   locally. Hosted and local (Ollama) providers are supported.
5. **Verify** every generated claim, labelling it `verified`, `inferred` or `unsupported`, with
   citations back to source.
6. **Publish** Markdown documentation, Mermaid diagrams and an MkDocs site from stored facts.
7. **Answer questions** over the generated docs through a local FastAPI service.
8. **Evaluate** the three pipeline configurations (static only, static + LLM, static + LLM +
   verification) against manually prepared ground truth.

## Publishing (static, no AI)

The publisher turns the fact store into a deterministic documentation site. It makes no
model calls: every page is generated from stored IR facts. The generated page set is:

| Page | Contents |
| --- | --- |
| `index.md` | Project totals, page index and scope |
| `claims.md` | Verification-labelled claims and their evidence |
| `architecture.md` | Module dependencies, inheritance, calls and unresolved references |
| `entry-points.md` | Routes, servlets, filters, actions, JSPs and request inputs |
| `database.md` | CRUD coverage for every table named in SQL |
| `side-effects.md` | Filesystem, HTTP, email, process and session effects |
| `hotspots.md` | Function length, complexity and nesting rankings |
| `files.md` | Every scanned file with size and declaration counts |

```python
from legacylenscli.publisher import publish_site

result = publish_site(store, ".legacylens/site", build=True)
print(result.pages, result.built, result.skipped_reason)
```

`publish_site` writes `docs/*.md`, `docs/javascripts/mermaid.js`, `mkdocs.yml` and a
manifest used to remove stale pages. MkDocs is optional: without the `site` extra the
Markdown and configuration are still written and the HTML build is skipped with a reason.
Diagrams are bounded (60 nodes/120 edges for dependencies and inheritance, 50/100 for
calls, 40 tables for CRUD) and the pages state when a diagram was truncated.

## Evaluation

The harness scores the three pipeline configurations against a hand-prepared ground-truth
file. It is a source-checkout tool: `scripts/` and `evaluation/` are not included in the
installed wheel.

| Configuration | LLM | Verification |
| --- | --- | --- |
| `static_only` | no | no |
| `llm_no_verification` | yes | no |
| `full_pipeline` | yes | yes |

```bash
uv run python scripts/run_evaluation.py tests/fixtures/legacy_web
# live LLM configurations are opt-in
uv run python scripts/run_evaluation.py path/to/app \
  --ground-truth evaluation/ground_truth/app.json \
  --provider openai --model gpt-4o-mini --max-targets 20
```

Ground truth lives in `evaluation/ground_truth/<app>.json` and is validated strictly; see
[`evaluation/ground_truth/README.md`](https://github.com/Kay-pereira/legacylens/blob/main/evaluation/ground_truth/README.md).
Each configuration uses an isolated SQLite store, so cached completions and generated claims
cannot leak between runs. Results are written to `evaluation/results/<app>.json`.

Metrics are precision/recall/F1 over entry points, table/operation pairs and business-rule
evidence, plus claim status counts and verification rates. Usage reporting separates
`estimated_cost_usd` (provider cost for every completion, including cache hits) from
`billable_cost_usd` (cache misses only), alongside prompt/completion tokens and cache hit
rate. Without a provider the script runs `static_only` and makes no network calls.

## Asking questions locally

The Q&A service retrieves over two corpora and answers with citations:

- the eight generated documentation pages, rebuilt in memory from the IR at startup, or a
  published Markdown directory passed with `--docs`;
- one structured passage per file, class, function, table, entry point and publishable
  claim, using the same citations the verifier understands.

Ranking is dependency-free BM25 with weighted title/section/citation/body fields, exact
symbol and path boosts, and filters for passage kind and documentation page. Extractive
answers are the offline default. LLM mode uses the cached completion layer and validates
every returned citation against the evidence supplied to the model: a citation outside that
set is reported as unresolved and can never silently become support.

```bash
python -m legacylenscli.qa --db .legacylens/legacylens.db --port 8000
# optional: index a published docs tree and enable hosted synthesis
python -m legacylenscli.qa --db .legacylens/legacylens.db --docs .legacylens/site/docs \
  --provider openai --model gpt-4o-mini
```

Open `http://127.0.0.1:8000`. The interface has kind/page filters, expandable evidence with
matched terms, citation links, copy actions and an explicit status badge:

| Status | Meaning |
| --- | --- |
| `verified` | Every returned citation belongs to the retrieved evidence. |
| `inferred` | Some citations are unresolved, or the model flagged incomplete evidence. |
| `unsupported` | No retrieved evidence answers the question. |

JSON clients can use `GET /health`, `GET /search?q=...&kind=...&page=...` and
`POST /ask` with `{"question": "...", "mode": "extractive", "kinds": ["table"]}`. Static
assets are packaged with the module; the UI loads no CDN resources and binds to
`127.0.0.1` by default. Answers are not persisted as claims.

## Commands

```bash
legacylenscli analyze --root path/to/legacy-app
legacylenscli build   --root path/to/legacy-app
legacylenscli serve   --root path/to/legacy-app --port 8000
```

`analyze` writes the health report, `build` writes the static documentation site and `serve`
analyses the repository before starting the local Q&A interface.

## Layout

```
docs/          IR specification and architecture notes
src/legacylenscli/
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

1. Bump `__version__` in `src/legacylenscli/__init__.py` (the single source of truth) and add a
   `CHANGELOG.md` entry.
2. Merge to `main`, then publish a GitHub release tagged with the same version (for example
   `v0.1.0`).
3. The `release` workflow builds the sdist and wheel, checks them with Twine and publishes to
   PyPI through trusted publishing. Configure the PyPI trusted publisher for repository
   `Kay-pereira/legacylens`, workflow `release.yml`, environment `pypi`.

Do not store PyPI tokens in the repository.

## Docs

- [`docs/ir-spec.md`](https://github.com/Kay-pereira/legacylens/blob/main/docs/ir-spec.md) — IR specification (project deliverable)
- [`docs/architecture.md`](https://github.com/Kay-pereira/legacylens/blob/main/docs/architecture.md) — pipeline and module design

## License

MIT
