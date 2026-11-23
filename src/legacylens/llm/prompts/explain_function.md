# Explain a function

You are documenting a legacy PHP/Java web application. Using only the supplied facts, write
short, concrete documentation for the target function.

Rules:

- Every claim must cite at least one supplied citation id.
- Do not speculate about behaviour that the facts do not support.
- Prefer the narrowest citation that proves the statement.
- Return strict JSON only, with this shape:
  `{"summary": "...", "claims": [{"kind": "summary|purpose|behaviour|business_rule|risk|data_model|other", "text": "...", "citations": ["..."]}]}`.
- The `summary` may be short, but every entry in `claims` must stand alone and be cited.

Target: {{target}}

Facts:

{{context}}
