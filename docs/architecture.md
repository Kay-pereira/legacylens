# Architecture

Status: scaffold. This note records the intended pipeline and module boundaries.

## Pipeline

```
source tree
   │  ingestion/scanner.py        walk, filter, language detection
   │  ingestion/hashing.py        content hash -> incremental cache
   ▼
parsers/{php,java}                Tree-sitter -> ParseResult (IR facts)
   │
   ▼
ir/store.py (SQLite)              files, classes, functions, edges, data access,
   │                              entry points, side effects, claims
   ├── analysis/inventory.py      counts, sizes, language mix
   ├── analysis/graph.py          include/import/call graphs, unresolved edges
   ├── analysis/entrypoints.py    routes, servlets, actions, JSPs, CLI
   ├── analysis/sql_extract.py    SQLGlot -> DataAccess
   ├── analysis/crud.py           table x operation matrix
   ├── analysis/side_effects.py   effect classification and risk mix
   ├── analysis/metrics.py        complexity, nesting, fan-in/out, hotspots
   └── analysis/rules.py          business-rule candidates, dead code
   │
   ▼
llm/context_builder.py + prompts     budgeted, citation-tagged context
llm/providers/{hosted,local}.py      OpenAI-compatible or Ollama
llm/cache.py                         persistent completion cache
   │
   ▼
verification/verifier.py             verified / inferred / unsupported
   │
   ▼
publisher/markdown_gen.py + diagrams.py + templates/
publisher/site.py                    mkdocs.yml + build
   │
   ├── qa/retrieval.py + server.py    local search and grounded Q&A
   └── evaluation/runner.py           three configurations vs ground truth
```

## Design decisions

1. **Facts before prose.** Parsers and analyses produce typed, citable facts; the LLM never
   invents structure. Publishing refuses unsupported claims.
2. **SQLite as the contract.** Every stage communicates through the store, so stages can be
   run, cached and evaluated independently.
3. **Partial success is normal.** Parse errors, unresolved includes and unsupported files are
   recorded rather than aborting the run.
4. **LLM optional.** With `provider="none"` the pipeline still produces a complete static
   inventory, graph, data model and hotspot report.
5. **Incremental by content hash.** File hashes gate re-parsing; prompt hashes gate reuse of
   completions.

## Evaluation configurations

- **A — static only:** no LLM; all facts and claims are deterministic.
- **B — static + LLM:** LLM summaries and rule narrative, no verification gate.
- **C — static + LLM + verification:** published claims must resolve; unsupported claims are
  dropped and counted.

Metrics: precision/recall/F1 for entry points, tables and business rules; claim verification
rate; cache hit rate; tokens and cost.
