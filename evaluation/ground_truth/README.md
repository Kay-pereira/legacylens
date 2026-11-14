# Ground truth

Hand-prepared reference results for the evaluation corpus. One JSON file per application,
committed (unlike `evaluation/results/`).

Suggested shape:

```json
{
  "app": "php-legacy",
  "commit": "<sha>",
  "entry_points": ["GET /index.php?id", "servlet:GET /users/*"],
  "tables": [{"name": "users", "operations": ["SELECT", "UPDATE"]}],
  "business_rules": ["role must be admin to promote a user"],
  "dead_code": ["src/legacy.php#old_helper"],
  "claims": ["User::find reads the users table"],
  "notes": "Prepared by hand from manual review; see PR description."
}
```

Rules:

- Identifiers are stable and human-readable; matching is done on normalised strings.
- Include only what manual review confirmed; omit anything uncertain and note it.
- Never derive ground truth from LegacyLens output.
