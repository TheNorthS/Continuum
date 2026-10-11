import os
import uuid

import pytest
from pydantic import SecretStr

from app.main import create_app
from tests.signed_client import SignedClient
from tests.test_security_complete import P, headers, register


@pytest.mark.external
@pytest.mark.skipif(not os.getenv("RAGE_TEST_NEO4J_URI"), reason="Neo4j test service not configured")
def test_real_graph_encryption_private_namespaces_and_revocation(settings):
    settings.graph_enabled = True
    settings.neo4j_uri = os.environ["RAGE_TEST_NEO4J_URI"]
    settings.neo4j_password = SecretStr(os.getenv("RAGE_TEST_NEO4J_PASSWORD", "rag-e-local-neo4j"))
    settings.redis_url = SecretStr(os.getenv("RAGE_TEST_REDIS_URL", ""))
    settings.knowledge_namespace = "security_" + uuid.uuid4().hex
    with SignedClient(create_app(settings)) as client:
        alice, bob = register(client, "graphalice"), register(client, "graphbob")
        a, b = headers(alice), headers(bob)
        base = client.post(P + "/knowledge/bases", headers=a, json={"name": "PRIVATE-GRAPH"}).json()
        doc = client.post(
            P + "/knowledge/documents",
            headers=a,
            data={"base_id": base["id"]},
            files={"file": ("secret.md", "PRIVATE-ENTITY --关联--> SECRET-ENTITY".encode())},
        ).json()
        assert doc["graph_status"] == "ready"
        graph = client.get(P + "/graph/network", headers=a).json()
        assert len(graph["nodes"]) == 2
        assert client.get(P + "/graph/network", headers=b).json()["nodes"] == []
        grant = {"subject_type": "user", "subject_id": bob["user"]["id"], "access": "read"}
        assert (
            client.put(f"{P}/knowledge/bases/{base['id']}/grants", headers=a, json=grant).status_code == 204
        )
        assert len(client.get(P + "/graph/network", headers=b).json()["nodes"]) == 2
        grant["access"] = None
        client.put(f"{P}/knowledge/bases/{base['id']}/grants", headers=a, json=grant)
        assert client.get(P + "/graph/network", headers=b).json()["nodes"] == []
        assert client.post(f"{P}/graph/documents/{doc['id']}/rebuild", headers=b).status_code == 404

        async def inspect():
            services = client.app.state.container
            async with services.graph.store.driver.session() as session:
                rows = await (
                    await session.run(
                        "MATCH (n:RageEntity {namespace:$ns}) RETURN n.name AS name,n.kind AS kind",
                        ns=settings.knowledge_namespace,
                    )
                ).data()
                assert all(n["name"].startswith("rage1:") and n["kind"].startswith("rage1:") for n in rows)
                assert "PRIVATE-ENTITY" not in repr(rows)
            if services.cache.redis:
                keys = [key async for key in services.cache.redis.scan_iter(match="rag-e:secure:*")]
                for key in keys:
                    value = await services.cache.redis.get(key)
                    assert value.startswith(b"rage1:") and b"PRIVATE-ENTITY" not in value

        client.portal.call(inspect)

        async def rotate_graph():
            services = client.app.state.container
            services.cipher.rotate()
            await services.graph.close()
            await services.graph.start()

        client.portal.call(rotate_graph)
        assert len(client.get(P + "/graph/network", headers=a).json()["nodes"]) == 2
        client.delete(f"{P}/knowledge/documents/{doc['id']}", headers=a)

        async def cleanup():
            async with client.app.state.container.graph.store.driver.session() as session:
                await (
                    await session.run(
                        "MATCH (g:RageGraph {namespace:$ns}) DELETE g", ns=settings.knowledge_namespace
                    )
                ).consume()

        client.portal.call(cleanup)
