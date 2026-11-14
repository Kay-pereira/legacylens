# LegacyLens IR specification

Status: **proposal / draft**. This document is a project deliverable and defines the
intermediate representation (IR) that every parser produces and every analysis consumes.

## 1. Goals

- Flat, language-agnostic, append-only facts that survive partial analysis.
- Every fact is traceable to a file and line, or explicitly marked as heuristic.
- Stable keys so incremental re-analysis can replace one file's facts atomically.
- Enough structure for graphs, SQL/CRUD analysis, documentation and evaluation, without
  attempting to be a full compiler IR.

## 2. Confidence labels

| Label         | Meaning                                                                 |
|---------------|-------------------------------------------------------------------------|
| `verified`    | Observed directly in the AST or resolved deterministically against the IR. |
| `inferred`    | Produced by a heuristic or LLM; supported by resolvable citations.         |
| `unsupported` | Not grounded; never published as fact, kept only for evaluation.          |

Parser facts default to `verified` for syntactic observations (a function exists, an include
statement names a file) and `inferred` for resolution-dependent facts (a call target matched by
name). Generated claims inherit the weakest label of their evidence.

## 3. Entities

TODO: specify fields, keys, nullability and examples for:

- **File** — repository-relative path, language, content hash, size, LOC, module/package.
- **Class** — file, name, qualified name, kind (class/interface/trait/enum), extends,
  implements, span, modifiers, annotations.
- **Function** — file, optional owning class, qualified name, signature, parameters, return
  type, visibility, static/abstract flags, span, LOC, cyclomatic complexity, nesting.
- **Edge** — source, destination, kind (`include`, `require`, `import`, `call`, `instantiate`,
  `extends`, `implements`, `uses_trait`, `route`, `view`), file, line, resolved flag,
  confidence. Unresolved edges retain the source reference verbatim.
- **DataAccess** — function, operation (`SELECT`/`INSERT`/`UPDATE`/`DELETE`/`DDL`/…), table,
  SQL text, line, driver, parameters, confidence.
- **EntryPoint** — kind (route/servlet/action/jsp/cli/cron), file, handler function, HTTP
  method, path, framework, line, auth hint, confidence.
- **SideEffect** — function, kind (database/filesystem/http/email/session/output/process/…),
  detail, sink, line, confidence.
- **Claim** — subject key, claim type, text, status, citations, origin (`static`/`llm`), model,
  prompt hash, timestamp.

## 4. Citation grammar

TODO: finalise and document unambiguous citation ids for verification:

```
<file-path>:<line>          e.g. src/User.php:14
fn:<file-path>#<qualname>   e.g. fn:src/User.php#User::find
class:<file-path>#<name>    e.g. class:src/User.php#User
table:<name>                e.g. table:users
entry:<kind>:<method>:<path>
```

## 5. Persistence

SQLite, one database per project. Tables mirror the entities above; JSON columns store
multi-valued attributes (parameters, implements, annotations, citations). Re-analysis of a
changed file deletes and reinserts that file's facts in a single transaction.

## 6. Non-goals

- Type inference beyond annotations and obvious literals.
- Executing or instrumenting the application.
- Perfect resolution of dynamic dispatch, string-built includes or ORM magic. These are
  reported as unresolved edges / low-confidence facts instead of guessed.

## 7. Open questions

- Minimum evidence required to promote an inferred call edge to verified?
- How should generated-file and vendored-code facts be represented so they can be excluded?
- Do we version the IR per parser, or store a global schema version?
