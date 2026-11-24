from checkcord.eval.clustering_metrics import (
    PairScore,
    RankedList,
    confusion_counts,
    mean_reciprocal_rank,
    precision_recall_f1,
    retrieval_recall,
    threshold_sweep,
    top1_accuracy,
)


def _pair_scores() -> list[PairScore]:
    return [
        PairScore(anchor_id="A", candidate_id="B", is_true_positive=True, similarity=0.9),
        PairScore(anchor_id="A", candidate_id="C", is_true_positive=False, similarity=0.9),
        PairScore(anchor_id="A", candidate_id="D", is_true_positive=True, similarity=0.5),
        PairScore(anchor_id="A", candidate_id="E", is_true_positive=False, similarity=0.5),
    ]


def test_confusion_counts_at_threshold_0_85() -> None:
    cm = confusion_counts(_pair_scores(), threshold=0.85)
    counts = (cm.true_positive, cm.false_positive, cm.false_negative, cm.true_negative)
    assert counts == (1, 1, 1, 1)


def test_precision_recall_f1_hand_computed() -> None:
    cm = confusion_counts(_pair_scores(), threshold=0.85)
    result = precision_recall_f1(cm)
    assert result.precision == 0.5
    assert result.recall == 0.5
    assert result.f1 == 0.5
    assert result.accuracy == 0.5


def test_confusion_counts_at_looser_threshold_admits_everything() -> None:
    cm = confusion_counts(_pair_scores(), threshold=0.3)
    counts = (cm.true_positive, cm.false_positive, cm.false_negative, cm.true_negative)
    assert counts == (2, 2, 0, 0)

    result = precision_recall_f1(cm)
    assert result.precision == 0.5
    assert result.recall == 1.0
    assert round(result.f1, 4) == 0.6667
    assert result.accuracy == 0.5


def test_precision_recall_f1_zero_division_resolves_to_zero_not_error() -> None:
    cm = confusion_counts(pair_scores=[], threshold=0.85)
    result = precision_recall_f1(cm)
    assert result.precision == 0.0
    assert result.recall == 0.0
    assert result.f1 == 0.0
    assert result.accuracy == 0.0


def test_precision_recall_f1_all_true_negative_zero_precision_and_recall() -> None:
    """No predicted-same at all (threshold higher than every similarity) ->
    precision is undefined-by-convention 0.0, not a ZeroDivisionError."""
    cm = confusion_counts(_pair_scores(), threshold=0.99)
    assert (cm.true_positive, cm.false_positive) == (0, 0)
    result = precision_recall_f1(cm)
    assert result.precision == 0.0
    assert result.recall == 0.0
    assert result.f1 == 0.0


def test_threshold_sweep_runs_every_threshold_independently() -> None:
    results = threshold_sweep(_pair_scores(), thresholds=[0.3, 0.85])
    assert [r.threshold for r in results] == [0.3, 0.85]
    assert results[0].metrics.recall == 1.0
    assert results[1].metrics.recall == 0.5


def _ranked_lists() -> list[RankedList]:
    return [
        # Retrieval succeeded; true positive not ranked first.
        RankedList(
            anchor_id="A1",
            ranked_candidate_ids=["N1", "P1", "N2"],
            true_positive_ids={"P1", "P2"},
        ),
        # Retrieval succeeded; true positive ranked first.
        RankedList(anchor_id="A2", ranked_candidate_ids=["P3", "N3"], true_positive_ids={"P3"}),
        # Has a known positive, but retrieval failed to surface it at all.
        RankedList(anchor_id="A3", ranked_candidate_ids=["N4", "N5"], true_positive_ids={"P4"}),
        # No known positive anywhere in the corpus -- excluded from every metric.
        RankedList(anchor_id="A4", ranked_candidate_ids=["N6", "N7"], true_positive_ids=set()),
    ]


def test_retrieval_recall_counts_anchors_with_a_positive_anywhere_in_pool() -> None:
    # Eligible: A1, A2, A3 (A4 has no known positive). Hits: A1, A2. -> 2/3.
    assert round(retrieval_recall(_ranked_lists()), 4) == round(2 / 3, 4)


def test_top1_accuracy_only_counts_anchors_where_retrieval_succeeded() -> None:
    # Eligible: A1 (positive present, not first), A2 (positive present, first).
    # A3 excluded (positive never retrieved) -- correct=1 (A2 only) / eligible=2.
    assert top1_accuracy(_ranked_lists()) == 0.5


def test_mean_reciprocal_rank_hand_computed() -> None:
    # A1: first true positive at rank 2 -> 1/2. A2: rank 1 -> 1/1. avg = 0.75.
    assert mean_reciprocal_rank(_ranked_lists()) == 0.75


def test_retrieval_metrics_empty_input_returns_zero_not_error() -> None:
    assert retrieval_recall([]) == 0.0
    assert top1_accuracy([]) == 0.0
    assert mean_reciprocal_rank([]) == 0.0


def test_reranking_can_move_top1_and_mrr_without_touching_retrieval_recall() -> None:
    """The scenario this whole module exists to measure: reranking reorders
    an already-retrieved pool. Retrieval recall (what got retrieved) is
    unchanged; top-1 accuracy and MRR (the order within it) improve."""
    baseline = [
        RankedList(anchor_id="A1", ranked_candidate_ids=["N1", "P1"], true_positive_ids={"P1"}),
    ]
    reranked = [
        RankedList(anchor_id="A1", ranked_candidate_ids=["P1", "N1"], true_positive_ids={"P1"}),
    ]

    assert retrieval_recall(baseline) == retrieval_recall(reranked) == 1.0
    assert top1_accuracy(baseline) == 0.0
    assert top1_accuracy(reranked) == 1.0
    assert mean_reciprocal_rank(baseline) == 0.5
    assert mean_reciprocal_rank(reranked) == 1.0
