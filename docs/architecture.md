# Architecture

Status: early implementation. Ingestion, the PHP/Java parser thin slices, the fact store,
inventory, function metrics and relationship graphs are in place; the remaining analyses
and the publishing pipeline are still scaffolds.

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

## Metric definitions

Function metrics are measured once, while the parser still has the syntax tree, and stored
on the `functions` row:

- **Length** — `FunctionRecord.loc`, the inclusive declaration span in physical lines
  (signature and body, including blank lines and annotations). It is not a non-comment LOC
  count.
- **Cyclomatic complexity** — `1 + decision points`: `if`/`elseif` clauses, loops, `catch`
  clauses, ternary expressions, non-default `switch` cases, non-default `match`/switch arms,
  and logical operators (`&&`, `||`, and PHP `and`/`or`/`xor`/`??`).
- **Maximum nesting** — the deepest chain of control-flow constructs in the body
  (`if`, loops, `switch`, `try`). `else if` ladders are flattened to one level.
- **Distributions** — count, total, min, max, mean, median and a nearest-rank p95 across all
  functions; top-N rankings are deterministic.

Call-graph fan-in/fan-out and the combined hotspot score depend on `analysis/graph.py`;
fan-in/fan-out are available from the graph summaries, the combined hotspot score is not
part of this metric layer yet.

## Relationship graphs

Parsers emit `include`, `require`, `import`, `extends`, `implements`, `uses_trait` and
`call` edges. A parser resolves only what its own file determines (literal include paths
and same-file declarations); `analysis/graph.py` then resolves the stored references
against the whole project and builds three NetworkX `DiGraph` objects:

- **includes** — `include`/`require`/`import` edges between file paths;
- **inheritance** — `extends`/`implements`/`uses_trait` edges between class keys;
- **calls** — `call` edges between function keys, attributed to the file for top-level
  script code.

Resolution is conservative: exactly one compatible target wins, matching within the
declaring language. Short-name fallback applies only to unqualified references and
imports require the exact qualified type name. Ambiguous, dynamic and external references
stay unresolved and appear as `unresolved:<kind>:<reference>` nodes, with the original
expression preserved on the edge and in `RelationshipGraphs.unresolved`.

Parallel edges are aggregated per node pair with `count`, `resolved_count`,
`unresolved_count`, `dangling`, `kinds`, `lines` and `raws`. `RelationshipGraphs.summary()`
reports node/edge counts, dangling targets, strongly-connected cycle groups and
deterministic top-N fan-in/fan-out rankings; `as_dict()` is JSON-friendly.

## Evaluation configurations

- **A — static only:** no LLM; all facts and claims are deterministic.
- **B — static + LLM:** LLM summaries and rule narrative, no verification gate.
- **C — static + LLM + verification:** published claims must resolve; unsupported claims are
  dropped and counted.

Metrics: precision/recall/F1 for entry points, tables and business rules; claim verification
rate; cache hit rate; tokens and cost.
