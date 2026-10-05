# RAG operations and security

Running a RAG system after launch. Companion to `SKILL.md` sections 9 and 10. This file
lists what a RAG pipeline should emit and guard, whatever logging stack you use.

## 1. Observability

Emit one structured record per stage per request, tied together by a correlation ID
(thread/session ID, run ID, node name), as `key=value` fields. Log aggregates, not
content.

| Stage | Fields |
|---|---|
| route | label chosen, latency |
| rewrite / decompose | number of sub-queries, latency |
| retrieve | candidates from each retriever, fused count, top rerank score, latency |
| grade | evidence grade, hop number |
| generate | model, prompt/completion tokens, retries, latency |
| verify | claims checked, minimum claim score, judge model, failure reason |
| outcome | reason code (`verified`, `low_evidence_grade`, `unsupported_claims`, ...), abstained yes/no |

- Never log secrets, full prompts, completions, retrieved documents, claim text or personal data
  at INFO. If debugging needs them, log truncated and masked at DEBUG only.
- Build dashboards on: abstention rate by reason code, minimum-claim-score distribution,
  verifier-fail rate, retrieval top-score distribution, hops per query, per-stage p50/p95
  latency, and cost per answered query. Alert on sudden shifts, which usually mean corpus
  drift, an index problem or a provider change.
- Record the index version, embedding model, prompt version, model names and thresholds
  on every run so any answer is traceable to its exact configuration.

## 2. Close the feedback loop

- Review a sample of abstentions and thumbs-down weekly. Cluster them into:
  1. **Corpus gap**: the answer is not in the sources (add or fix content).
  2. **Retrieval failure**: it is in the sources but not retrieved (chunking, embeddings,
     filters, query rewriting).
  3. **Generation/verification failure**: right evidence, wrong or rejected answer
     (prompt, judge, `tau`).
  4. **Policy gap**: the question should be handed off or declined by design.
- Add every confirmed case to the golden set (`evaluation.md`) so fixes stay fixed.
- A rising abstention rate on previously answerable topics is an early signal of stale
  or broken content.

## 3. Freshness, versioning and rollback

- Re-ingest on source change; expire or tombstone superseded versions; support hard
  deletes for withdrawn content and verify the deletion reached both the vector and the
  sparse index.
- Cite the document version or effective date in answers where it matters (policies,
  figures, terms).
- Version the index, chunking config, embedding model, prompts, models and thresholds
  together. Build a new index beside the old one, evaluate it, then swap atomically;
  keep the previous one for rollback.
- Make ingestion idempotent and checkpointed so an interrupted bulk job resumes without
  repeating paid LLM or embedding calls. Estimate and cap cost before bulk passes.

## 4. Security

- **Treat retrieved text as untrusted data, never instructions.** Documents can contain
  prompt injection ("ignore previous instructions", hidden text, links that trigger
  actions). Delimit passages clearly, state in the system prompt that context is data,
  and never let retrieved content trigger tool calls or side effects without a separate
  authorization check.
- **Enforce access control at retrieval**: filter by user/tenant/role inside the search
  query. Never rely on the generator to withhold restricted content it was shown.
- **Do not put secrets or PII into the index or the context** unless the use case
  requires it and access is controlled; scrub at ingest.
- **Keep per-user private facts out of the shared index.** Account state and personal
  records come from authorized lookups scoped to the signed-in user, not from retrieval.
- Validate and size-limit user input; rate-limit per user; protect provider keys and keep
  judge/eval keys separate from production keys.

## 5. Failure behavior

- **Fail closed.** If retrieval, grading or verification errors, times out, or returns an
  unparseable result, abstain or hand off. Never fall back to an unverified answer.
- Use timeouts and bounded retries with backoff for every external call (LLM, embedder,
  reranker, vector store); count retries in logs.
- Degrade explicitly: if the reranker is down, either abstain or serve fused results at a
  stricter `tau`, as a deliberate, logged mode rather than an accident.
- Bound every loop (hops, tokens, wall-clock) so a bad query cannot run up cost.
- Provide a human-handoff path for abstentions on high-stakes topics.
