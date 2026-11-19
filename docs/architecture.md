# Architecture

Status: early implementation. Ingestion, the PHP/Java parsers (including JSP and XML
descriptors), the fact store, inventory, function metrics, relationship graphs, the
entry-point/request-input layer, SQL extraction and the CRUD matrix, side-effect detection
and the static publisher are in place; the remaining analyses (business rules and dead
code), LLM integration/verification, Q&A and pipeline wiring are still scaffolds.

## Pipeline

```
source tree
   │  ingestion/scanner.py        walk, filter, language detection
   │  ingestion/hashing.py        content hash -> incremental cache
   ▼
parsers/{php,java}                Tree-sitter -> ParseResult (IR facts)
parsers/java/{jsp,xml_config}.py  JSP pages and web.xml/Struts/TLD descriptors
   │
   ▼
ir/store.py (SQLite)              files, classes, functions, edges, data access,
   │                              entry points, request inputs, side effects, claims
   ├── analysis/inventory.py      counts, sizes, language mix
   ├── analysis/graph.py          include/import/call graphs, unresolved edges
   ├── analysis/entrypoints.py    routes, servlets, actions, JSPs, CLI + inputs
   ├── analysis/sql_extract.py    SQLGlot -> DataAccess
   ├── analysis/crud.py           table x operation matrix
   ├── analysis/side_effects.py   effect classification and risk mix
   ├── analysis/metrics.py        complexity, nesting, fan-in/out, hotspots
   ├── analysis/rules.py          business-rule candidates, dead code
   │
   ├── publisher/ (static)        Markdown + Mermaid + MkDocs from stored facts
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

## Entry points and request inputs

Parsers record entry points where the declaration actually appears, and `request_inputs`
wherever a user-controlled value is read. `analysis/entrypoints.py` joins the two:

- **PHP** — top-level executable/HTML evidence marks a page route; a shebang marks a CLI
  script; declaration- and include-only files are not entry points. `$_GET`/`$_POST`/
  `$_REQUEST`/`$_COOKIE`/`$_FILES` and `filter_input*` become typed inputs; `$_SERVER` maps
  only header, body and path/query keys.
- **Servlets and filters** — `@WebServlet`/`@WebFilter` annotations and `web.xml`
  `<servlet-mapping>`/`<filter-mapping>` entries store a `handler_ref`; the analysis
  resolves it through the class lineage to `doGet`/`doPost`/… or `doFilter`.
- **Struts** — `struts-config.xml` and `struts.xml` actions store the action class or
  `Class::method`; `ActionForm` fields and `<form-property>` entries become inferred form
  inputs.
- **JSP** — pages under the web root are entry points, while `WEB-INF` pages, `.jspf`,
  `.tag` and `.tagx` fragments are not. EL (`param`, `header`, `cookie`), scriptlet request
  calls, static includes and form fields contribute inputs; `jsp-file` mappings in
  `web.xml` make a hidden page reachable through a URL.
- **Attribution** — handler inputs follow project-resolved calls, inheritance and includes
  transitively, bounded by `max_call_depth`/`max_include_depth`. Inputs read outside any
  reachable entry point are reported as unassigned rather than dropped.

Dynamic keys stay explicit as `<dynamic>` with the original expression in `raw`; the
analysis never invents a parameter name. The report exposes deterministic `as_dict()`
output with per-entry handlers and inputs, unassigned inputs and kind counts.

## SQL extraction and the CRUD matrix

Parsers collect SQL-looking string literals and literal concatenations while they still
have the syntax tree, and pass `(text, line)` snippets to `analysis/sql_extract.py`. That
keeps `FileFacts` atomic and gives every statement an exact line and the innermost enclosing
function. The engine:

1. splits multi-statement strings on semicolons outside quotes and comments;
2. rejects prose that merely starts with a keyword (`Select a user from the list`);
3. parses each statement with SQLGlot, ordering dialects by lexical evidence (backticks →
   MySQL, `[...]` → T-SQL, `%s`/`::` → PostgreSQL);
4. accepts only understood statement nodes, and rejects `Command` results or identifiers
   that still contain quote characters;
5. falls back to the leading keyword plus explicit `FROM`/`INTO`/`UPDATE`/`JOIN` clauses
   when SQLGlot cannot classify the statement, marking it `inferred`.

A statement that contains `<dynamic>` (PHP interpolation, a non-literal Java operand) is
never given to SQLGlot: it is always `inferred`, and the dynamic fragment never yields a
table name. Drivers come from file evidence only (PHP `PDO`/`mysqli`/`$wpdb`, Java
`java.sql` → `JDBC`).

Each statement is stored as one `data_access` row per distinct table. The first table keeps
the statement's operation; additional join/subquery tables are recorded as reads. A
statement with no identifiable table gets a single row with `table_name = NULL`.
`analysis/crud.py` pivots those rows into a deterministic table → operations → functions
matrix and reports the CRUD letters a table is missing. There is no schema snapshot yet, so
the matrix reports coverage for tables the code mentions and never invents unreferenced
tables.

## Static publisher

`publisher/markdown_gen.py` renders seven deterministic pages from the analysis reports and
store queries: `index.md`, `architecture.md`, `entry-points.md`, `database.md`,
`side-effects.md`, `hotspots.md` and `files.md`. Every statement comes from the IR; this
milestone publishes no AI-generated content, no per-file pages and no inferred schema
relationships. The database page uses a coverage flowchart rather than an ER diagram
because the IR has no foreign-key facts.

`publisher/diagrams.py` renders Mermaid from the already-resolved graphs and matrices.
Diagrams are budgeted (60 nodes/120 edges for includes and inheritance, 50/100 for calls,
40 tables for CRUD); nodes are ranked by total degree and the page states when a diagram was
truncated. Unresolved references are listed as text instead of being drawn.

`publisher/site.py` writes `docs/*.md`, the Mermaid loader, `mkdocs.yml` and a page manifest
used to remove stale pages. MkDocs is an optional dependency (`site` extra): without it the
Markdown and configuration are still written and the build is skipped with a reason; with
it, `publish_site(..., build=True)` runs `mkdocs build` and raises `SiteBuildError` with the
build tail on failure. Pages contain repository-relative paths, no timestamps and no
environment data, so repeated publishes are byte-stable.

## Evaluation configurations

- **A — static only:** no LLM; all facts and claims are deterministic.
- **B — static + LLM:** LLM summaries and rule narrative, no verification gate.
- **C — static + LLM + verification:** published claims must resolve; unsupported claims are
  dropped and counted.

Metrics: precision/recall/F1 for entry points, tables and business rules; claim verification
rate; cache hit rate; tokens and cost.
