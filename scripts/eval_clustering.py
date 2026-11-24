#!/usr/bin/env python3
"""Golden-set evaluation harness for the merge/cluster stage.

Runs against local JSONL files. Embedding calls are always made; Voyage
rerank calls only with --rerank. Both are real API calls.

Two measurements are reported (see checkcord.eval.clustering_metrics):
  1. A threshold sweep (precision/recall/F1/accuracy), independent of
     reranking.
  2. Retrieval and ranking metrics (retrieval recall, top-1 accuracy, MRR),
     which reranking can change.

Usage:
    uv run python scripts/eval_clustering.py \\
        --golden-set golden_sets/clustering.jsonl \\
        --rerank
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from checkcord.core.config import get_settings
from checkcord.eval.clustering_metrics import (
    PairScore,
    RankedList,
    mean_reciprocal_rank,
    retrieval_recall,
    threshold_sweep,
    top1_accuracy,
)
from checkcord.eval.golden_set import GoldenSetRow, filter_extracted_signal, load_golden_set
from checkcord.pipeline.embedder import EmbeddingClient
from checkcord.pipeline.embeddings_factory import build_embeddings_provider
from checkcord.pipeline.reranker import RerankCandidate, Reranker

logger = logging.getLogger("eval_clustering")

DEFAULT_THRESHOLDS = [0.3, 0.5, 0.7, 0.85, 0.9]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return float(dot / (norm_a * norm_b))


def _is_same_cluster(a: GoldenSetRow, b: GoldenSetRow) -> bool:
    return a.cluster_id is not None and a.cluster_id == b.cluster_id


def build_pair_scores(
    rows: list[GoldenSetRow], embeddings: dict[str, list[float]]
) -> list[PairScore]:
    """Every unordered pair in the corpus, for the threshold sweep."""
    pairs = []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a, b = rows[i], rows[j]
            pairs.append(
                PairScore(
                    anchor_id=a.id,
                    candidate_id=b.id,
                    is_true_positive=_is_same_cluster(a, b),
                    similarity=_cosine_similarity(embeddings[a.id], embeddings[b.id]),
                )
            )
    return pairs


def build_baseline_ranked_lists(
    rows: list[GoldenSetRow],
    embeddings: dict[str, list[float]],
    retrieval_threshold: float,
    pool_size: int,
) -> list[RankedList]:
    """Per-anchor candidate pool exactly as ClusteringWorker would build it
    when reranking is on: everything else in the corpus above
    `retrieval_threshold` cosine similarity, capped to `pool_size`, ordered
    best-first.
    """
    ranked_lists = []
    for anchor in rows:
        true_positive_ids = {
            r.id for r in rows if r.id != anchor.id and _is_same_cluster(anchor, r)
        }
        scored = [
            (candidate.id, _cosine_similarity(embeddings[anchor.id], embeddings[candidate.id]))
            for candidate in rows
            if candidate.id != anchor.id
        ]
        scored = [(cid, sim) for cid, sim in scored if sim >= retrieval_threshold]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        scored = scored[:pool_size]
        ranked_lists.append(
            RankedList(
                anchor_id=anchor.id,
                ranked_candidate_ids=[cid for cid, _ in scored],
                true_positive_ids=true_positive_ids,
            )
        )
    return ranked_lists


async def build_reranked_ranked_lists(
    baseline: list[RankedList],
    content_by_id: dict[str, str],
    reranker: Reranker,
) -> list[RankedList]:
    """Rerank each anchor's baseline candidate pool with the real Voyage API.

    A reranker failure for one anchor falls back to that anchor's baseline
    order; the run continues.
    """
    reranked_lists = []
    for base in baseline:
        if not base.ranked_candidate_ids:
            reranked_lists.append(base)
            continue
        candidates = [
            RerankCandidate(id=cid, text=content_by_id[cid]) for cid in base.ranked_candidate_ids
        ]
        try:
            reranked = await reranker.rerank(
                query=content_by_id[base.anchor_id], candidates=candidates
            )
            order = [rc.id for rc in reranked]
        except Exception as e:
            logger.warning(
                "rerank failed for anchor %s, falling back to baseline order: %s",
                base.anchor_id,
                e,
            )
            order = base.ranked_candidate_ids
        reranked_lists.append(
            RankedList(
                anchor_id=base.anchor_id,
                ranked_candidate_ids=order,
                true_positive_ids=base.true_positive_ids,
            )
        )
    return reranked_lists


async def _main_async(args: argparse.Namespace) -> int:
    settings = get_settings()

    load_result = load_golden_set([Path(p) for p in args.golden_set])
    if load_result.errors:
        logger.warning("%d row(s) failed to load:", len(load_result.errors))
        for err in load_result.errors:
            logger.warning("  %s:%d: %s", err.source_file, err.line_number, err.reason)
    if not load_result.rows:
        logger.error("No valid rows loaded from %s", args.golden_set)
        return 1

    rows = filter_extracted_signal(load_result.rows)
    logger.info(
        "Loaded %d rows (%d after filtering to signal+decision)",
        len(load_result.rows),
        len(rows),
    )
    if len(rows) < 2:
        logger.error("Fewer than 2 usable rows after filtering -- nothing to evaluate.")
        return 1

    provider = build_embeddings_provider(settings)
    if provider is None:
        logger.error(
            "No embedding provider configured -- missing API key for embedding_provider=%r",
            settings.embedding_provider,
        )
        return 1
    embedder = EmbeddingClient(provider)
    raw_embeddings = await embedder.embed_texts([row.content for row in rows])
    embeddings: dict[str, list[float]] = {}
    for row, emb in zip(rows, raw_embeddings, strict=True):
        if emb is None:
            logger.warning("Row %s produced no embedding (empty content?) -- excluding it.", row.id)
            continue
        embeddings[row.id] = emb
    rows = [row for row in rows if row.id in embeddings]

    # -- 1. Threshold sweep (rerank-independent) --
    pair_scores = build_pair_scores(rows, embeddings)
    thresholds = [float(t) for t in args.thresholds.split(",")]
    sweep = threshold_sweep(pair_scores, thresholds)

    print("\n=== Threshold sweep (precision/recall/F1/accuracy, rerank-independent) ===")
    for result in sweep:
        m = result.metrics
        cm = result.confusion_matrix
        print(
            f"  threshold={result.threshold:.2f}  precision={m.precision:.3f} recall={m.recall:.3f} "
            f"f1={m.f1:.3f} accuracy={m.accuracy:.3f}  (tp={cm.true_positive} fp={cm.false_positive} "
            f"fn={cm.false_negative} tn={cm.true_negative})"
        )

    # -- 2. Retrieval / ranking metrics --
    baseline = build_baseline_ranked_lists(rows, embeddings, args.rerank_threshold, args.pool_size)
    print(f"\n=== Retrieval @ threshold={args.rerank_threshold}, pool_size={args.pool_size} ===")
    print(f"  retrieval_recall={retrieval_recall(baseline):.3f}")
    print(f"  top1_accuracy (baseline order)={top1_accuracy(baseline):.3f}")
    print(f"  mean_reciprocal_rank (baseline order)={mean_reciprocal_rank(baseline):.3f}")

    output: dict[str, object] = {
        "row_count": len(rows),
        "threshold_sweep": [r.model_dump() for r in sweep],
        "retrieval_recall": retrieval_recall(baseline),
        "top1_accuracy_baseline": top1_accuracy(baseline),
        "mrr_baseline": mean_reciprocal_rank(baseline),
    }

    if args.rerank:
        if not settings.voyage_api_key:
            logger.error("--rerank passed but voyage_api_key is not configured.")
            return 1
        from checkcord.pipeline.reranker import VoyageReranker

        reranker: Reranker = VoyageReranker(
            api_key=settings.voyage_api_key,
            model=args.rerank_model,
            timeout_seconds=settings.rerank_timeout_seconds,
        )
        content_by_id = {row.id: row.content for row in rows}
        reranked = await build_reranked_ranked_lists(baseline, content_by_id, reranker)
        print(f"  top1_accuracy (reranked)={top1_accuracy(reranked):.3f}")
        print(f"  mean_reciprocal_rank (reranked)={mean_reciprocal_rank(reranked):.3f}")
        output["top1_accuracy_reranked"] = top1_accuracy(reranked)
        output["mrr_reranked"] = mean_reciprocal_rank(reranked)
    else:
        print("  (pass --rerank to also measure reranked ordering)")

    if args.output_json:
        Path(args.output_json).write_text(json.dumps(output, indent=2))
        logger.info("Wrote results to %s", args.output_json)

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate clustering/dedup accuracy against a labeled golden set."
    )
    parser.add_argument(
        "--golden-set",
        action="append",
        required=True,
        help="Path to a golden-set JSONL file. Repeatable -- multiple files are concatenated.",
    )
    parser.add_argument(
        "--thresholds",
        default=",".join(str(t) for t in DEFAULT_THRESHOLDS),
        help=f"Comma-separated similarity thresholds to sweep (default {DEFAULT_THRESHOLDS}).",
    )
    parser.add_argument(
        "--rerank-threshold",
        type=float,
        default=0.85,
        help=(
            "Similarity threshold used to build each anchor's candidate pool "
            "for the retrieval/ranking metrics (default 0.85, matching "
            "ClusteringWorker's default)."
        ),
    )
    parser.add_argument(
        "--pool-size",
        type=int,
        default=None,
        help="Candidate pool cap for retrieval/ranking metrics (default: settings.rerank_candidate_pool_size).",
    )
    parser.add_argument(
        "--rerank",
        action="store_true",
        default=False,
        help="Also call the real Voyage rerank API and report reranked-order metrics (default off).",
    )
    parser.add_argument(
        "--rerank-model",
        default=None,
        help="Voyage rerank model id (default: settings.rerank_model).",
    )
    parser.add_argument(
        "--output-json",
        default=None,
        help="Optional path to write the full results as JSON.",
    )
    args = parser.parse_args()

    settings = get_settings()
    if args.pool_size is None:
        args.pool_size = settings.rerank_candidate_pool_size
    if args.rerank_model is None:
        args.rerank_model = settings.rerank_model

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    exit_code = asyncio.run(_main_async(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
