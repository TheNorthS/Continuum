from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pytest

from app.services.graph_service import GraphService
from app.services.knowledge_service import KnowledgeService, fuse_candidates


def source(cid, text=None, **metadata):
    return {"id": cid, "text": text or cid, "score": 0.8, "metadata": {"document_id": cid, **metadata}}


def test_fusion_counts_once_per_channel_and_preserves_source_information():
    hybrid = [source("a", "primary", filename="original.md"), source("a", "duplicate"), source("b")]
    graph = [
        source("b"),
        source("a", "graph copy", retrieval="graph", graph_recall={"min_hops": 1, "matched_entities": 2}),
    ]
    before = deepcopy((hybrid, graph))
    result = fuse_candidates(hybrid, graph)
    assert [r["id"] for r in result] == ["a", "b"]  # Equal totals; stable ID tie-break.
    assert all(r["score"] == pytest.approx(1 / 61 + 1 / 62) for r in result)
    assert result[0]["text"] == "primary"
    assert result[0]["metadata"]["filename"] == "original.md"
    assert result[0]["metadata"]["retrieval_channels"] == ["hybrid", "graph"]
    details = result[0]["metadata"]["retrieval_details"]
    assert details["hybrid"] == {"rank": 1, "score": 0.8}
    assert details["graph"]["rank"] == 2
    assert details["graph"]["graph_recall"] == {"min_hops": 1, "matched_entities": 2}
    assert result[1]["metadata"]["retrieval_details"]["hybrid"]["rank"] == 2
    result[0]["metadata"]["retrieval_details"]["graph"]["graph_recall"]["min_hops"] = 9
    assert (hybrid, graph) == before  # Do not mutate cached/upstream payloads.


@pytest.mark.parametrize("channel", ["hybrid", "graph"])
def test_single_channel_keeps_scores_order_first_duplicate_and_provenance(channel):
    items = [
        {**source(cid, retrieval=channel), "score": score}
        for cid, score in (("z", 0.032), ("a", 0.032), ("b", 0.0))
    ]
    duplicate = {**source("z", "duplicate"), "score": 0.99}
    ranked = [items[0], duplicate, *items[1:]]
    before = deepcopy(ranked)
    result = fuse_candidates(ranked, []) if channel == "hybrid" else fuse_candidates([], ranked)
    assert [r["id"] for r in result] == ["z", "a", "b"]
    assert [r["score"] for r in result] == [0.032, 0.032, 0.0]
    assert result[0]["text"] == "z"
    for rank, row in enumerate(result, 1):
        assert row["metadata"]["retrieval_channels"] == [channel]
        assert row["metadata"]["retrieval_details"][channel] == {"rank": rank, "score": row["score"]}
    assert ranked == before


def test_empty_channels_and_duplicate_invariance():
    assert fuse_candidates([], []) == []
    items = [source("z"), source("a"), source("b")]
    # Duplicating a result in either channel must not change ranking or scores.
    assert fuse_candidates(items, items[::-1]) == fuse_candidates(
        [items[0], *items], [*items[::-1], items[0]]
    )


@pytest.mark.parametrize("graph_state", ["disabled", "empty", "unavailable"])
async def test_single_channel_passthrough_when_graph_has_no_results(settings, graph_state):
    repo = Mock()
    repo.revision.return_value = "rev"
    repo.filter_ready.return_value = {"z", "a"}
    candidates = [source("z"), source("a")]
    candidates[0]["score"], candidates[1]["score"] = 0.032, 0.029
    cache = Mock(get_or_load=AsyncMock(return_value=candidates))
    service = KnowledgeService(settings, repo, None, None, None, cache)
    service.graph = Mock(enabled=graph_state != "disabled", recall=AsyncMock(return_value=[]))
    if graph_state == "unavailable":
        service.graph.recall.side_effect = RuntimeError("graph unavailable")
    result = await service.candidates("query", 2)
    assert [(r["id"], r["score"]) for r in result] == [("z", 0.032), ("a", 0.029)]
    assert all(r["metadata"]["retrieval_channels"] == ["hybrid"] for r in result)
    if graph_state == "disabled":
        service.graph.recall.assert_not_awaited()


async def test_fusion_filters_all_candidates_before_cutoff(settings):
    repo = Mock()
    repo.revision.return_value = "rev"
    repo.filter_ready.return_value = {"c", "d"}
    cached = [source("a"), source("b"), source("c"), source("d")]
    cache = Mock(get_or_load=AsyncMock(return_value=cached))
    service = KnowledgeService(settings, repo, None, None, None, cache)
    service.graph = Mock(enabled=True, recall=AsyncMock(return_value=[source("a"), source("b")]))
    result = await service.candidates("query", 2)
    assert [r["id"] for r in result] == ["c", "d"]
    assert set(repo.filter_ready.call_args.args[0]) == {"a", "b", "c", "d"}


async def test_graph_preserves_rank_when_sql_reorders_and_drops_revoked_chunks(settings):
    settings.graph_enabled = True
    repo = Mock()
    graph = GraphService(settings, repo, None, None, None)
    graph.context = AsyncMock(return_value=({"ns": "test", "ready": ["doc"]}, "rev"))
    graph.candidate_nodes = AsyncMock(return_value=[{"id": "seed", "name": "星河"}])
    records = [
        {"chunk_id": "z", "matched_entities": 2, "min_hops": 1},
        {"chunk_id": "revoked", "matched_entities": 1, "min_hops": 1},
        {"chunk_id": "a", "matched_entities": 1, "min_hops": 2},
    ]
    graph.store.read = AsyncMock(return_value=records)
    repo.chunks_by_ids.return_value = [{"id": cid, "text": cid, "meta": {}} for cid in ("a", "z")]
    result = await graph.recall("星河的产品", 4)
    assert [r["id"] for r in result] == ["z", "a"]
    assert [r["score"] for r in result] == [1 / 61, 1 / 62]
    assert result[1]["metadata"]["graph_recall"] == {"matched_entities": 1, "min_hops": 2}


async def test_graph_seed_budget_prefers_specific_mentions_not_database_order(settings):
    settings.graph_enabled = True
    graph = GraphService(settings, Mock(), None, None, None)
    graph.context = AsyncMock(return_value=({"ns": "test", "ready": ["doc"]}, "rev"))
    nodes = [{"id": f"{i:02}", "name": "名" * i} for i in range(1, 13)]
    graph.candidate_nodes = AsyncMock(return_value=nodes)
    graph.store.read = AsyncMock(return_value=[])
    await graph.recall("名" * 12, 4)
    expected = [f"{i:02}" for i in range(12, 2, -1)]
    assert graph.store.read.call_args.args[2]["seeds"] == expected
    graph.candidate_nodes.return_value = nodes[::-1]
    await graph.recall("名" * 12, 4)
    assert graph.store.read.call_args.args[2]["seeds"] == expected
    graph.store.read.reset_mock()
    assert await graph.recall("no entity", 4) == []
    assert await graph.recall(" ", 4) == []
    assert await graph.recall("名", 0) == []
    graph.store.read.assert_not_awaited()
