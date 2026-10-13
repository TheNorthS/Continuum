import asyncio
import os
import uuid

import pytest
from pydantic import SecretStr

from app.infrastructure.container import Container
from app.knowledge.graph.extraction import entity_id


@pytest.mark.external
@pytest.mark.skipif(not os.getenv("RAGE_TEST_NEO4J_URI"), reason="test Neo4j URI not configured")
async def test_neo4j_extraction_paths_retrieval_cache_and_deletion(settings):
    settings.graph_enabled = True
    settings.neo4j_uri = os.environ["RAGE_TEST_NEO4J_URI"]
    settings.neo4j_password = SecretStr(os.getenv("RAGE_TEST_NEO4J_PASSWORD", "rag-e-local-neo4j"))
    settings.knowledge_namespace = "test_" + uuid.uuid4().hex
    settings.redis_url = SecretStr(os.getenv("RAGE_TEST_REDIS_URL", ""))
    services = Container(settings)
    docs = []
    try:
        await services.start()
        for name, text in [
            ("biology.md", "研究团队 --发现--> 蛋白质A\n蛋白质A --参与--> 免疫调节"),
            ("business.md", "星河科技 --研发--> 产品B\n产品B --服务于--> 企业管理"),
        ]:
            doc = await services.knowledge.ingest(name, text.encode())
            docs.append(doc["id"])
            assert doc["graph_status"] == "ready"
        network = await services.graph.network()
        assert len(network["nodes"]) == 6 and len(network["links"]) == 4
        before = services.cache.counts["l1_hit"]
        assert await services.graph.network() == network
        assert services.cache.counts["l1_hit"] > before
        if services.cache.redis:
            services.cache.local.clear()
            before = services.cache.counts["l2_hit"]
            assert await services.graph.network() == network
            assert services.cache.counts["l2_hit"] > before
        paths = await services.graph.paths(
            entity_id(settings.knowledge_namespace + ":legacy", "研究团队"),
            entity_id(settings.knowledge_namespace + ":legacy", "免疫调节"),
            2,
        )
        assert paths["paths"] and len(paths["paths"][0]["links"]) == 2
        recall = await services.graph.recall("研究团队", 4)
        assert recall and "免疫调节" in recall[0]["text"]
        await services.graph.index(docs[0])
        assert len((await services.graph.network())["links"]) == 4
        assert (await services.graph.network("' OR true RETURN n //"))["nodes"] == []
        await services.knowledge.delete(docs.pop(0))
        network = await services.graph.network()
        assert len(network["nodes"]) == 3 and all(n["name"] != "研究团队" for n in network["nodes"])
        assert await services.graph.recall("研究团队", 4) == []
        assert await asyncio.to_thread(services.repository.stats)
    finally:
        for document in docs:
            await services.knowledge.delete(document)
        if services.graph.store.driver:
            async with services.graph.store.driver.session() as session:
                result = await session.run(
                    "MATCH (g:RageGraph {namespace:$ns}) DELETE g", ns=settings.knowledge_namespace
                )
                await result.consume()
        await services.close()


@pytest.mark.external
@pytest.mark.skipif(not os.getenv("RAGE_TEST_NEO4J_URI"), reason="test Neo4j URI not configured")
async def test_graph_recall_ranks_entity_coverage_and_distance_before_limit(settings, monkeypatch):
    settings.graph_enabled = True
    settings.graph_retrieval_hops = 2
    settings.neo4j_uri = os.environ["RAGE_TEST_NEO4J_URI"]
    settings.neo4j_password = SecretStr(os.getenv("RAGE_TEST_NEO4J_PASSWORD", "rag-e-local-neo4j"))
    settings.knowledge_namespace = "ranking_" + uuid.uuid4().hex
    services = Container(settings)
    documents = {}
    try:
        await services.start()
        for name, text in (
            ("both", "星河 --关联--> 汇聚\n海岚 --关联--> 汇聚"),
            ("direct", "星河 --关联--> 中转"),
            ("far", "中转 --关联--> 终端"),
            ("tie", "星河 --关联--> 旁支"),
        ):
            doc = await services.knowledge.ingest(name + ".md", text.encode())
            documents[name] = doc["id"]
            assert doc["graph_status"] == "ready"
        # Force SQL hydration into the opposite order: ranking must come from Cypher.
        original = services.repository.chunks_by_ids

        def reverse_hydration(ids):
            by_id = {c["id"]: c for c in original(ids)}
            return [by_id[cid] for cid in reversed(ids) if cid in by_id]

        monkeypatch.setattr(services.repository, "chunks_by_ids", reverse_hydration)
        result = await services.graph.recall(
            "星河 海岚", 1
        )  # Fourfold overfetch, ranked before SQL hydration.
        expected = [
            documents["both"] + "_0",
            *sorted(documents[n] + "_0" for n in ("direct", "tie")),
            documents["far"] + "_0",
        ]
        assert [r["id"] for r in result] == expected
        assert result[0]["metadata"]["graph_recall"] == {"matched_entities": 2, "min_hops": 1}
        assert result[-1]["metadata"]["graph_recall"] == {"matched_entities": 1, "min_hops": 2}
        assert await services.graph.recall("星河 海岚", 1) == result
        single = await services.graph.recall("星河", 4)
        assert len(single) == 4
        assert all(r["metadata"]["graph_recall"]["matched_entities"] == 1 for r in single)
        assert single[-1]["id"] == documents["far"] + "_0"
    finally:
        for document in documents.values():
            await services.knowledge.delete(document)
        if services.graph.store.driver:
            async with services.graph.store.driver.session() as session:
                await (
                    await session.run(
                        "MATCH (g:RageGraph {namespace:$ns}) DELETE g", ns=settings.knowledge_namespace
                    )
                ).consume()
        await services.close()
