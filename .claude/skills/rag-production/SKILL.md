---
name: rag-production
description: Best practices for production-grade Retrieval-Augmented Generation (RAG) with minimal hallucination - ingestion, chunking, embeddings and indexes, hybrid retrieval and reranking, query routing, cited generation, claim-level verification, abstention, corrective loops, evaluation, cost and operations. Use whenever designing, adding or reviewing a RAG pipeline, retriever, chunker, vector store, reranker, citation or grounding logic, hallucination guardrail or RAG eval, or when asked how to make answers faithful or production-ready. Framework-, model- and vendor-agnostic.
---

# RAG production best practices

A checklist for designing and reviewing RAG systems you can run, measure and trust. It
is independent of any framework, model vendor, vector store or domain: it records the
design decisions, not the library calls. Follow your own project's conventions for code
style, logging and file layout.

Reference files (read when you reach that work):
- `references/patterns.md`: tested code patterns for chunking, rank fusion, citations,
  structured claims, the verification gate, the corrective loop and eval helpers.
- `references/evaluation.md`: golden sets, metrics, calibration, judge validation.
- `references/operations.md`: observability, feedback loops, freshness, security.

**How to read the numbers.** Every threshold, size and count below (chunk tokens, dedup
similarity, `k`, hop limits, grade cut-offs, `tau`) is a starting point. Calibrate it on
your own eval set; none is a universal default. Library APIs also change quickly, so
confirm signatures against current docs before coding.

## 1. Decide whether you need RAG, then start simple

| Need | Use |
|---|---|
| Structured, exact data (records, transactions, account state) | SQL or tool lookup, not RAG |
| Small, stable corpus that fits in context | Put it in the prompt (cache it) |
| Large or changing unstructured knowledge (policies, manuals, documentation, FAQs) | RAG |
| Style, format or behavior | Prompting or fine-tuning, not RAG |

- Build the simplest end-to-end baseline first (chunk, embed, top-k, generate), build
  an eval set, then add one layer at a time (hybrid, rerank, verify, ...). Keep a layer
  only if the metrics move.
- Most "hallucination" is a retrieval or corpus problem. Measure retrieval recall
  before touching prompts or the generator.

## 2. Ingestion and corpus hygiene

- **Inspect before indexing**: size distributions, empty or garbled documents, a sample
  of real content.
- **Normalize text** (Unicode NFKC, collapse whitespace, strip boilerplate such as
  headers, footers and navigation) so lexical search tokenizes consistently.
- **Deduplicate near-copies** with MinHash/LSH (start around 0.9 Jaccard), never
  quadratic comparison. Duplicates crowd the top-k and inflate retrieval metrics.
  **Scope dedup by version and entity**: two policy versions, or two near-identical
  records that differ only in a figure or date, are different evidence. Dedup within
  one scope and never drop a candidate whose numbers differ from the copy you keep.
- **Attach metadata at ingest**: source, document ID, section, version, effective
  date, language, access scope/tenant. You need it for filtering, citations, freshness
  and deletion.
- **Idempotent, incremental ingestion.** Key chunks by content hash or (doc ID,
  version), re-index only what changed, support hard deletes (stale or withdrawn
  content is a hallucination source), and checkpoint expensive artifacts so a rerun
  does not repeat paid work.
- **Estimate cost first.** Before any per-chunk LLM pass (contextual prefixes,
  summaries), print a call-count and cost estimate and support a cap. Run a tiny smoke
  profile (hundreds of documents) end to end before the full corpus.
- **Scrub or tag sensitive data** (PII, secrets) before it enters the index.

## 3. Chunking

- **Pack whole sentences up to a token budget** with small overlap (for example 200-400
  tokens, 10-15% overlap, tuned by retrieval recall on your eval set), not fixed
  character windows that cut answer-bearing sentences. Use a real sentence segmenter;
  handle a single over-budget sentence.
- **Count tokens with the generator's tokenizer.** A silent context overflow truncates
  evidence and invites invented answers.
- **Respect structure**: split on headings first; keep tables, lists and code intact
  (or serialize a table row with its header).
- **Contextual prefix, for retrieval only.** A one-line LLM-written situating prefix
  ("From the Travel Policy, section on reimbursement for international trips") resolves
  pronouns and entity references so isolated chunks are findable. Embed and index the
  prefixed text, but **store and show the generator and verifier the raw chunk text**.
  The prefix is model-written, so if it counts as evidence, the verifier can "support"
  a claim with text that was invented at indexing time. Batch the calls and use prompt
  caching.
- **Keep parent pointers** (doc ID, position) to expand to neighbors or the full section.

## 4. Embeddings and the index

- Choose the embedding model on your own domain's retrieval eval, not a leaderboard.
  Normalize vectors and use cosine or inner product consistently.
- **Use asymmetric modes where the model offers them**: embed queries and passages with
  their separate task types or prefixes. Mixing them silently lowers recall.
- **Version the embedding model and chunking config** with the index. Changing either
  means a full re-embed; never mix vectors from different models in one index.
- **Approximate indexes (HNSW, IVF-PQ) trade recall for speed.** Measure recall@k
  against exact search on your real embeddings, tune `ef_search` / `nprobe`, and
  remember quantization loses recall (a reranker only recovers what the candidate set
  contains). Do not trust scale numbers from synthetic random vectors or extrapolate a
  trend from a few sizes; benchmark at your target size with realistic queries.
- **Apply metadata filters inside the search** (tenant, language, version, ACL), not
  after, so filtering never starves the result list.
- Build new indexes side by side and swap atomically so you can roll back.
- The embedder, reranker and generator are separate components. Choose, version and
  configure each independently so any one can be swapped without touching the others.

## 5. Retrieval: hybrid, fused, reranked

1. **Hybrid search.** Index chunks as dense vectors and as BM25/sparse postings. Dense
   catches paraphrase; BM25 catches rare names, IDs, codes and numbers.
2. **Fuse by rank, not score** with Reciprocal Rank Fusion (`sum(1 / (k + rank))`,
   k about 60). Dense and BM25 scores live on different scales; ranks do not.
3. **Go wide, then narrow.** Retrieve many candidates cheaply (100-200), rerank with a
   cross-encoder that reads query and passage together, keep a small final set
   (5-20). Reranker cost depends on candidate count, not corpus size.
4. **Diversify** (MMR or per-document caps) so one long document does not fill the
   context.
5. **Right-size the context.** More passages is not better: weak passages are
   distractors. Passage order can matter (some models under-use the middle); test it
   on your eval rather than assuming.
6. Track recall@k at each stage. If gold evidence is not in the candidates, nothing
   downstream can save the answer.

## 6. Query understanding

- **Route each query** with a cheap, constrained classifier (small model, fixed label
  set, `temperature=0`): `no_retrieval` (greetings, chit-chat, out-of-scope),
  `single_hop`, `multi_hop`.
- **`no_retrieval` must not become a free-form answer.** Return a scoped canned reply or
  hand off. An unconstrained answer is ungrounded by construction.
- **Decompose multi-hop questions** into 2-3 self-contained sub-questions (no dangling
  pronouns), retrieve per sub-question, merge and dedupe the evidence.
- **Rewrite follow-ups** into standalone queries using conversation history: carry the
  entities forward, detect a topic change and reset, keep the original query too.
- **False premises** ("Why was feature X deprecated?" when it was not): let evidence
  and verification catch them, since no passage supports the premise. A separate
  detector is optional and best run offline; a per-query LLM call that gates nothing is
  wasted latency and cost.

## 7. Hallucination control (the core)

Treat "near-zero hallucination" as a layered contract, not a prompt trick. You cannot
make a model never hallucinate; you can build a system whose answers are only
statements it can ground in retrieved text, and which abstains otherwise. That turns
hallucination into a safe, measurable failure mode (abstention).

### 7.1 Retrieve the right evidence
Sections 2-6. A faithful answer over the wrong evidence is still wrong.

### 7.2 Constrain generation
- Rules in the system prompt: answer **only** from the numbered passages; if they do
  not contain the answer emit a fixed sentinel (for example `INSUFFICIENT_EVIDENCE`);
  every claim carries the ID of the passage it uses; be concise; no outside knowledge.
- **Prefer structured output**: a schema of claims, each with `chunk_ids`
  (`patterns.md` section 4). Citations become validated data, and you get the claim
  list without a separate claim-splitting call. Fall back to inline `[chunk_id]`
  markers parsed with a strict ID-shaped regex (`patterns.md` section 3).
- `temperature=0`, stable passage IDs, question after the context.

### 7.3 Validate citations mechanically
Keep only IDs that exist in the retrieved set; strip invented ones. A sentence or claim
left with no valid citation is unsupported. An answer with no valid citation at all
fails; do not fall back to verifying against every retrieved chunk.

### 7.4 Verify every atomic claim
- Score each claim against **its cited passages only**, with the exact text the
  generator saw. Never truncate the judge's context silently; if it does not fit,
  select relevant spans or abstain.
- **Judging**: ask for a categorical label (supported / partial / unsupported) or use
  an NLI / fact-check model or token log-probabilities. A verbalized "0.3" from a small
  model is coarse and poorly calibrated.
- **Deterministic checks first for exact values**: figures, quantities, dates,
  identifiers, reference numbers. Check that the number occurs in the cited text;
  reserve the LLM judge for meaning.
- **The answer is as good as its weakest claim.** Any claim below `tau` fails the
  answer; averages hide one smuggled-in falsehood.
- **Fail closed**: no claims, a claim-splitter or judge error, an unparseable reply, or
  a timeout all mean abstain, never pass.
- **Separate the judge**: the model that wrote the answer should not be its only judge.
  Use a different model (ideally a different family) where possible, and always its own
  API key and quota so judge traffic never starves production.
- **Validate the judge** on human-labelled faithful/hallucinated pairs (AUROC) before
  trusting it; an unvalidated judge just moves hallucination into the scorecard.
- **Chain-of-Verification repair**: for borderline failures, ask the model to remove
  or soften unsupported claims (keeping citations), re-verify, and abstain if it still
  fails. Never return an unverified revision.

### 7.5 Corrective retrieval loop, bounded
Grade whether the retrieved evidence can answer the question (0-1), using the **same
passages the generator will see** (not truncated snippets):

| Grade | Action |
|---|---|
| high (starting point: 0.7+) | generate |
| borderline (starting point: 0.4-0.7) | refine or decompose the query, re-retrieve, **merge** the new evidence |
| borderline after the last hop | **generate anyway**; the verification gate is the final safety check |
| low (below ~0.4) | abstain without generating |

Cap refinements with a counter in state (for example 3), and never spend a refine call
you will not use. Trade-off to decide explicitly: a low grade on the first hop is often
a bad query that one rewrite fixes, so allowing a single retry raises coverage at extra
cost. Grade cut-offs are model-dependent and need calibration like `tau`.

### 7.6 Abstention is a first-class outcome
- Abstaining on an unanswerable question is a **correct** answer. Design the
  user-facing message (what was not found, next step such as a human handoff).
- Abstain when: routed `no_retrieval`, evidence grade is low, the model emitted the
  sentinel, uncited or unsupported claims, or retrieval/verification errored.
- Log a **distinct reason code** for each: `routed_no_retrieval`, `low_evidence_grade`,
  `model_abstained`, `uncited_sentences`, `no_claims`, `unsupported_claims`,
  `error`, `verified`. Do not report a low-evidence abstention as `model_abstained`;
  you would lose the signal that tells you whether to fix retrieval or the prompt.

### 7.7 Calibrate `tau` on a risk-coverage curve
Sweep the threshold and plot risk against coverage. Two numbers are both called
"hallucination rate"; report both and state which one your budget means:
- **Selective risk**: wrong answers among answered questions, including confident wrong
  answers on *answerable* questions.
- **Unanswerable-answered rate**: unanswerable questions that received an answer.

Choose `tau` on a **dev split**, report on a **held-out split**, and show a confidence
interval (2 hallucinations in 100 questions is roughly 0.5%-7%, not "2%").
High-stakes content (financial, medical, legal, safety) abstains more; low-stakes
content answers more. Re-calibrate when the corpus, models or prompts change. Do not
promise "zero" hallucination; state the measured rate, the eval set and the dial.

### 7.8 Risk tiering and optional signals
- **Tier verification by intent risk.** Full claim verification for financial, legal,
  policy and medical-style answers; a lighter check (citation validation plus exact-value
  checks) for low-risk informational questions. Verifying everything is the cost driver
  in section 9, so make the tiering explicit.
- **Optional uncertainty signal**: sample several answers and measure disagreement
  (semantic entropy). It multiplies generation cost; use it only for high-risk tiers
  where measured gains justify it.

## 8. Evaluation (details in `references/evaluation.md`)

Build a balanced golden set (about half unanswerable), score retrieval, generation and
end-to-end behavior separately, calibrate on dev and report on held-out with confidence
intervals, validate the judge, and gate changes in CI. Read `evaluation.md` before
building any of it.

## 9. Latency, cost and scale

- Profile first. In a verified pipeline **LLM calls dominate**, not vector search:
  router, grader, generator and per-claim judge calls cost more than the ANN lookup.
- Cut model calls before tuning the index: cache query embeddings, grades and verdicts;
  batch reranker pairs and claim judgements; use risk tiers (7.8); skip optional signals.
- Use the cheapest reliable model per role (small for routing, grading and claim
  scoring; larger for synthesis and hard reasoning); upgrade only where evals show
  errors. Model names live in config, not code.
- Tag requests via a gateway or metadata (feature, run ID) so cost and failures are
  attributable, and keep judge and eval traffic on their own key and quota (7.4).
- Precompute and cache static artifacts (embeddings, prefixes), estimate cost before
  bulk LLM passes (section 2), and scale ingest with batching and async workers.
- Streaming: a verified answer cannot be streamed before it is verified. Stream a status
  ("searching", "checking sources") instead, or stream only after verification passes.

## 10. Operations and security (details in `references/operations.md`)

Observe every stage with correlation IDs and reason codes, close the feedback loop from
abstentions, version the index with its config and keep rollback ready, treat retrieved
text as untrusted data, enforce access control at retrieval, and fail closed. Read
`operations.md` before shipping.

## 11. Mapping the pipeline onto a workflow or graph orchestrator

Whatever orchestrator you use (graph framework, workflow engine, plain code), one
step per stage keeps each unit testable and the loop bounded:

`rewrite` -> `route` -> (`no_retrieval`: `canned_reply`) | `retrieve` -> `grade` ->
(`refine` back to `retrieve` while hops remain) | `generate` -> `validate_citations` ->
`verify` -> (`repair` once) -> `respond` | `abstain`.

At the hop limit, borderline evidence flows to `generate` (7.5), low evidence flows to
`abstain`. State keys (typed): `question`, `rewritten_query`, `route`, `candidates`,
`passages`, `evidence_grade`, `hops`, `draft`, `claims`, `claim_scores`,
`outcome_reason`, `final_answer`. Routing steps return only the named next steps; the
hop counter and an overall step limit enforce termination.

## 12. Anti-patterns

- **Process**: tuning prompts before measuring retrieval recall; RAG over data a SQL or
  tool call answers exactly.
- **Evaluation**: answerable-only sets; mean faithfulness instead of per-claim minimum
  and both risk numbers; choosing `tau` on the data you report on; claiming "zero
  hallucination" instead of a measured rate.
- **Ingestion and chunking**: fixed character chunks; the wrong tokenizer; dedup that
  merges versions or variants differing in a number; no way to delete or expire content.
- **Retrieval**: pure dense search, fusing raw scores, mixing query and passage
  embedding modes, stuffing 30 passages, ignoring ANN recall loss, extrapolating scale
  from a synthetic benchmark.
- **Verification**: failing open (an empty claim list or a judge error passes);
  silently truncating the judge's context; judging different text than the generator
  saw; showing the LLM-written prefix as evidence; trusting citations unchecked or
  parsing them with a loose regex; the generator as sole judge.
- **Control flow**: unbounded retrieve-refine loops, a wasted final refine call, no
  abstention path or abstentions without distinct reason codes; letting retrieved text
  act as instructions.
