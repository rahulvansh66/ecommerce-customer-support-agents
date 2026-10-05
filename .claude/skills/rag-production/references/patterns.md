# RAG production code patterns

Short, framework-agnostic building blocks for the rules in `SKILL.md`, written in
Python with type hints and docstrings. Adapt names, types and style to your project.
Library calls (vector store, reranker, LLM client) are left as injected callables so
the logic stays unit-testable with mocks.

Every default threshold below is a starting point. Calibrate it on your own eval set
(see `evaluation.md`) before relying on it.

## Contents
1. Sentence-packed chunking
2. Reciprocal Rank Fusion
3. Citation validation (regex fallback)
4. Structured, cited claims (preferred)
5. Claim-level verification gate (fails closed)
6. Corrective retrieval loop (bounded)
7. Risk-coverage sweep, split and confidence interval

## 1. Sentence-packed chunking

The regex splitter is a simplification: it breaks on abbreviations ("Dr.", "e.g."). Use
a real sentence segmenter (for example pysbd or spaCy) in production and keep the
packing logic as is.

```python
"""Chunking helpers: pack whole sentences into token-bounded chunks."""

import re
from collections.abc import Callable

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def split_oversize(
    sentence: str, count_tokens: Callable[[str], int], max_tokens: int
) -> list[str]:
    """Split one over-budget sentence into word-aligned pieces within the budget.

    A single word that alone exceeds the budget is emitted as its own piece.

    Args:
        sentence: The sentence whose token count exceeds ``max_tokens``.
        count_tokens: Function returning the token count of a string.
        max_tokens: Token budget per piece.

    Returns:
        Pieces in order; each fits the budget unless it is a single huge word.
    """
    pieces: list[str] = []
    current: list[str] = []
    for word in sentence.split():
        # Close the piece when adding the next word would exceed the budget
        if current and count_tokens(" ".join([*current, word])) > max_tokens:
            pieces.append(" ".join(current))
            current = []
        current.append(word)
    if current:
        pieces.append(" ".join(current))
    return pieces


def pack_sentences(
    text: str,
    count_tokens: Callable[[str], int],
    max_tokens: int = 256,
    overlap_tokens: int = 32,
) -> list[str]:
    """Pack whole sentences into chunks of at most ``max_tokens`` tokens.

    Splitting on sentence boundaries keeps answer-bearing sentences intact, and
    counting with the generator's tokenizer prevents hidden context overflow. A
    sentence longer than the budget is split so no chunk silently overflows.

    Args:
        text: Normalized document text.
        count_tokens: Function returning the token count of a string, backed by the
            generator model's tokenizer.
        max_tokens: Token budget per chunk (a starting point; tune by retrieval
            recall).
        overlap_tokens: Approximate tokens of trailing sentences repeated at the
            start of the next chunk.

    Returns:
        Chunk strings in document order.
    """
    # Count each sentence once and split any that exceed the budget on their own
    units: list[tuple[str, int]] = []
    for sentence in (s for s in _SENTENCE_SPLIT.split(text) if s.strip()):
        n = count_tokens(sentence)
        if n <= max_tokens:
            units.append((sentence, n))
        else:
            for piece in split_oversize(sentence, count_tokens, max_tokens):
                units.append((piece, count_tokens(piece)))

    chunks: list[str] = []
    current: list[tuple[str, int]] = []
    current_tokens = 0
    for sentence, n in units:
        if current and current_tokens + n > max_tokens:
            chunks.append(" ".join(s for s, _ in current))
            # Seed the next chunk with trailing sentences as overlap
            carry: list[tuple[str, int]] = []
            carried = 0
            for prev, p in reversed(current):
                if carried + p > overlap_tokens:
                    break
                carry.insert(0, (prev, p))
                carried += p
            # Drop overlap that would push the incoming sentence over budget
            while carry and carried + n > max_tokens:
                carried -= carry.pop(0)[1]
            current, current_tokens = carry, carried
        current.append((sentence, n))
        current_tokens += n

    if current:
        chunks.append(" ".join(s for s, _ in current))
    return chunks
```

## 2. Reciprocal Rank Fusion

```python
"""Rank fusion for hybrid (dense + sparse) retrieval."""


def reciprocal_rank_fusion(
    rankings: list[list[str]],
    k: int = 60,
    top_n: int = 150,
) -> list[str]:
    """Fuse several ranked ID lists using ranks, not raw scores.

    Dense and BM25 scores sit on different scales, so only ranks are comparable. An ID
    ranked well in several lists outranks one favored by a single retriever.

    Args:
        rankings: One ranked list of chunk IDs per retriever, best first.
        k: Damping constant; larger values flatten the influence of top ranks.
        top_n: Number of fused IDs to return.

    Returns:
        Chunk IDs ordered by fused score, best first, at most ``top_n``.
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores, key=scores.__getitem__, reverse=True)[:top_n]
```

Pipeline shape: `dense_top(N) + bm25_top(N) -> RRF -> cross-encoder rerank -> top 5-20`.

## 3. Citation validation (regex fallback)

Prefer structured output (section 4). If you must parse free text, match **only** the
exact shape of your chunk IDs so ordinary brackets such as `[Note: runs small]` or
`[1]` are never mistaken for citations.

```python
"""Post-generation citation checks for free-text answers."""

import re
from collections.abc import Collection

_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?!\[)")


def _citation_regex(id_pattern: str) -> re.Pattern[str]:
    """Build a regex matching ``[id]`` or ``[id1, id2]`` for the given ID shape.

    Args:
        id_pattern: Regex for ONE chunk ID, written without capturing groups, for
            example ``r"[0-9a-f]{12}"``.

    Returns:
        Compiled pattern whose group 1 is the comma-separated ID list.
    """
    return re.compile(rf"\[\s*({id_pattern}(?:\s*,\s*{id_pattern})*)\s*\]")


def strip_invalid_citations(
    answer: str, valid_ids: Collection[str], id_pattern: str
) -> tuple[str, int]:
    """Remove citation IDs that do not refer to a retrieved chunk.

    Valid IDs inside a mixed list such as ``[good, invented]`` are kept.

    Args:
        answer: Raw model answer containing ``[chunk_id]`` markers.
        valid_ids: IDs of the chunks actually supplied as context.
        id_pattern: Regex for one chunk ID (no capturing groups); there is no safe
            default because it depends on your ID format.

    Returns:
        Tuple of the cleaned answer and the number of invented IDs removed.
    """
    pattern = _citation_regex(id_pattern)
    removed = 0

    def _rewrite(match: re.Match[str]) -> str:
        """Keep only the valid IDs of one bracket; drop the bracket if none remain."""
        nonlocal removed
        ids = [i.strip() for i in match.group(1).split(",")]
        kept = [i for i in ids if i in valid_ids]
        removed += len(ids) - len(kept)
        return f"[{', '.join(kept)}]" if kept else ""

    cleaned = pattern.sub(_rewrite, answer)
    # Tidy the whitespace left behind by removed markers
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([.,;!?])", r"\1", cleaned)
    return cleaned.strip(), removed


def uncited_sentences(
    answer: str, valid_ids: Collection[str], id_pattern: str
) -> list[str]:
    """List sentences that carry no valid citation (run after stripping).

    Args:
        answer: Answer text after ``strip_invalid_citations``.
        valid_ids: IDs of the chunks actually supplied as context.
        id_pattern: Regex for one chunk ID (no capturing groups).

    Returns:
        Sentences with no valid citation; treat each as unsupported.
    """
    pattern = _citation_regex(id_pattern)
    missing: list[str] = []
    for sentence in _SENTENCE_BREAK.split(answer.strip()):
        if not sentence.strip():
            continue
        ids = [i.strip() for m in pattern.finditer(sentence) for i in m.group(1).split(",")]
        if not any(i in valid_ids for i in ids):
            missing.append(sentence)
    return missing
```

An answer with no valid citation at all fails; do not fall back to "verify against all
retrieved chunks".

## 4. Structured, cited claims (preferred)

Ask the model for a structured answer instead of prose with inline markers. Citations
become data you can validate, and the claim list is produced at generation time, so you
do not need a separate claim-splitting call (which can itself fail or drop claims). Use
whatever structured-output or JSON-schema feature your stack offers, and validate the
reply in your own code.

**Schema (plain description)**

- `abstain` (boolean): true when the context does not contain the answer. When true,
  `claims` must be empty.
- `claims` (list, in presentation order). Each claim has:
  - `text` (string): one atomic, independently checkable factual statement.
  - `chunk_ids` (list of strings): IDs of the retrieved chunks the claim relies on, at
    least one.

```json
{
  "abstain": false,
  "claims": [
    {"text": "First atomic statement.", "chunk_ids": ["a1b2c3"]},
    {"text": "Second atomic statement.", "chunk_ids": ["a1b2c3", "d4e5f6"]}
  ]
}
```

**Validation after parsing, in order**

1. Malformed output (invalid JSON, missing fields, wrong types) gets at most one retry,
   then abstain.
2. `abstain: true` together with claims, or `abstain: false` with no claims, is
   malformed.
3. Remove every chunk ID that is not in the retrieved set.
4. A claim left with no valid ID is unsupported and fails the answer. Drop it and carry
   on only if you deliberately present partial answers.
5. Verify each remaining claim against only the chunks it cites (section 5).
6. Render the user-facing text yourself from the verified claims, so the user never sees
   wording that was not checked.

## 5. Claim-level verification gate (fails closed)

The answer is only as good as its weakest claim. Every failure path (no claims,
uncited sentences, unparseable judge output, scorer error) must end in abstention, never
in a pass.

```python
"""Claim-level faithfulness gate: the answer is only as good as its weakest claim."""

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

logger = logging.getLogger(__name__)

INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
LABEL_SCORES = {"supported": 1.0, "partial": 0.5, "unsupported": 0.0}
_LABEL = re.compile(r"\b(unsupported|partial|supported)\b", re.IGNORECASE)
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


@dataclass(frozen=True)
class Verdict:
    """Outcome of the verification gate.

    Attributes:
        passed: Whether every claim met the threshold.
        min_score: Lowest claim support score (0.0 when nothing could be verified).
        reason: Outcome reason code for audit logs.
    """

    passed: bool
    min_score: float
    reason: str


def parse_judge_label(raw: str) -> float:
    """Map a categorical judge reply to a support score, failing closed.

    Categorical labels are more stable than a verbalized number from a small model.

    Args:
        raw: Judge output expected to contain ``supported``, ``partial`` or
            ``unsupported``.

    Returns:
        1.0, 0.5 or 0.0; 0.0 when no label is found.
    """
    match = _LABEL.search(raw)
    return LABEL_SCORES[match.group(1).lower()] if match else 0.0


def numbers_supported(claim: str, context: str) -> bool:
    """Deterministically check that every number in a claim appears in the context.

    Cheaper and more reliable than an LLM judge for figures, quantities, dates and
    identifiers. It does not understand spelled-out numbers, units or derived values, so a
    claim that computes a value still needs the judge.

    Args:
        claim: The atomic claim text.
        context: Text of the cited passages.

    Returns:
        True when the claim has no numbers or all of them occur in the context.
    """
    def norm(value: str) -> str:
        """Strip thousands separators so ``1,299`` equals ``1299``."""
        return value.replace(",", "").rstrip(".")

    context_numbers = {norm(n) for n in _NUMBER.findall(context)}
    return all(norm(n) in context_numbers for n in _NUMBER.findall(claim))


def verify_answer(
    answer: str,
    context: str,
    claims: Sequence[str],
    score_claim: Callable[[str, str], float],
    tau: float,
    uncited: Sequence[str] = (),
    exact_check: Callable[[str, str], bool] | None = None,
) -> Verdict:
    """Score every atomic claim against the cited context and gate on the minimum.

    Args:
        answer: Citation-validated draft answer (checked for the abstention sentinel).
        context: Text of the cited passages only, passed to the judge untruncated.
        claims: Atomic claims, from the structured output or a claim splitter.
        score_claim: Returns 0.0-1.0 support of a claim given the context (judge
            label via ``parse_judge_label``, or an NLI model).
        tau: Minimum acceptable claim score. Calibrate on a dev split; there is no
            safe universal default.
        uncited: Sentences without a valid citation; any entry fails the answer.
        exact_check: Optional deterministic check such as ``numbers_supported``; a
            claim that fails it scores 0.0 without calling the judge.

    Returns:
        A ``Verdict``; ``passed`` is False for the abstention sentinel, uncited
        sentences, an empty claim list, or any claim scoring below ``tau``.

    Raises:
        Exception: Anything raised by ``score_claim`` or ``exact_check`` propagates.
            Callers must catch it and abstain (fail closed), never pass the answer.
    """
    if INSUFFICIENT_EVIDENCE in answer:
        return Verdict(False, 0.0, "model_abstained")
    if uncited:
        return Verdict(False, 0.0, "uncited_sentences")
    # An empty claim list means verification did not happen, so it cannot pass
    if not claims:
        return Verdict(False, 0.0, "no_claims")

    scores = [
        0.0
        if exact_check is not None and not exact_check(claim, context)
        else score_claim(claim, context)
        for claim in claims
    ]
    min_score = min(scores)

    # Count and score only; never log claim text (may contain PII)
    logger.info(
        "node=verify claims=%d min_score=%.2f tau=%.2f", len(claims), min_score, tau
    )
    if min_score < tau:
        return Verdict(False, min_score, "unsupported_claims")
    return Verdict(True, min_score, "verified")
```

Judge prompt shape: "Reply with exactly one word: supported if the context clearly
states or entails the claim, partial if it supports only part, unsupported if it
contradicts the claim or does not mention it." Pass the full cited context; if it does
not fit, select the relevant spans or abstain. Never truncate it silently. Run the judge
on a different model where possible and always on its own key and quota.

Repair step: on `unsupported_claims` in a borderline band, ask the model to remove or
soften unsupported claims keeping citations, re-run `verify_answer` once, and abstain if
it still fails.

## 6. Corrective retrieval loop (bounded)

```python
"""Bounded corrective retrieval: grade evidence, refine, or abstain."""

from collections.abc import Callable


def retrieve_with_correction(
    question: str,
    retrieve: Callable[[str], list[str]],
    grade: Callable[[str, list[str]], float],
    refine: Callable[[str, list[str]], str],
    max_hops: int = 3,
    grade_ok: float = 0.7,
    grade_refine: float = 0.4,
) -> tuple[list[str], str]:
    """Retrieve, grade the evidence, and refine the query a bounded number of times.

    Passages from every hop are merged so earlier good evidence is never lost. At the
    hop limit, borderline evidence still goes to generation, because the verification
    gate (not the grader) is the final safety check; only clearly poor evidence
    abstains.

    Args:
        question: The user question (already rewritten to be standalone).
        retrieve: Returns passages for a query (rerank inside, so the list is the
            set the generator will see).
        grade: Returns 0-1 for how well the passages can answer the question. It must
            see the same passages the generator will see, not truncated snippets.
        refine: Produces a better query (for example sub-questions joined) from the
            question and the current passages.
        max_hops: Maximum number of refinements after the first retrieval.
        grade_ok: Grade at or above which evidence is sufficient. Calibrate it.
        grade_refine: Grade below which evidence is too poor to refine or generate.

    Returns:
        Tuple of merged passages and a decision: ``"generate"`` or ``"abstain"``.
    """
    query = question
    passages: list[str] = []
    for hop in range(max_hops + 1):
        # Merge across hops, keeping first-seen order and dropping duplicates
        passages = list(dict.fromkeys([*passages, *retrieve(query)]))
        score = grade(question, passages)
        if score >= grade_ok:
            return passages, "generate"
        if score < grade_refine:
            return passages, "abstain"
        # Borderline: refine only while hops remain, so no refine call is wasted
        if hop == max_hops:
            return passages, "generate"
        query = refine(question, passages)
    return passages, "abstain"
```

Trade-off to decide explicitly: a poor grade on the *first* hop usually means a bad
query, which one refinement often fixes. Abstaining immediately saves cost; allowing one
retry raises coverage. Pick by measuring coverage and cost on your eval set.

In a graph or workflow orchestrator, express this as `retrieve -> grade -> conditional
branch` with a `hops` counter in state rather than a Python loop; the logic is identical.

## 7. Risk-coverage sweep, split and confidence interval

Two different "hallucination" numbers are in use; report both and say which your budget
refers to:

- **Selective risk**: wrong answers among answered questions (includes confident wrong
  answers on answerable questions).
- **Unanswerable-answered rate**: unanswerable questions that received an answer.

```python
"""Evaluation helpers: pick tau from a risk-coverage curve on held-out data."""

import math
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class EvalRecord:
    """One evaluated question.

    Attributes:
        answerable: Whether the corpus can answer the question.
        hard_abstain: True when the system abstained for a reason independent of
            ``tau`` (route, model sentinel, poor evidence, error).
        min_score: Verifier minimum claim score for the draft answer.
        correct: Whether the draft matched the gold answer (False if unanswerable).
    """

    answerable: bool
    hard_abstain: bool
    min_score: float
    correct: bool


@dataclass(frozen=True)
class SweepPoint:
    """Metrics at one threshold.

    Attributes:
        tau: The threshold; an answer is emitted when ``min_score >= tau``.
        coverage: Answered answerable questions / all answerable questions.
        selective_risk: Wrong answers / answered questions (0.0 if none answered).
        unanswerable_answered_rate: Answered unanswerable / all unanswerable.
    """

    tau: float
    coverage: float
    selective_risk: float
    unanswerable_answered_rate: float


def sweep_tau(records: list[EvalRecord], steps: int = 21) -> list[SweepPoint]:
    """Compute coverage and both risk measures across thresholds.

    Args:
        records: Evaluated questions with verifier scores.
        steps: Number of evenly spaced thresholds from 0.0 to 1.0.

    Returns:
        One ``SweepPoint`` per threshold, in increasing ``tau``.
    """
    answerable = [r for r in records if r.answerable]
    unanswerable = [r for r in records if not r.answerable]
    points: list[SweepPoint] = []
    for i in range(steps):
        tau = i / (steps - 1)
        answered = [r for r in records if not r.hard_abstain and r.min_score >= tau]
        # Wrong means unanswerable, or answerable with an incorrect answer
        wrong = [r for r in answered if not r.answerable or not r.correct]
        points.append(
            SweepPoint(
                tau=tau,
                coverage=sum(r.answerable for r in answered) / max(len(answerable), 1),
                selective_risk=len(wrong) / len(answered) if answered else 0.0,
                unanswerable_answered_rate=(
                    sum(not r.answerable for r in answered) / max(len(unanswerable), 1)
                ),
            )
        )
    return points


def choose_tau(points: list[SweepPoint], max_selective_risk: float) -> float | None:
    """Pick the threshold with the most coverage within the risk budget.

    Fit this on a dev split, then report the metrics at the chosen tau on a held-out
    split; choosing and reporting on the same data overstates safety.

    Args:
        points: Output of ``sweep_tau`` computed on the dev split.
        max_selective_risk: Maximum acceptable selective risk, for example 0.05.

    Returns:
        The chosen tau (ties go to the higher, safer tau), or ``None`` when no
        threshold with non-zero coverage meets the budget; in that case do not ship
        the answer path. A threshold that answers nothing has zero risk and is not a
        valid choice.
    """
    ok = [
        p for p in points if p.coverage > 0 and p.selective_risk <= max_selective_risk
    ]
    return max(ok, key=lambda p: (p.coverage, p.tau)).tau if ok else None


def split_dev_test(
    records: list[EvalRecord], seed: int = 42, dev_fraction: float = 0.5
) -> tuple[list[EvalRecord], list[EvalRecord]]:
    """Split records into dev and held-out test sets, stratified by answerability.

    Args:
        records: All evaluated questions.
        seed: Seed for a reproducible shuffle.
        dev_fraction: Share of each stratum placed in the dev split.

    Returns:
        Tuple ``(dev, test)``.
    """
    rng = random.Random(seed)
    dev: list[EvalRecord] = []
    test: list[EvalRecord] = []
    for flag in (True, False):
        stratum = [r for r in records if r.answerable is flag]
        rng.shuffle(stratum)
        cut = round(len(stratum) * dev_fraction)
        dev.extend(stratum[:cut])
        test.extend(stratum[cut:])
    return dev, test


def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a rate of ``k`` events in ``n`` trials.

    Small eval sets give wide intervals: 2 hallucinations in 100 unanswerable
    questions is about 0.5%-7%, not "2%".

    Args:
        k: Number of events (for example hallucinated answers).
        n: Number of trials (for example unanswerable questions).
        z: Normal quantile; 1.96 gives a 95% interval.

    Returns:
        Tuple ``(low, high)``; ``(0.0, 1.0)`` when ``n`` is zero.
    """
    if n == 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return max(0.0, center - half), min(1.0, center + half)
```
