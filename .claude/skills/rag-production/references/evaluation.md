# RAG evaluation

How to measure whether a RAG system is faithful, useful and safe to ship. Companion to
`SKILL.md` sections 7.7 and 8; code helpers (sweep, split, interval) are in
`patterns.md` section 7. Keep unit tests mock-based; the runs described here are offline
eval jobs that make real model calls, so cap and cost them.

## 1. Build the golden set

A golden set is the single most valuable asset in a RAG project. Build it in strata:

| Stratum | Purpose | Notes |
|---|---|---|
| Answerable, single-hop | Baseline recall and faithfulness | Record the gold supporting passages/sentences, not just the answer |
| Answerable, multi-hop | Tests decomposition and merging evidence | Gold evidence spans several documents |
| Unanswerable, out of scope | Tests abstention | Topic the corpus cannot answer |
| Unanswerable, false premise | Tests that no passage supports the premise | "Why was feature X deprecated?" when it never was |
| Near miss | Corpus almost answers (wrong version, adjacent topic) | Catches confident wrong answers |

- Aim for roughly half unanswerable. An answerable-only set rewards confident guessing.
- Generate candidates synthetically from your corpus, then have a person review and fix
  them. Unreviewed synthetic questions leak the wording of the source passage and
  inflate retrieval scores.
- Add real production questions over time (abstentions, thumbs-down, escalations).
- **Prevent leakage.** Keep a held-out set that is never used to tune prompts,
  thresholds or chunking, and never include the same document pair in tuning and
  reporting. If a public benchmark is used, keep one stratum out of everything you tune.

## 2. Metrics by stage

Evaluate stages separately so a failure points at one component.

- **Retrieval**: recall@k at the candidate stage and after rerank (does gold evidence
  appear?), MRR, nDCG. Measure the lift of each layer: dense, then hybrid, then reranked.
- **Generation**: faithfulness (claims supported by the cited context), answer
  correctness against gold, citation precision (cited passages really support the
  claim) and recall, answer relevance.
- **End to end**: coverage, abstention accuracy, both risk numbers (below), latency
  per stage and total, and cost per answered query.

## 3. The 2x2 outcome matrix and the two risk numbers

|  | Answered | Abstained |
|---|---|---|
| Answerable | correct answer, or **wrong answer** | false refusal |
| Unanswerable | **hallucination** | correct abstention |

- **Unanswerable-answered rate** = answered-unanswerable / all unanswerable.
- **Selective risk** = wrong answers / answered, including wrong answers on answerable
  questions. This is the one a user experiences, and it is the one that a metric
  restricted to unanswerable questions would miss.
- **Coverage** = answered answerable / all answerable.
- Abstentions that do not depend on `tau` (routing, poor evidence, errors, model
  sentinel) are `hard_abstain` records. Do not count them as answered when sweeping.

## 4. Calibration without fooling yourself

1. Split the golden set into dev and held-out test, stratified by answerability.
2. Sweep `tau` (and the evidence-grade cut-offs) on dev; pick the threshold with the
   highest coverage within the selective-risk budget. If no non-zero-coverage threshold
   meets the budget, the answer path is not ready.
3. Report the metrics at that threshold on held-out only.
4. **Report confidence intervals.** With 100 unanswerable questions, 2 hallucinations
   has a 95% Wilson interval of roughly 0.5%-7%. A "98% safe" claim on a small set is
   not evidence of a 2% rate. Grow the set, or report the interval.
5. Re-run when the corpus, embedding model, prompts, generator or judge change.

## 5. Validate the judge

The faithfulness verifier is a model too.
- Run it on human-labelled faithful/hallucinated (context, answer) pairs from your own
  domain plus a public set, and report **AUROC** and the operating point for the `tau`
  you chose. A judge only slightly above chance (AUROC well below ~0.8) caps your safety.
- Check for judge bias: leniency toward fluent answers, position effects, sensitivity to
  context length, and agreement between two different judge models.
- If judge and generator are the same model family, expect shared blind spots; prefer a
  different family, or ensemble with an NLI model.
- Spot-check a sample of "verified" answers by hand each release.

## 6. Cost, reproducibility and CI

- **Smoke profile**: a tiny corpus and a dozen questions that exercise every code path
  in minutes. Run it before any full eval and in CI.
- **CI gate**: a small, fixed subset with thresholds on retrieval recall, faithfulness,
  and both risk numbers, using recorded or mocked model responses where possible so CI
  is cheap and deterministic. Run the full set before release.
- **Unit tests** mock the LLM, embedder and vector store; reserve real calls for a few
  small integration tests and these offline eval runs.
- **Freeze config**: one config object (models, chunk sizes, `k`, hops, cut-offs, `tau`,
  seed) printed at the start of every run and written to a run manifest with metrics and
  artifact paths, so any number can be reproduced and any diff attributed.
- Fix random seeds (Python, NumPy, any ML framework) and note that hosted models are not
  perfectly deterministic even at `temperature=0`; average repeat runs for decisions
  near a threshold.
- Isolate eval and judge traffic on its own API key and quota (`SKILL.md` section 9).

## 7. Online evaluation

- Log a sampled slice of production traffic (masking or excluding PII and secrets) and have
  humans label correctness, faithfulness and abstention appropriateness.
- Track abstention rate, mean and minimum claim score, verifier-fail rate, and
  thumbs-down rate over time; alert on shifts (corpus drift shows up here first).
- Feed every confirmed failure back into the golden set so it cannot regress silently.
