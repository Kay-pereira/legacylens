# LegacyLens IR specification

Status: **normative, version 1**. This document defines the intermediate representation
(IR) that every parser produces, the SQLite store persists and every analysis, LLM,
verification and publishing stage consumes. It is a project deliverable and a companion
to [`architecture.md`](architecture.md).

The executable definition is split across:

| Artefact | Purpose |
|----------|---------|
| [`src/legacylens/ir/models.py`](../src/legacylens/ir/models.py) | Immutable dataclasses and enum vocabulary |
| [`src/legacylens/ir/schema.sql`](../src/legacylens/ir/schema.sql) | SQLite DDL, constraints and indexes |
| [`src/legacylens/ir/store.py`](../src/legacylens/ir/store.py) | Atomic writes, typed reads and aggregates |

If this document and the code disagree, the code wins and this document must be fixed.

## 1. Goals

- Flat, language-agnostic facts that are cheap to write, query and diff.
- Every fact traceable to a repository-relative file and a 1-based line, or explicitly
  marked as a heuristic with a lower confidence label.
- Stable keys so re-analysing one file replaces exactly that file's facts.
- Enough structure for dependency/call graphs, SQL and CRUD coverage, entry-point
  inventories, metrics, documentation and evaluation, without pretending to be a compiler
  IR.
- Partial success: a file with a parse error still has a `files` row and whatever facts
  were extracted; the failure is recorded, not hidden.

## 2. Conventions

### 2.1 Paths and identities

- Every path is **repository-relative**, POSIX-separated (`/`), and has no leading `./`,
  drive letter or absolute prefix. The scanner normalises paths before parsing.
- A file's primary key is its path. There is one `files` row per path; the same physical
  content in two paths is two files.
- Paths are case-preserving. Consumers must not rely on case-insensitive comparison.
- `#` is reserved as the separator in `fn:`/`class:` keys and is not permitted inside a
  file path or qualified name. Newlines are not permitted inside any path or citation.
  Ingestion rejects such paths before parsing.
- Generated and vendored files are stored like any other file; the `is_generated` and
  `is_vendored` flags let analyses exclude them. Both default to `false`.

### 2.2 Lines, columns and spans

- Lines are **1-based** everywhere: `line`, `span_start_line`, `span_end_line`, citations.
- Columns are **0-based** and measure characters, matching Tree-sitter. `span_start_column`
  is the first character of the node; `span_end_column` is the exclusive end offset, as
  reported by Tree-sitter.
- `Span.end_line` is inclusive. `line_count` is `end_line - start_line + 1`.
- A function's `loc` is derived from its span; it is not an independent measurement of
  blank/comment lines.

### 2.3 Qualified names

Qualified names are language-agnostic strings. Use the namespace or package as a prefix
and `::` as the member separator for **all** languages:

| Language | Class example | Function example |
|----------|---------------|------------------|
| PHP, global | `User` | `User::find` |
| PHP, namespaced | `App\Domain\User` | `App\Domain\User::find` |
| PHP, top-level function | — | `App\helpers\format_date` |
| Java | `com.example.UserRepository` | `com.example.UserRepository::findById` |
| Closure / anonymous function | — | `User::find::{closure@9:13}` |

Closures and anonymous functions need a deterministic suffix supplied by the parser, for
example `{closure@<line>:<column>}` scoped under the enclosing qualified name. Names must
be unique within a file; duplicate class or function keys make the file invalid.

### 2.4 Confidence labels

| Label | Meaning | Typical source |
|-------|---------|----------------|
| `verified` | Observed directly in the AST, or resolved deterministically and uniquely against the IR. | A function declaration; `require 'inc/db.php'`; a resolved call target. |
| `inferred` | Produced by a heuristic, name matching or an LLM, and supported by resolvable citations. | A call target selected by name; a table name guessed from SQL; an LLM summary. |
| `unsupported` | Not grounded in the IR. Never published as fact; retained for evaluation and debugging. | A claim whose citations do not resolve. |

Parser facts default to `verified` for syntactic observations. Any fact that depends on
symbol resolution starts at `inferred`. Generated claims inherit the **weakest** label of
their evidence (`models.weakest_confidence`). Verification may only promote a claim by
re-resolving evidence; it must never promote merely because text looks plausible.

### 2.5 Timestamps

Timestamps are timezone-aware UTC `datetime` values in Python and ISO 8601 strings in
SQLite. Naive inputs are treated as UTC. `created_at` records when a claim or cache entry
was produced; cache `last_used_at` changes on a hit.

### 2.6 Enum vocabulary

All enum columns store the values below. The DDL repeats them in `CHECK` constraints, so
adding a value requires a schema-version bump.

| Enum | Values |
|------|--------|
| `Language` | `php`, `java`, `jsp`, `xml`, `other` |
| `ClassKind` | `class`, `interface`, `trait`, `enum`, `record` |
| `Visibility` | `public`, `protected`, `private`, `package` |
| `EdgeKind` | `include`, `require`, `import`, `call`, `instantiate`, `extends`, `implements`, `uses_trait`, `route`, `view` |
| `SqlOperation` | `select`, `insert`, `update`, `delete`, `replace`, `merge`, `ddl`, `exec`, `other` |
| `HttpMethod` | `GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `HEAD`, `OPTIONS`, `ANY` |
| `EntryKind` | `route`, `servlet`, `filter`, `action`, `jsp`, `cli`, `cron` |
| `SideEffectKind` | `database`, `filesystem`, `http`, `email`, `session`, `output`, `process`, `logging`, `cache`, `other` |
| `ClaimKind` | `summary`, `purpose`, `behaviour`, `business_rule`, `risk`, `data_model`, `entry_point`, `table`, `other` |
| `ClaimOrigin` | `static`, `llm` |

`SqlOperation.crud` maps operations to the CRUD matrix: `select → R`, `insert`/`replace →
C`, `update → U`, `delete → D`; everything else has no CRUD letter.

## 3. Fact model

All records are frozen dataclasses. Defaults in the tables below are the defaults on the
dataclass, not implicit database magic. "JSON" columns are TEXT containing compact JSON.

### 3.1 File — `FileRecord` / `files`

The unit of ingestion and incremental replacement.

| Field | Type | Null | Notes |
|-------|------|------|-------|
| `path` | `str` | no | Primary key; repository-relative POSIX path. |
| `language` | `Language` | no | Detected language or descriptor format. |
| `content_hash` | `str` | no | SHA-256 over content plus extractor version; empty is invalid. |
| `size_bytes` | `int` | no | File size on disk, `>= 0`. |
| `loc` | `int` | no | Physical line count, `>= 0`; default `0`. |
| `package` | `str \| None` | yes | Java package or PHP namespace. |
| `module` | `str \| None` | yes | Logical/build module or Composer package. |
| `is_generated` | `bool` | no | Default `false`. |
| `is_vendored` | `bool` | no | Default `false`. |
| `parse_error` | `str \| None` | yes | Non-null when parsing was partial or failed. |

Example: `src/User.php`, `php`, hash `sha256:…`, 20 lines, namespace `App\Domain`.

### 3.2 Class — `ClassRecord` / `classes`

| Field | Type | Null | Notes |
|-------|------|------|-------|
| `id` | `str` | no | Derived key `class:<path>#<qualified_name>`; primary key. |
| `file_path` | `str` | no | FK to `files.path`, cascade delete. |
| `name` | `str` | no | Short declaration name. |
| `qualified_name` | `str` | no | Package/namespace-qualified name. |
| `kind` | `ClassKind` | no | Default `class`. |
| `extends` | `str \| None` | yes | Single superclass, raw qualified text. |
| `implements` | `tuple[str, ...]` | no | JSON array; default empty. |
| `span` | `Span` | no | Declaration span. |
| `modifiers` | `tuple[str, ...]` | no | Raw modifiers (`final`, `abstract`, …). |
| `annotations` | `tuple[str, ...]` | no | Raw annotation/attribute text. |

### 3.3 Parameter — JSON value object

Stored in `functions.parameters` as an ordered JSON array. It is not a table.

| Field | Type | Null | Notes |
|-------|------|------|-------|
| `name` | `str` | no | Source name, including `$` for PHP. |
| `type` | `str \| None` | yes | Declared or annotated type, verbatim. |
| `default` | `str \| None` | yes | Default expression as source text. |
| `variadic` | `bool` | no | Default `false`. |
| `by_reference` | `bool` | no | Default `false`; meaningful for PHP. |

### 3.4 Function — `FunctionRecord` / `functions`

| Field | Type | Null | Notes |
|-------|------|------|-------|
| `id` | `str` | no | Derived key `fn:<path>#<qualified_name>`; primary key. |
| `file_path` | `str` | no | FK to `files.path`, cascade delete. |
| `class_id` | `str \| None` | yes | FK to `classes.id`; `NULL` for top-level functions/closures. |
| `name` | `str` | no | Short name. |
| `qualified_name` | `str` | no | Must be unique within the file. |
| `signature` | `str` | no | Declaration line(s), normalised whitespace. |
| `parameters` | `tuple[Parameter, ...]` | no | JSON array; order preserved. |
| `return_type` | `str \| None` | yes | Declared type or return annotation. |
| `visibility` | `Visibility` | no | Default `public`. |
| `is_static` | `bool` | no | Default `false`. |
| `is_abstract` | `bool` | no | Default `false`. |
| `span` | `Span` | no | Declaration/body span. |
| `loc` | `int` | no | Derived from `span.line_count`; persisted for SQL convenience. |
| `cyclomatic_complexity` | `int` | no | `>= 1`; default `1` for a straight-line function. |
| `max_nesting` | `int` | no | `>= 0`. |
| `modifiers` | `tuple[str, ...]` | no | Raw modifiers. |
| `annotations` | `tuple[str, ...]` | no | Raw annotations/attributes. |

### 3.5 Edge — `EdgeRecord` / `edges`

Edges are directed and intentionally over-approximate when unresolved.

| Field | Type | Null | Notes |
|-------|------|------|-------|
| `id` | `int` | no | Autoincrement row id. |
| `src` | `str` | no | Source unit: file path, `fn:` key or `class:` key. |
| `dst` | `str` | no | Target key when resolved; verbatim reference when not. |
| `kind` | `EdgeKind` | no | Relationship kind. |
| `file_path` | `str` | no | File containing the reference; FK, cascade delete. |
| `line` | `int` | no | 1-based source line. |
| `resolved` | `bool` | no | Default `true`; see below. |
| `confidence` | `Confidence` | no | Default `verified`; resolution-dependent edges use `inferred`. |
| `raw` | `str \| None` | yes | Original source expression, recommended for unresolved edges. |

Target conventions by kind:

| Kind | `src` | `dst` when resolved |
|------|-------|---------------------|
| `include`, `require`, `import` | file path | target file path |
| `call` | `fn:` key | `fn:` key |
| `instantiate` | `fn:` key (or file path) | `class:` key |
| `extends`, `implements`, `uses_trait` | `class:` key | `class:` key |
| `route` | file path or `fn:` key | entry-point citation |
| `view` | `fn:` key or file path | template/JSP file path |

When `resolved` is `false`, `dst` must retain the reference exactly as written (for example
`\Legacy\Db::connect`) and `raw` should preserve the full expression. Consumers must not
invent a target.

### 3.6 Data access — `DataAccessRecord` / `data_access`

| Field | Type | Null | Notes |
|-------|------|------|-------|
| `id` | `int` | no | Autoincrement row id. |
| `function_id` | `str \| None` | yes | FK to `functions.id`; `NULL` for script-level SQL. Cascade from `files`, set null when a function disappears independently. |
| `file_path` | `str` | no | FK to `files.path`, cascade delete. |
| `operation` | `SqlOperation` | no | Normalised statement operation. |
| `table_name` | `str \| None` | yes | Normalised table name; `NULL` when it cannot be determined. Schema qualification is preserved. |
| `sql` | `str` | no | Statement text, whitespace-normalised. |
| `line` | `int` | no | 1-based line of the statement or SQL string. |
| `driver` | `str \| None` | yes | e.g. `PDO`, `JDBC`, `mysqli`. |
| `parameters` | `tuple[str, ...]` | no | Placeholders in order, e.g. `(":id",)`. |
| `confidence` | `Confidence` | no | `verified` for clean SQLGlot parses, `inferred` for regex fallback or guessed tables. |

### 3.7 Entry point — `EntryPointRecord` / `entry_points`

| Field | Type | Null | Notes |
|-------|------|------|-------|
| `id` | `int` | no | Autoincrement row id. |
| `kind` | `EntryKind` | no | route/servlet/filter/action/jsp/cli/cron. |
| `file_path` | `str` | no | File declaring or implementing the entry point; FK, cascade delete. |
| `function_id` | `str \| None` | yes | Handler when known; FK, set null on function deletion. |
| `http_method` | `HttpMethod \| None` | yes | `NULL` for CLI, cron and non-HTTP entries. |
| `path` | `str` | no | URL pattern for web entries, script path for CLI, schedule/job name for cron. |
| `framework` | `str \| None` | yes | Detected framework or mechanism, e.g. `web.xml`, `legacy-router`. |
| `line` | `int` | no | 1-based line of the declaration/mapping. |
| `requires_auth` | `bool \| None` | yes | `true`/`false` only when observed; `NULL` when unknown. |
| `auth_hint` | `str \| None` | yes | Free-text evidence for the auth decision. |
| `confidence` | `Confidence` | no | Default `verified` for explicit declarations. |

### 3.8 Side effect — `SideEffectRecord` / `side_effects`

| Field | Type | Null | Notes |
|-------|------|------|-------|
| `id` | `int` | no | Autoincrement row id. |
| `function_id` | `str \| None` | yes | FK to `functions.id`; null for script-level effects. |
| `file_path` | `str` | no | FK to `files.path`, cascade delete. |
| `kind` | `SideEffectKind` | no | Classified effect family. |
| `detail` | `str` | no | Short human-readable description. |
| `sink` | `str \| None` | yes | API/function that performs the effect, e.g. `PDO::prepare`. |
| `line` | `int` | no | 1-based line. |
| `confidence` | `Confidence` | no | Default `verified`. |

### 3.9 Claim — `ClaimRecord` / `claims`

A claim is the only record that may contain generated prose. It is not tied to a file by
foreign key because its subject may be a table, an entry point or a project-level summary.

| Field | Type | Null | Notes |
|-------|------|------|-------|
| `id` | `int` | no | Autoincrement row id. |
| `subject` | `str` | no | Citation or key the claim is about. |
| `kind` | `ClaimKind` | no | Claim subject matter. |
| `text` | `str` | no | The assertion. |
| `status` | `Confidence` | no | Default `inferred`; `unsupported` is never published. |
| `citations` | `tuple[str, ...]` | no | Citations in the grammar from §4. |
| `origin` | `ClaimOrigin` | no | Default `static`; set `llm` for generated prose. |
| `model` | `str \| None` | yes | Model id for LLM claims. |
| `prompt_hash` | `str \| None` | yes | Hash of prompt + context + settings, for cache/evaluation. |
| `created_at` | `datetime` | no | UTC timestamp. |

### 3.10 Cached completion — `CachedCompletion` / `llm_cache`

Operational data, not a published fact. It shares the project database so results,
cache hits, tokens and cost travel together.

| Field | Type | Null | Notes |
|-------|------|------|-------|
| `cache_key` | `str` | no | SHA-256 of prompt, model, provider and sampling settings; primary key. |
| `response` | `str` | no | Raw completion text. |
| `provider` | `str \| None` | yes | Provider id. |
| `model` | `str \| None` | yes | Model id. |
| `prompt_hash` | `str \| None` | yes | Prompt-only hash, for grouping. |
| `prompt_tokens` | `int` | no | `>= 0`. |
| `completion_tokens` | `int` | no | `>= 0`. |
| `cost_usd` | `float` | no | `>= 0`. |
| `created_at` | `datetime` | no | First insertion. |
| `last_used_at` | `datetime` | no | Updated on a cache hit. |
| `hits` | `int` | no | `>= 0`; preserved when an entry is refreshed. |

## 4. Keys and citation grammar

### 4.1 Keys

| Entity | Key |
|--------|-----|
| File | `<path>` |
| Class | `class:<path>#<qualified-name>` |
| Function | `fn:<path>#<qualified-name>` |
| Table (in citations) | `table:<name>` |
| Entry point (citation) | `entry:<kind>:<method-or-dash>:<path>` |

Class and function keys are derived, never assigned randomly: `ClassRecord.id` and
`FunctionRecord.id` compute them from the file and qualified name, and the store persists
that value as the primary key. This is what makes per-file replacement safe.

### 4.2 Citation grammar

```
<file-path>:<line>
fn:<file-path>#<qualified-name>
class:<file-path>#<qualified-name>
table:<table-name>
entry:<kind>:<method-or-dash>:<path>
```

Rules:

- `<line>` is 1-based and parsed from the right, because repository-relative POSIX paths
  may contain `:` on Unix.
- `#` is reserved as the separator between a path and a qualified name. File paths and
  qualified names **must not** contain `#` or a newline; ingestion rejects such paths.
- `<method-or-dash>` is the uppercase HTTP method, or `-` when the entry is not HTTP
  (CLI, cron, JSP without a method).
- `<table-name>` may include a schema qualifier (`schema.table`).
- Citations never span multiple lines. A claim citing a block cites its first line.
- Examples: `src/User.php:14`, `fn:src/User.php#User::find`,
  `class:src/User.php#User`, `table:users`, `entry:route:GET:/users/{id}`,
  `entry:cli:-:bin/report.php`.

Verification resolves a citation only against the IR: the file must exist, the line must
be within the file, the function/class key must exist, the table must appear in
`data_access`, and an entry citation must match an `entry_points` row. Resolution failure
sets a claim's status to `unsupported`.

## 5. File aggregate and atomic replacement

Parsers return one `FileFacts` value per file:

```python
FileFacts(
    file=FileRecord(...),
    classes=(... ,),
    functions=(... ,),
    edges=(... ,),
    data_access=(... ,),
    entry_points=(... ,),
    side_effects=(... ,),
)
```

`FileFacts.__post_init__` enforces:

- every child `file_path` equals `file.path`;
- class keys and function keys are unique within the file;
- every non-null `FunctionRecord.class_id` refers to a class in the same aggregate.

`FactStore.replace_file` then deletes the existing `files` row and inserts the aggregate in
one transaction. Foreign keys cascade the delete to all child tables, so stale facts from
the previous parse cannot survive. A parser failure is represented by a `FileFacts` whose
`file.parse_error` is set and whose child collections may be empty or partial.

## 6. SQLite persistence

### 6.1 Database and schema version

- One database per project (`[tool.legacylens] db_path`, default
  `.legacylens/legacylens.db`).
- `SCHEMA_VERSION` is `1`, stored in `PRAGMA user_version`.
- `FactStore.initialize()` creates the schema when `user_version` is `0`, accepts exactly
  the current version, and otherwise raises `SchemaVersionError`. Pre-1.0 the remedy is to
  delete and rebuild the database; no in-place migrations are promised yet.
- `PRAGMA foreign_keys = ON`, `journal_mode = WAL`, `synchronous = NORMAL`,
  `busy_timeout = 5000`.
- Tables use SQLite `STRICT` typing. Enum values, booleans, line numbers and confidence
  labels are checked in the DDL.

### 6.2 Tables and indexes

| Table | Purpose | Important indexes |
|-------|---------|-------------------|
| `files` | one row per source file | `language`, `content_hash`, `(is_generated, is_vendored)` |
| `classes` | type declarations | `file_path`, `qualified_name` |
| `functions` | functions/methods/closures | `file_path`, `class_id`, `qualified_name` |
| `edges` | includes, imports, calls, inheritance | `src + kind`, `dst + kind`, unresolved partial index |
| `data_access` | SQL statements | `function_id`, `file_path`, `(table_name, operation)` |
| `entry_points` | routes, servlets, JSPs, CLI, cron | `file_path`, `function_id`, `(kind, path)` |
| `side_effects` | classified effects | `function_id`, `file_path`, `kind` |
| `claims` | generated/derived prose | `subject`, `(status, origin)`, `kind` |
| `llm_cache` | completion cache | `model`, `last_used_at` |

Multi-valued fields (`implements`, `parameters`, `modifiers`, `annotations`, `citations`,
data-access parameter placeholders) are JSON arrays. Booleans are `INTEGER` 0/1; nullable
booleans remain `NULL`.

### 6.3 Store API

`FactStore` is the only supported way to read or write the database.

| Method | Contract |
|--------|----------|
| `replace_file(facts)` | Delete/reinsert one file's facts atomically. |
| `delete_file(path)` | Delete a file and cascade; returns whether a row existed. |
| `get_file`, `list_files` | File reads; filter language/generated/vendored. |
| `get_class`, `find_class`, `list_classes` | Class reads by id, qualified name or file. |
| `get_function`, `find_function`, `list_functions` | Function reads by id, qualified name, file or owner class. |
| `list_edges` | Filter by kind, source, destination, resolution and file. |
| `list_data_access` | Filter by function, file, table and operation. |
| `list_entry_points` | Filter by kind, file, path and HTTP method. |
| `list_side_effects` | Filter by function, file and kind. |
| `add_claim`, `add_claims`, `replace_claims`, `list_claims`, `clear_claims` | Claim lifecycle. `replace_claims(subject=…)` is the normal verification/publishing path. |
| `put_cached_completion`, `get_cached_completion`, `delete_cached_completion`, `cache_stats`, `clear_cache` | Completion cache, including hit accounting. |
| `summary` | Row counts plus unresolved-edge count. |
| `confidence_counts` | Three-label, zero-filled counts per fact table (claims counted by `status`). |

`connection` exposes the raw SQLite connection as a read-only escape hatch for analyses
that need a query the typed helpers do not cover. Writes must still go through the store
so transactions and cascades stay correct.

## 7. Confidence and resolution policy

1. A syntactically observed fact is `verified`; its *meaning* may still require inference.
   A `require` statement naming `inc/db.php` is verified; the function called on the next
   line is only verified once its target is resolved uniquely.
2. Resolution rules:
   - Include/import/require paths are resolved relative to the declaring file, then to the
     project root; a single existing file wins.
   - A call is `verified` only when exactly one function matches within the declaring
     language and the owning class/namespace is compatible. Zero or multiple candidates
     leave the edge `inferred` (or unresolved).
   - A table name parsed from literal SQL is verified; a guessed table or regex fallback is
     inferred.
3. Entry-point declarations in configuration and annotations are verified when the mapping
   is explicit; heuristic route discovery is inferred.
4. LLM output starts as `inferred` and `origin = llm`. Verification resolves every
   citation: all resolve → `verified`; some resolve but evidence is weak → `inferred`;
   none resolve → `unsupported`.
5. `unsupported` claims remain in the database for evaluation and debugging. Publishing
   excludes them by default.
6. A claim never strengthens beyond the weakest label of its evidence.

## 8. Incremental analysis

- The scanner computes `content_hash` from file content **plus the extractor version**, so
  changing a parser invalidates only the files it can affect.
- On re-analysis, an unchanged hash lets the pipeline skip parsing; a changed hash produces
  a fresh `FileFacts` and calls `replace_file`.
- Deleting a source file calls `delete_file`, which cascades classes, functions, edges, data
  access, entry points and side effects.
- If a function referenced from another file disappears, `ON DELETE SET NULL` clears the
  FK on data access, entry points and side effects. The referencing edges remain and are
  reported as unresolved rather than silently retargeted.
- Claims are not cascade-deleted with files because their subjects may be tables or
  aggregates. Re-verification owns them: use `replace_claims` per subject and count the
  results in the evaluation report.
- The LLM cache is keyed by prompt/model/settings hashes, not by file hash, so a completion
  is reused whenever the exact prompt and settings recur.

## 9. Non-goals

- Full type inference, overload resolution or generics beyond declared types.
- Executing, instrumenting or calling into the legacy application.
- Perfect resolution of dynamic dispatch, variable functions, string-built includes, ORM
  magic or dependency-injection wiring. These stay inferred/unresolved and appear in the
  "unknowns" report.
- Storing source bodies in the IR. The IR stores spans and signatures; consumers re-read
  source through the file path.
- A general-purpose SQL AST. `data_access` is a normalised fact, not a query engine.

## 10. Versioning and resolved questions

**Versioning.** There is one global schema version (`PRAGMA user_version`) and one IR
version, not a per-parser version. Parser evolution is handled through the content hash;
prompt evolution is handled through prompt hashes. A change to an enum value, table shape
or key format bumps the schema version.

Earlier open questions are resolved as follows:

| Question | Decision |
|----------|----------|
| Minimum evidence to promote an inferred call edge to verified? | Exactly one compatible target must exist in the IR, in the same language, with a matching namespace/owner. Otherwise the edge stays inferred. |
| How are generated and vendored code represented? | `files.is_generated` and `files.is_vendored`; analyses exclude both by default but the facts remain queryable. |
| Per-parser IR version or global schema version? | One global schema version; parser version is folded into `content_hash`. |
