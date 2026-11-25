# Architecture

Status: early implementation. Ingestion, the PHP/Java parsers (including JSP and XML
descriptors), the fact store, inventory, function metrics, relationship graphs, the
entry-point/request-input layer, SQL extraction, the CRUD matrix, side effects, the static
publisher, dead-code detection, business-rule candidates, the aggregate health report, the
optional LLM layer, claim verification, the local Q&A service and the evaluation harness are
in place; pipeline wiring is still a scaffold.

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

## Dead-code detection

`analysis/rules.py` reports functions that the project-resolved call graph cannot reach from
any entry point. Both passes are conservative:

1. **Roots** — every entry-point file, plus every resolved handler and explicit
   `function_id`, is expanded through the include graph.
2. **Reachability** — the resulting set is expanded through the call graph. Unresolved
   sentinel nodes are never traversed.

Only functions outside that closure are considered. The following are excluded because they
are API or framework surface rather than unreachable code:

- generated and vendored files (unless explicitly included);
- abstract methods and members of interfaces/traits;
- constructors, magic methods and `main`;
- methods annotated `@Override`;
- public and protected methods.

A function whose name appears in an unresolved call reference is also suppressed, because
the missing edge may be the only reason it looks unreachable. Exclusions are aggregated by
reason so the report shows what the heuristic left out. When the store has no entry points,
reachability cannot be established: the report is empty with `skipped_reason` instead of
flagging the whole codebase. Every remaining candidate carries a citation, the incoming
resolved callers (if any) and an `inferred` confidence.

## Business-rule candidates

Candidates are deterministic evidence, not generated prose. `analysis/rules.py` mines four
sources:

- **conditions** — `if` / `elseif` / `while` expressions in PHP and Java source, classified
  as authorisation, threshold (numeric comparison) or validation (type/format checks), in
  that priority order;
- **calls** — known validation or authorisation APIs outside a condition (`validate`,
  `filter_var`, `is_numeric`, `check_permission`, `has_role`, …);
- **annotations** — validation (`@NotNull`, `@Size`, `@Pattern`, …) and authorisation
  (`@PreAuthorize`, `@RolesAllowed`, `@Secured`, …) annotations already stored in the IR;
- **entry points** — an entry point whose `requires_auth` is explicitly `true`.

Comments and string literals are masked before scanning, so matches come from executable
text. Source is re-read through the repository-relative IR paths only when `source_root` is
provided and only for PHP and Java files; without it the analysis reports IR-only evidence.
Missing, generated and vendored files are counted separately.

Candidates are de-duplicated and sorted by file, line, kind and evidence. Each keeps the
exact expression, a citation and a confidence label. Interpretation stays `inferred` even
when the expression is directly observed; only an explicit entry-point auth mapping keeps
its stored label.

## Health report

`analysis/health.py` runs inventory, metrics, graphs, entry points, CRUD, side effects,
dead code and business rules, then converts their counts into a small set of stable findings.
The defaults are:

| Finding | Trigger | Severity |
| --- | --- | --- |
| `parse-errors` | any file with a parse error | `critical` at ≥ 25% of files, else `warning` |
| `unresolved-references` | any unresolved edge | `warning` at ≥ 10% of edges, else `info` |
| `dead-code` | any dead-code candidate | `warning` |
| `dead-code-skipped` | no entry points | `info` |
| `high-complexity` | complexity ≥ 10 | `warning` |
| `long-functions` | span ≥ 80 lines | `info` |
| `deep-nesting` | nesting ≥ 4 | `info` |
| `crud-gaps` | missing CRUD operations | `info` |
| `auth-evidence` | entry points without auth evidence | `info` |
| `external-effects` | any side effect | `info` |
| `business-rule-candidates` | any candidate | `info` |

Thresholds are explicit keyword arguments, so the same store and settings always produce the
same report. The status is the highest severity present: `critical` for a critical finding,
`attention` for a warning, otherwise `ok`. The report embeds the underlying analyses and is
JSON-ready via `as_dict()`.

## Static publisher

`publisher/markdown_gen.py` renders eight deterministic pages from the analysis reports and
store queries: `index.md`, `claims.md`, `architecture.md`, `entry-points.md`, `database.md`,
`side-effects.md`, `hotspots.md` and `files.md`. Every statement comes from the IR; this
milestone publishes no AI-generated content except for verified and inferred claims, which are
labelled and cited, and no unsupported claims are published. Pages contain repository-relative
paths, no timestamps and no environment data, so repeated publishes are byte-stable.

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

## LLM layer

The LLM stage is optional and provider-agnostic. It never feeds generated prose back into the
IR: a completion can only produce `claims`, and those claims start as `inferred` with
`origin = llm`.

- **Providers** — `llm/providers` defines one `complete(system, prompt, ...) -> Completion`
  interface. `HostedProvider` speaks the OpenAI-compatible `/chat/completions` shape, so any
  compatible endpoint works by changing `base_url`; `LocalProvider` speaks Ollama's
  `/api/chat`; `NullProvider` is the no-network adapter used when the stage is disabled.
  The factory `build_provider(settings)` selects an adapter and rejects missing models.
- **Settings and keys** — `LLMSettings` carries the provider, model, endpoint, sampling
  values, timeouts, retry policy and optional per-1,000-token prices. API keys are read from
  explicit settings or `LEGACYLENS_API_KEY`/`OPENAI_API_KEY`/`OLLAMA_API_KEY`, are redacted
  from `repr`/`as_dict`, and are excluded from cache keys. Hosted requests send a bearer
  token only when one is configured.
- **Context builder** — `llm/context_builder.py` resolves a function, class, file or project,
  gathers its metrics, source excerpt, calls, data access, side effects, entry points,
  business rules and reachability, and renders deterministic sections. Every fact carries a
  citation; source lines are re-read through repository-relative paths only when
  `source_root` is supplied. A character budget (estimated at four characters per token)
  trims sections in priority order and marks the bundle `truncated`.
- **Prompts and claims** — prompt templates in `llm/prompts` require strict JSON and cite
  supplied ids. `parse_claims` treats response text as untrusted: it accepts only claims with
  text and at least one citation that actually appeared in the context, drops everything
  else, and records `origin=llm`, `status=inferred`, the model and the prompt hash.
- **Cache** — `llm/cache.py` keys completions by SHA-256 over provider, model, endpoint,
  sampling settings, system prompt and user prompt. Hits preserve prompt/completion tokens
  and cost, update the store's hit counter and are reported by `FactStore.cache_stats()`.
  The null provider bypasses the cache.
- **Service** — `llm/service.py` composes context, prompt rendering, cached completion and
  claim parsing, then replaces claims for the target subject. The no-op provider generates
  nothing and never clears existing claims.

Verification is now implemented in `verification/verifier.py`: it parses the citation grammar,
resolves each citation against the store and, when available, the source tree, assigns
`verified`, `inferred` or `unsupported`, and persists the result with `verify_claims`. The
publisher then reads stored statuses and excludes unsupported claims.

## Claim verification

`verification/verifier.py` resolves every stored claim against the IR and, optionally, the
source tree. Citations follow the IR grammar (`file:line`, `fn:`, `class:`, `table:`,
`entry:`); malformed citations are unsupported, a claim with no resolvable citation is
unsupported, and a partially resolved claim is at most `inferred`. A file-line citation is
checked against the stored line count and, when `source_root` is supplied, against the
physical file. Table citations inherit the strongest confidence of the SQL rows that name
the table; entry citations inherit the matched entry point's confidence. The claim subject
must also resolve, except for the virtual subjects `project` and `*`.

Verification writes statuses back through `replace_claims`, subject by subject, only when a
status changes. It never edits claim text or citations. `VerificationReport` exposes per-label
counts, `verification_rate`, `verified_rate` and a JSON-ready `as_dict()`.

## Local Q&A

`qa/retrieval.py` builds an immutable, deterministic BM25 index over two corpora:

- **Documentation** — the publisher's eight Markdown pages, split at `##` headings into
  section passages with unique slugs. Pages are generated in memory from the IR by default,
  so the index cannot drift from the store; `docs_dir` indexes a published tree instead.
- **IR facts** — one passage per file, class, function, table, entry point and publishable
  claim. Passages carry the same citation grammar as the verifier (`fn:`, `class:`,
  `table:`, `entry:`, `path`, `path:line`) plus embedded evidence references. Doc citations
  are `docs/<page>.md#<section>` and never masquerade as source facts.

Tokenisation is deterministic: lowercase words, identifier splitting on `_`, `.` and camel
case, and a fixed stopword set. Field weights are title 3, section 2, citations 2 and body
1. Exact id/citation/title matches receive a larger boost than embedded references, so a
question naming `fn:src/User.php#User::find` ranks the function itself above the file and
class passages that mention it. Filters restrict results to passage kinds and to a generated
page; irrelevant or stopword-only queries return no hits rather than arbitrary rows.

`qa/answers.py` has two modes over the same evidence list:

- **Extractive** — assembles the top distinct passages into an answer. It needs no model and
  can only cite retrieved passages.
- **LLM** — renders numbered evidence with exact citation strings, uses the `qa` prompt and
  the persistent completion cache, then validates every returned citation against the
  citations supplied in that request. Numeric markers such as `"1"` map to the corresponding
  evidence block. Unknown citations are reported as `unresolved_citations` and never become
  support.

Status mirrors claim verification: all returned citations supplied → `verified`; partial
support or a model-declared insufficient answer → `inferred`; no valid citation or no
retrieval match → `unsupported`. Answers are not persisted as claims.

`qa/server.py` exposes a local FastAPI app: `GET /health`, `GET /search`, `POST /ask`, the
HTML interface and its packaged CSS/JS. The module runner binds `127.0.0.1` by default, uses
the no-op provider unless one is configured, and can index `--docs`. The store is opened
with `check_same_thread=False` because FastAPI may dispatch synchronous endpoints from a
worker thread; SQLite serialises statements on that shared local connection.

## Evaluation configurations

- **A — static only:** no LLM; all facts and claims are deterministic.
- **B — static + LLM:** LLM summaries and rule narrative, no verification gate.
- **C — static + LLM + verification:** published claims must resolve; unsupported claims are
  dropped and counted.

`evaluation/metrics.py` scores each run against hand-prepared ground truth with set
precision/recall/F1. Entry points are matched by `<kind>:<path>` or
`<kind>:<METHOD> <path>`; tables by table/operation pairs; business rules by normalised
evidence text. Duplicate endpoint declarations collapse to one identifier. Ground truth is
strictly validated JSON (`evaluation/ground_truth/README.md`): unknown fields, duplicate
identifiers and unknown operations are rejected. It is written by hand and never derived
from LegacyLens output.

`evaluation/runner.py` scans and parses the repository into a fresh SQLite store for each
configuration, then runs the same static analyses in all three so detection metrics are
directly comparable. LLM configurations explain an explicit target list, or the functions
ordered by file and line up to `max_targets`. Configuration B leaves generated claims
`inferred` and reports `verification_rate = null`; configuration C runs
`verification/verifier.py`, reports label counts, verification rate, verified rate and
publishable claims. Stores are never shared, so cached completions and claim state cannot
leak between configurations.

Usage accounting separates `estimated_cost_usd` — the provider-reported cost of every
completion, including cache hits — from `billable_cost_usd`, which counts cache misses only.
Reports also carry prompt/completion token totals and the cache hit rate. Results are
JSON-ready and written under `evaluation/results/`; no network call is made unless a
provider is configured.
