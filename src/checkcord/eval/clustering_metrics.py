"""Pure metrics for the golden-set evaluation harness.

No I/O, no embedding or reranker calls; `scripts/eval_clustering.py` is
the caller that wires these to real embeddings and reranking.

Two kinds of measurement:

- `threshold_sweep`, `confusion_counts`, `precision_recall_f1`: whether a
  candidate clears the similarity threshold. Reranking does not change
  this, so these are identical with reranking on or off.
- `retrieval_recall`, `top1_accuracy`, `mean_reciprocal_rank`: candidate
  order, which is what reranking changes and what the linkage worker's
  first-positive-match loop depends on.
"""

from __future__ import annotations

from pydantic import BaseModel


class PairScore(BaseModel):
    """One (anchor, candidate) comparison: is `candidate` a true cluster-mate
    of `anchor`, and what similarity score did retrieval give the pair.
    """

    anchor_id: str
    candidate_id: str
    is_true_positive: bool
    similarity: float


class ConfusionMatrix(BaseModel):
    threshold: float
    true_positive: int
    false_positive: int
    false_negative: int
    true_negative: int


class PRF1(BaseModel):
    precision: float
    recall: float
    f1: float
    accuracy: float


def confusion_counts(pair_scores: list[PairScore], threshold: float) -> ConfusionMatrix:
    """Classify every pair as predicted-same iff `similarity >= threshold`."""
    tp = fp = fn = tn = 0
    for pair in pair_scores:
        predicted_same = pair.similarity >= threshold
        if predicted_same and pair.is_true_positive:
            tp += 1
        elif predicted_same and not pair.is_true_positive:
            fp += 1
        elif not predicted_same and pair.is_true_positive:
            fn += 1
        else:
            tn += 1
    return ConfusionMatrix(
        threshold=threshold,
        true_positive=tp,
        false_positive=fp,
        false_negative=fn,
        true_negative=tn,
    )


def precision_recall_f1(cm: ConfusionMatrix) -> PRF1:
    """Standard precision/recall/F1/accuracy from a confusion matrix.

    Zero-division cases resolve to 0.0 rather than raising -- an empty or
    degenerate slice of the golden set (e.g. a threshold so high nothing
    clears it) is a valid, reportable measurement, not an error.
    """
    tp, fp, fn, tn = cm.true_positive, cm.false_positive, cm.false_negative, cm.true_negative
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    total = tp + fp + fn + tn
    accuracy = (tp + tn) / total if total > 0 else 0.0
    return PRF1(precision=precision, recall=recall, f1=f1, accuracy=accuracy)


class ThresholdResult(BaseModel):
    threshold: float
    confusion_matrix: ConfusionMatrix
    metrics: PRF1


def threshold_sweep(pair_scores: list[PairScore], thresholds: list[float]) -> list[ThresholdResult]:
    """Compute precision/recall/F1/accuracy at each threshold in `thresholds`.

    Pass several thresholds, e.g. the clustering and MCP-search defaults, to
    compare them on identical data.
    """
    results = []
    for threshold in thresholds:
        cm = confusion_counts(pair_scores, threshold)
        results.append(
            ThresholdResult(
                threshold=threshold, confusion_matrix=cm, metrics=precision_recall_f1(cm)
            )
        )
    return results


class RankedList(BaseModel):
    """One anchor's already-threshold-gated candidate pool, in retrieval order.

    `ranked_candidate_ids` is best-first (baseline: descending cosine
    similarity; reranked: descending Voyage relevance score).
    `true_positive_ids` is the set of candidate ids that are true
    cluster-mates of the anchor -- not necessarily all present in
    `ranked_candidate_ids` (a true cluster-mate can fail to clear the
    similarity threshold or the candidate-pool cap; that's a retrieval
    failure, which `retrieval_recall` reports separately from ranking
    quality).
    """

    anchor_id: str
    ranked_candidate_ids: list[str]
    true_positive_ids: set[str]

    @property
    def has_known_positive(self) -> bool:
        """Whether this anchor has at least one true cluster-mate anywhere
        in the golden set (not necessarily retrieved). Anchors with none are
        excluded from recall/ranking metrics below -- they have nothing to
        find, so "did we find it" is undefined for them, not zero.
        """
        return len(self.true_positive_ids) > 0


def retrieval_recall(ranked_lists: list[RankedList]) -> float:
    """Fraction of anchors-with-a-known-positive whose candidate pool
    actually contains >=1 true cluster-mate anywhere in it. Governed by
    the similarity threshold and candidate-pool size, not by reranking
    (reranking only reorders what's already in the pool).
    """
    eligible = [rl for rl in ranked_lists if rl.has_known_positive]
    if not eligible:
        return 0.0
    hits = sum(
        1 for rl in eligible if any(cid in rl.true_positive_ids for cid in rl.ranked_candidate_ids)
    )
    return hits / len(eligible)


def _retrieval_succeeded(rl: RankedList) -> bool:
    return rl.has_known_positive and any(
        cid in rl.true_positive_ids for cid in rl.ranked_candidate_ids
    )


def top1_accuracy(ranked_lists: list[RankedList]) -> float:
    """Among anchors whose pool actually contains >=1 true cluster-mate,
    fraction where the #1-ranked candidate is one of them -- i.e. whether
    `LinkageWorker`'s first-positive-match loop would see the right answer
    first. This is the metric reranking is meant to move.
    """
    eligible = [rl for rl in ranked_lists if _retrieval_succeeded(rl)]
    if not eligible:
        return 0.0
    correct = sum(
        1
        for rl in eligible
        if rl.ranked_candidate_ids and rl.ranked_candidate_ids[0] in rl.true_positive_ids
    )
    return correct / len(eligible)


def mean_reciprocal_rank(ranked_lists: list[RankedList]) -> float:
    """Average of 1/rank-of-first-true-positive, over anchors whose pool
    contains >=1 true cluster-mate. 1.0 is perfect (always first); lower is
    worse. Rank is 1-indexed.
    """
    eligible = [rl for rl in ranked_lists if _retrieval_succeeded(rl)]
    if not eligible:
        return 0.0
    total = 0.0
    for rl in eligible:
        for rank, candidate_id in enumerate(rl.ranked_candidate_ids, start=1):
            if candidate_id in rl.true_positive_ids:
                total += 1.0 / rank
                break
    return total / len(eligible)
