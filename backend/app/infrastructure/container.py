import asyncio

import httpx

from app.agents.orchestrator import Orchestrator
from app.common.encryption import Cipher, encrypt_database
from app.database.session import Database
from app.infrastructure.cache import Cache
from app.infrastructure.monitoring import Monitoring
from app.infrastructure.repositories import Repository
from app.infrastructure.tracing import Tracing
from app.knowledge.rag.document_processor import DocumentProcessor
from app.knowledge.rag.embedding import Embedder
from app.knowledge.rag.local_store import LocalStore
from app.services.chat_service import ChatService
from app.services.graph_service import GraphService
from app.services.knowledge_service import KnowledgeService
from app.services.llm_service import LLMService
from app.services.moderation_service import ModerationService
from app.services.rerank_service import Reranker
from app.services.security_service import SecurityService
from app.services.web_search import WebSearch
from app.utils.security import hash_password


class Container:
    def __init__(self, settings):
        self.settings = settings
        # Metrics: QPS,请求耗时，错误率
        self.monitoring = Monitoring(settings)
        # 链路追踪：定位一次请求经过了哪些服务，服务的耗时
        self.tracing = Tracing(settings, self.monitoring)
        # 加密服务：开发环境：允许自动生成密钥，降低启动门槛。
        #           生产环境：要求预先配置好密钥，防止意外创建新密钥导致已有数据无法解密。
        self.cipher = Cipher(settings.encryption_key_file, create=settings.environment != "production")
        # 创建数据库管理对象
        self.db = Database(settings.database_url, self.cipher)
        # 创建安全服务
        self.security = SecurityService(settings, self.db, self.cipher)
        self.moderation = ModerationService(settings, self.security, self.monitoring, self.tracing)
        # 创建数据访问层：业务代码不直接依赖具体 SQL，而是通过统一的数据访问接口操作数据。
        self.repository = Repository(self.db)
        self.repository.security = self.security
        # 异步的Http连接池
        self.http = httpx.AsyncClient(
            timeout=settings.llm_timeout,
            limits=httpx.Limits(
                max_connections=settings.llm_max_connections,  # HTTP 客户端连接池最多允许 100 条并发连接。
                max_keepalive_connections=settings.llm_max_connections,  # 连接池最多保留多少条空闲的 Keep-Alive 连接
            ),
        )
        # redis+L1本地缓存
        self.cache = Cache(
            settings.redis_url.get_secret_value(),
            settings.cache_ttl,
            l1_size=settings.cache_l1_size,
            l1_ttl=settings.cache_l1_ttl,
            monitoring=self.monitoring,
        )
        self.cache.cipher = self.cipher
        # 精排模型服务
        self.reranker = Reranker(settings, self.http, self.cache, self.tracing)
        # Embedding模型服务
        self.embedder = Embedder(settings, self.http)
        # 文档处理服务
        self.processor = DocumentProcessor(settings, self.embedder)
        # 存储后端
        if settings.vector_backend == "milvus":
            from app.services.milvus_service import MilvusStore

            self.store = MilvusStore(settings, self.repository, self.cipher)
        else:
            self.store = LocalStore(self.repository)
        # 组装RAG核心服务
        self.knowledge = KnowledgeService(
            settings, self.repository, self.processor, self.embedder, self.store, self.cache
        )
        # 大模型调用封装
        self.llm = LLMService(settings, self.http, self.tracing)
        # 知识图谱相关服务
        self.graph = GraphService(settings, self.repository, self.llm, self.cache, self.tracing)
        self.graph.cipher = self.cipher
        self.graph.store.cipher = self.cipher
        self.knowledge.graph = self.graph
        self.knowledge.tracing = self.tracing
        self.knowledge.reranker = self.reranker
        self.embedder.tracing = self.tracing
        # 创建联网搜索服务，并加入链路追踪
        self.web = WebSearch(settings, self.http)
        self.web.tracing = self.tracing
        # Agent系统的编排层
        self.orchestrator = Orchestrator(
            settings, self.llm, self.knowledge, self.web, self.tracing, self.moderation
        )
        # 为数据库操作接入监控埋点，例如统计查询次数和耗时。
        self.db.instrument(self.monitoring)
        # 聊天业务服务
        self.chat = ChatService(settings, self.repository, self.orchestrator, self.moderation)
        self.registry = self.monitoring.registry
        self.requests = self.monitoring.requests
        self.latency = self.monitoring.latency

    async def start(self):
        # 数据库迁移
        if self.settings.auto_migrate:
            await asyncio.to_thread(self.db.migrate)
        migrated = await asyncio.to_thread(self._migrate_encryption)
        # 获取Emnedding服务签名
        signature = {
            name: getattr(self.settings, name)
            for name in (
                "embedding_provider",
                "embedding_model",
                "embedding_dimension",
                "vector_backend",
            )
        }
        # 如果是API补充签名
        if self.settings.embedding_provider == "api":
            signature["embedding_base_url"] = self.settings.embedding_base_url
        # 添加文档切分策略
        if self.settings.pipeline_profile:
            signature["ingestion"] = {
                "version": 1,
                "semantic_chunking": self.settings.semantic_chunking,
                "chunk_size": self.settings.chunk_size,
                "chunk_overlap": self.settings.chunk_overlap,
            }
        # milvus存储配置
        if self.settings.vector_backend == "milvus":
            signature["milvus_collection"] = self.settings.milvus_collection
            signature["milvus_uri"] = self.settings.milvus_uri
        await asyncio.to_thread(self.repository.ensure_index_profile, signature)
        # 管理员账号初始化
        if not await asyncio.to_thread(self.repository.user, username=self.settings.admin_username):
            # 计算管理员密码哈希
            hashed = await asyncio.to_thread(hash_password, self.settings.admin_password.get_secret_value())
            try:
                # 尝试创建管理员
                await asyncio.to_thread(
                    self.repository.create_user, self.settings.admin_username, hashed, "admin"
                )
            except Exception:
                if not await asyncio.to_thread(self.repository.user, username=self.settings.admin_username):
                    raise
        # 初始化向量存储数据库
        await asyncio.to_thread(self.store.initialize)
        admin = await asyncio.to_thread(self.repository.user, username=self.settings.admin_username)
        if self.settings.environment == "production":
            from app.utils.security import verify_password

            if await asyncio.to_thread(verify_password, "rag-e-demo-2026", admin["password_hash"]):
                raise ValueError("生产环境禁止数据库中仍使用演示密码的管理员账号；请先修改密码")
        await asyncio.to_thread(self.security.bootstrap, admin["id"])
        # 启动链路追踪服务和知识图谱服务
        await self.tracing.start()
        await self.graph.start()
        # 图索引重建
        if migrated and self.graph.enabled:
            for document in await asyncio.to_thread(self.repository.documents):
                if document["status"] == "ready" and document["graph_status"] == "ready":
                    await self.graph.index(document["id"])
        # redis历史缓存清理
        if self.cache.redis:
            # Pre-security cache namespaces may contain source text. Expire only this application's old keys.
            for pattern in ("rag-e:retrieval:*", "rag-e:cypher:*"):
                async for key in self.cache.redis.scan_iter(match=pattern, count=100):
                    await self.cache.redis.delete(key)
        await self.monitoring.start(self.dependency_health)

    def _migrate_encryption(self):
        from sqlalchemy import text

        with self.db.engine.connect() as connection:
            state = connection.execute(
                text("SELECT version FROM security_state WHERE id='encryption'")
            ).scalar()
        if state == 0:
            encrypt_database(self.db, allow_plaintext=True)
            with self.db.engine.begin() as connection:
                connection.execute(text("UPDATE security_state SET version=1 WHERE id='encryption'"))
            if self.db.engine.dialect.name == "sqlite":
                with self.db.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                    connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
                    connection.exec_driver_sql("VACUUM")
                    connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
            return True
        return False

    async def dependency_health(self):
        async def checked(name, call):
            try:
                await asyncio.wait_for(call(), timeout=3)
                return name, True
            except Exception:
                return name, False

        # 利用lambda延迟请求
        checks = [("database", lambda: asyncio.to_thread(self.db.healthy))]
        if self.cache.redis:
            checks.append(("redis", self.cache.redis.ping))
        if self.graph.enabled:
            checks.append(("neo4j", self.graph.store.healthy))
        if self.settings.vector_backend == "milvus":
            checks.append(("milvus", lambda: asyncio.to_thread(self.store.healthy)))

        # 并发执行
        return dict(await asyncio.gather(*(checked(name, call) for name, call in checks)))

    async def close(self):
        await self.monitoring.close()
        await self.reranker.close()
        await self.moderation.close()
        await self.graph.close()
        await self.tracing.close()
        await self.cache.close()
        await self.http.aclose()
        await asyncio.to_thread(self.store.close)
        await asyncio.to_thread(self.db.close)
