import hashlib
import json
import uuid

from neo4j import AsyncGraphDatabase, Query, unit_of_work

from app.common.errors import AppError


class Neo4jStore:
    """One pooled async driver per application; bounded, parameterized Cypher templates."""

    def __init__(self, settings, cache, tracing):
        self.settings, self.cache, self.tracing = settings, cache, tracing
        self.driver = None

    async def start(self):
        s = self.settings
        self.driver = AsyncGraphDatabase.driver(
            s.neo4j_uri,
            auth=(s.neo4j_username, s.neo4j_password.get_secret_value()),
            max_connection_pool_size=s.neo4j_pool_size,
            connection_acquisition_timeout=s.graph_timeout,
            connection_timeout=s.graph_timeout,
            max_transaction_retry_time=s.graph_timeout,
        )
        await self.driver.verify_connectivity()
        for statement in (
            "CREATE CONSTRAINT rage_entity IF NOT EXISTS FOR (n:RageEntity) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT rage_graph_revision IF NOT EXISTS FOR (n:RageGraph) REQUIRE n.namespace IS UNIQUE",
        ):
            async with self.driver.session(database=s.neo4j_database) as session:
                result = await session.run(Query(statement, timeout=s.graph_timeout))
                await result.consume()

        # Existing plaintext graph is migrated in-place; no source documents or edges are discarded.
        async with self.driver.session(database=s.neo4j_database) as session:
            nodes = await (
                await session.run(
                    "MATCH (n:RageEntity {namespace:$ns}) RETURN n.id AS id,n.name AS name,n.kind AS kind",
                    ns=s.knowledge_namespace,
                )
            ).data()
            for n in nodes:
                if not n["name"].startswith("rage1:") or n["name"].split(":")[1] != self.cipher.active:
                    original_name = (
                        self.cipher.decrypt(n["name"], "graph.name")
                        if n["name"].startswith("rage1:")
                        else n["name"]
                    )
                    original_kind = (
                        self.cipher.decrypt(n["kind"], "graph.kind")
                        if n["kind"].startswith("rage1:")
                        else n["kind"]
                    )
                    await (
                        await session.run(
                            "MATCH (n:RageEntity {id:$id}) SET n.name=$name,n.kind=$kind,n.normalized=$normalized",
                            id=n["id"],
                            name=self.cipher.encrypt(original_name, "graph.name"),
                            kind=self.cipher.encrypt(original_kind, "graph.kind"),
                            normalized=self.cipher.blind(original_name.casefold(), "entity"),
                        )
                    ).consume()
            edges = await (
                await session.run(
                    "MATCH ()-[r:RAGE_REL {namespace:$ns}]->() RETURN r.id AS id,r.predicate AS predicate,r.evidence AS evidence,r.filename AS filename",
                    ns=s.knowledge_namespace,
                )
            ).data()
            for edge in edges:
                if (
                    not edge["evidence"].startswith("rage1:")
                    or edge["evidence"].split(":")[1] != self.cipher.active
                ):
                    await (
                        await session.run(
                            "MATCH ()-[r:RAGE_REL {id:$id}]->() SET r.predicate=$predicate,r.evidence=$evidence,r.filename=$filename",
                            id=edge["id"],
                            **{
                                key: self.cipher.encrypt(
                                    self.cipher.decrypt(edge[key], "graph." + key)
                                    if edge[key].startswith("rage1:")
                                    else edge[key],
                                    "graph." + key,
                                )
                                for key in ("predicate", "evidence", "filename")
                            },
                        )
                    ).consume()
            await (
                await session.run(
                    "MERGE (g:RageGraph {namespace:$ns}) SET g.revision=$revision",
                    ns=s.knowledge_namespace,
                    revision=uuid.uuid4().hex,
                )
            ).consume()

    async def healthy(self):
        await self.driver.verify_connectivity()

    async def replace(self, document_id, entities, relations):
        namespace = self.settings.knowledge_namespace

        async def transaction(tx):
            result = await tx.run(
                "MATCH ()-[r:RAGE_REL {namespace:$ns, document_id:$doc}]->() DELETE r",
                ns=namespace,
                doc=document_id,
            )
            await result.consume()
            result = await tx.run(
                "MATCH (n:RageEntity {namespace:$ns}) WHERE $doc IN n.documents SET n.documents=[d IN n.documents WHERE d <> $doc]",
                ns=namespace,
                doc=document_id,
            )
            await result.consume()
            if entities:
                result = await tx.run(
                    """UNWIND $entities AS e MERGE (n:RageEntity {id:e.id})
                    SET n.namespace=$ns, n.name=e.name, n.normalized=e.normalized, n.kind=e.kind,
                        n.documents=CASE WHEN $doc IN coalesce(n.documents,[]) THEN n.documents ELSE coalesce(n.documents,[])+$doc END""",
                    entities=entities,
                    ns=namespace,
                    doc=document_id,
                )
                await result.consume()
            if relations:
                result = await tx.run(
                    """UNWIND $relations AS e
                    MATCH (a:RageEntity {id:e.source}), (b:RageEntity {id:e.target})
                    MERGE (a)-[r:RAGE_REL {id:e.id}]->(b)
                    SET r.namespace=$ns,r.document_id=$doc,r.chunk_id=e.chunk_id,r.predicate=e.predicate,
                        r.evidence=e.evidence,r.filename=e.filename,r.extraction=e.extraction""",
                    relations=relations,
                    ns=namespace,
                    doc=document_id,
                )
                await result.consume()
            result = await tx.run(
                "MATCH (n:RageEntity {namespace:$ns}) WHERE size(n.documents)=0 AND NOT (n)-[:RAGE_REL]-() DELETE n",
                ns=namespace,
            )
            await result.consume()
            result = await tx.run(
                "MERGE (g:RageGraph {namespace:$ns}) SET g.revision=$revision",
                ns=namespace,
                revision=uuid.uuid4().hex,
            )
            await result.consume()

        async with self.tracing.span("cypher", metadata={"kind": "replace"}):
            async with self.driver.session(database=self.settings.neo4j_database) as session:
                await session.execute_write(unit_of_work(timeout=self.settings.graph_timeout)(transaction))

    async def read(self, template, cypher, params, ready_revision):
        async with self.tracing.span("cypher", metadata={"template": template}):
            async with self.driver.session(
                database=self.settings.neo4j_database, default_access_mode="READ"
            ) as session:
                cursor = await session.run(
                    Query(
                        "MATCH (g:RageGraph {namespace:$ns}) RETURN g.revision AS revision",
                        timeout=self.settings.graph_timeout,
                    ),
                    ns=self.settings.knowledge_namespace,
                )
                record = await cursor.single()
            revision = record["revision"] if record else "empty"
            payload = [
                self.settings.neo4j_uri,
                self.settings.neo4j_database,
                cypher,
                params,
                revision,
                ready_revision,
            ]
            key = (
                "rag-e:secure:cypher:"
                + hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            )

            async def loader():
                async with self.driver.session(
                    database=self.settings.neo4j_database, default_access_mode="READ"
                ) as session:
                    cursor = await session.run(
                        Query(cypher, timeout=self.settings.graph_timeout), parameters=params
                    )
                    return await cursor.data()

            try:
                return await self.cache.get_or_load(key, loader)
            except Exception as exc:
                raise AppError("graph_query_failed", "图谱查询失败，请检查 Neo4j 状态", 503) from exc

    async def close(self):
        if self.driver:
            await self.driver.close()
