# Answer a question about the application

Answer the question using only the supplied documentation passages. Cite passages as
`[path#section]`. If the passages do not contain the answer, say so explicitly.

Return strict JSON only, with this shape:
`{"answer": "...", "citations": ["..."], "insufficient_evidence": false}`.

Question: {{question}}

Passages:

{{context}}
