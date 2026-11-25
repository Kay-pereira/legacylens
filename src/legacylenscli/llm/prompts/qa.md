# Answer a question about the application

Answer the question using only the supplied evidence passages. Every passage carries one or
more exact citation strings. Cite evidence by returning its exact citation string. Do not
cite anything that is not supplied.

If the passages do not contain the answer, say so explicitly and set
`insufficient_evidence` to `true`.

Return strict JSON only, with this shape:
`{"answer": "...", "citations": ["..."], "insufficient_evidence": false}`.

Question: {{question}}

Evidence:

{{context}}
