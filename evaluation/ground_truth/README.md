# Ground truth

Hand-prepared reference results for the evaluation corpus. One JSON file per application,
committed (unlike `evaluation/results/`).

## Shape

```json
{
  "app": "legacy_web",
  "commit": "<sha>",
  "entry_points": [
    "route:/index.php",
    "servlet:/users/*",
    "jsp:/users/list.jsp"
  ],
  "tables": [
    {"name": "articles", "operations": ["SELECT"]},
    {"name": "users", "operations": ["SELECT", "INSERT"]}
  ],
  "business_rules": [
    "filter_var($email, FILTER_VALIDATE_EMAIL)"
  ],
  "notes": "Prepared by hand from manual review; see PR description."
}
```

Only `app` is required. `commit`, `notes`, `entry_points`, `tables` and `business_rules`
are optional and default to empty; unknown fields are rejected so a typo cannot silently
drop reference data.

## Identifiers and matching

Matching is exact after whitespace normalisation; identifiers are human-readable, not
database ids.

- **Entry points** use `<kind>:<path>` or `<kind>:<METHOD> <path>`, for example
  `route:/index.php`, `cli:public/export.php`, `servlet:GET /users/*` and
  `jsp:/admin/secret`. Duplicate declarations of the same endpoint collapse to one
  identifier.
- **Tables** are objects with a table `name` and the uppercase SQL `operations` manual
  review confirmed. Scoring compares table/operation pairs, not whole tables.
- **Business rules** are the exact evidence expressions the deterministic rule scanner
  reports, for example a condition body or a call such as
  `filter_var($email, FILTER_VALIDATE_EMAIL)`. Prose summaries are not matched.

## Rules

- Include only what manual review confirmed; omit anything uncertain and note it.
- Never derive ground truth from LegacyLens output.
- Add the matching application fixture or corpus commit alongside the file.
