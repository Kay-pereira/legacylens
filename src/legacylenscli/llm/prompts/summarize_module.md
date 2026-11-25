# Summarise a module

Describe the responsibility of the supplied file or module and how it participates in the
application. Use only the supplied facts and cite them.

Rules:

- Every claim must cite at least one supplied citation id.
- Do not infer a framework, table or relationship that the facts do not show.
- Return strict JSON only, with this shape:
  `{"summary": "...", "claims": [{"kind": "summary|purpose|behaviour|business_rule|risk|data_model|entry_point|table|other", "text": "...", "citations": ["..."]}], "open_questions": ["..."]}`.

Target: {{target}}

Facts:

{{context}}
