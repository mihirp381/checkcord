from uuid import uuid4

from checkcord.storage.postgres import _RRF_K, _fuse_rrf


def _row(cid: object, **extra: object) -> dict[str, object]:
    return {"id": cid, **extra}


def test_fuse_rrf_candidate_in_both_lists_outranks_single_list_candidates() -> None:
    """A candidate present in both ranked lists accumulates both lists'
    contributions and must outrank a candidate present in only one."""
    a, b, c = uuid4(), uuid4(), uuid4()

    # a: rank 1 in vector only. b: rank 2 in vector, rank 1 in lexical. c: rank 1 in lexical only.
    vector_rows = [_row(a), _row(b)]
    lexical_rows = [_row(b), _row(c)]

    fused = _fuse_rrf(vector_rows, lexical_rows)
    fused_ids = [row["id"] for row in fused]

    assert fused_ids[0] == b  # present in both lists -> highest fused score
    assert set(fused_ids) == {a, b, c}


def test_fuse_rrf_empty_lexical_list_preserves_vector_order() -> None:
    """An empty candidate list (e.g. no lexical match) must not error."""
    a, b = uuid4(), uuid4()
    vector_rows = [_row(a), _row(b)]

    fused = _fuse_rrf(vector_rows, [])

    assert [row["id"] for row in fused] == [a, b]


def test_fuse_rrf_both_lists_empty_returns_empty() -> None:
    assert _fuse_rrf([], []) == []


def test_fuse_rrf_disjoint_lists_lexical_only_candidate_still_included() -> None:
    """A candidate that only appears in the lexical list (never passed the
    vector threshold at all) must still surface in the fused result -- this
    is the exact mechanism that recovers an exact-match case pure-vector
    search would have missed entirely."""
    a, b = uuid4(), uuid4()
    vector_rows = [_row(a)]
    lexical_rows = [_row(b)]

    fused = _fuse_rrf(vector_rows, lexical_rows)

    assert {row["id"] for row in fused} == {a, b}


def test_fuse_rrf_score_formula_matches_reciprocal_rank_sum() -> None:
    """Directly verifies score(id) = sum(1 / (k + rank_i)) across whichever
    ranked lists a candidate appears in, 1-indexed rank: a candidate present
    in both lists (even at a worse individual rank in each) outranks a
    candidate present in only one list at rank 1."""
    assert _RRF_K == 60
    a, b, c = uuid4(), uuid4(), uuid4()

    vector_rows = [_row(a), _row(c)]  # a: rank 1, c: rank 2
    lexical_rows = [_row(b), _row(c)]  # b: rank 1, c: rank 2

    fused = _fuse_rrf(vector_rows, lexical_rows)
    fused_ids = [row["id"] for row in fused]

    expected_score_a = 1.0 / (_RRF_K + 1)
    expected_score_b = 1.0 / (_RRF_K + 1)
    expected_score_c = 1.0 / (_RRF_K + 2) + 1.0 / (_RRF_K + 2)

    assert expected_score_c > expected_score_a
    assert expected_score_c > expected_score_b
    assert fused_ids[0] == c  # present in both lists -> highest fused score
    assert set(fused_ids) == {a, b, c}
