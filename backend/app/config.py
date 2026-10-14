from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", extra="ignore", env_prefix="RAGE_", hide_input_in_errors=True
    )
    pipeline_profile: Path | None = None

    @model_validator(mode="before")
    @classmethod
    def shared_pipeline(cls, values):
        from app.pipeline_profile import apply_profile

        return apply_profile(cls, values, ROOT)

    environment: Literal["development", "test", "production"] = "development"
    mode: Literal["demo", "live"] = "demo"
    database_url: str = f"sqlite:///{(ROOT / 'data' / 'rag_e.db').as_posix()}"
    auto_migrate: bool = True
    jwt_secret: SecretStr = SecretStr("local-demo-only-change-before-deploy-2026")
    token_minutes: int = Field(default=120, ge=5, le=1440)
    admin_username: str = Field(default="admin", min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    admin_password: SecretStr = SecretStr("rag-e-demo-2026")
    allow_registration: bool = False
    encryption_key_file: Path = ROOT / ".secrets" / "keyring.json"
    public_origin: str = "http://localhost:5173"
    proof_window_seconds: int = Field(default=60, ge=10, le=120)
    session_hours: int = Field(default=24, ge=1, le=168)
    trusted_proxy_hosts: list[str] = []
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = ""
    llm_thinking: Literal["default", "disabled", "enabled"] = "default"
    llm_timeout: float = Field(default=30, gt=0, le=120)
    vision_base_url: str = "https://api.openai.com/v1"
    vision_api_key: SecretStr = SecretStr("")
    vision_model: str = ""
    vision_timeout: float = Field(default=45, gt=0, le=120)
    vision_max_concurrency: int = Field(default=2, ge=1, le=8)
    attachment_space_bytes: int = Field(default=100 * 1024 * 1024, ge=1024, le=1024 * 1024 * 1024)
    attachment_max_files: int = Field(default=100, ge=1, le=1000)
    attachment_max_per_message: int = Field(default=4, ge=1, le=10)
    attachment_max_chars: int = Field(default=24000, ge=1000, le=60000)
    attachment_max_images: int = Field(default=12, ge=1, le=20)
    request_timeout: float = Field(default=150, gt=0, le=160)
    embedding_provider: Literal["hash", "huggingface", "api"] = "hash"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_device: str = "cpu"
    embedding_dimension: int = Field(default=512, ge=16, le=8192)
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_api_key: SecretStr = SecretStr("")
    vector_backend: Literal["local", "milvus"] = "local"
    milvus_uri: str = "http://localhost:19530"
    milvus_token: SecretStr = SecretStr("")
    milvus_collection: str = "rag_e_chunks"
    milvus_hnsw_m: int = Field(default=16, ge=2, le=64)
    milvus_hnsw_ef_construction: int = Field(default=128, ge=8, le=4096)
    milvus_hnsw_ef: int = Field(default=128, ge=1, le=32768)
    tavily_api_key: SecretStr = SecretStr("")
    exa_api_key: SecretStr = SecretStr("")
    web_search_enabled: bool = False
    web_search_provider: Literal["tavily", "exa", "hybrid"] = "tavily"
    web_search_max_results: int = Field(default=3, ge=1, le=10)
    web_search_timeout: float = Field(default=20, gt=0, le=60)
    web_search_max_concurrency: int = Field(default=4, ge=1, le=16)
    web_search_content_max_chars: int = Field(default=5000, ge=500, le=12000)
    web_search_failure_mode: Literal["error", "partial"] = "error"
    tavily_search_depth: Literal["basic", "advanced"] = "basic"
    knowledge_domain: str = "用户上传的各领域知识与专业文档，不限制行业；优先检索知识库，实时信息按需联网"
    knowledge_namespace: str = Field(default="default", pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    llm_max_connections: int = Field(default=32, ge=1, le=256)
    llm_max_concurrency: int = Field(default=8, ge=1, le=64)
    llm_batch_size: int = Field(default=32, ge=1, le=128)
    cache_l1_size: int = Field(default=256, ge=1, le=10000)
    cache_l1_ttl: int = Field(default=10, ge=1, le=300)
    graph_enabled: bool = False
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_username: str = "neo4j"
    neo4j_password: SecretStr = SecretStr("")
    neo4j_database: str = "neo4j"
    neo4j_pool_size: int = Field(default=16, ge=1, le=100)
    graph_timeout: float = Field(default=15, gt=0, le=60)
    graph_extract_max_chunks: int = Field(default=200, ge=1, le=2000)
    graph_max_entities: int = Field(default=40, ge=2, le=100)
    graph_retrieval_hops: int = Field(default=2, ge=1, le=3)
    profiler_window: int = Field(default=300, ge=10, le=3600)
    profiler_samples: int = Field(default=2000, ge=50, le=50000)
    alert_interval: float = Field(default=5, ge=0.1, le=60)
    alert_for_seconds: float = Field(default=30, ge=0, le=3600)
    alert_min_samples: int = Field(default=5, ge=1, le=1000)
    alert_error_rate: float = Field(default=0.2, gt=0, le=1)
    alert_p95_seconds: float = Field(default=20, gt=0)
    alert_loop_lag_seconds: float = Field(default=0.2, gt=0)
    metrics_token: SecretStr = SecretStr("")
    langfuse_enabled: bool = False
    langfuse_base_url: str = "https://cloud.langfuse.com"
    langfuse_public_key: str = ""
    langfuse_secret_key: SecretStr = SecretStr("")
    trace_capture_content: bool = False
    redis_url: SecretStr = SecretStr("")
    cache_ttl: int = Field(default=300, ge=1)
    rate_limit_per_minute: int = Field(default=120, ge=1)
    chunk_size: int = Field(default=1500, ge=200, le=5000)
    chunk_overlap: int = Field(default=150, ge=0)
    semantic_chunking: bool = True
    max_upload_bytes: int = Field(default=5 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)
    max_chunks: int = Field(default=2000, ge=1)
    retrieval_top_k: int = Field(default=4, ge=1, le=12)
    rerank_provider: Literal["none", "huggingface", "api"] = "none"
    rerank_model: str = "BAAI/bge-reranker-base"
    rerank_revision: str = ""
    rerank_device: str = "cpu"
    rerank_local_files_only: bool = True
    rerank_api_url: str = ""
    rerank_api_format: Literal["standard", "qwen3"] = "standard"
    rerank_api_key: SecretStr = SecretStr("")
    rerank_candidates: int = Field(default=32, ge=1, le=100)
    rerank_batch_size: int = Field(default=8, ge=1, le=32)
    rerank_max_concurrency: int = Field(default=2, ge=1, le=16)
    rerank_max_length: int = Field(default=512, ge=32, le=8192)
    rerank_query_max_chars: int = Field(default=1000, ge=128, le=4000)
    rerank_document_max_chars: int = Field(default=6000, ge=200, le=20000)
    rerank_timeout: float = Field(default=30, gt=0, le=120)
    rerank_min_score: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    rerank_failure_mode: Literal["error", "rrf"] = "error"
    max_rewrites: int = Field(default=2, ge=0, le=3)
    max_generations: int = Field(default=2, ge=1, le=3)
    moderation_backend: Literal["auto", "local", "llm"] = "auto"
    moderation_base_url: str = ""
    moderation_api_key: SecretStr = SecretStr("")
    moderation_model: str = ""
    moderation_thinking: Literal["default", "disabled", "enabled"] = "default"
    moderation_timeout: float = Field(default=12, gt=0, le=60)
    moderation_check_timeout: float = Field(default=35, gt=0, le=120)
    moderation_max_concurrency: int = Field(default=4, ge=1, le=16)
    moderation_chunk_chars: int = Field(default=12000, ge=1000, le=24000)
    moderation_max_chars: int = Field(default=200000, ge=4000, le=500000)
    moderation_thresholds: dict[str, float] = Field(
        default_factory=lambda: {
            "violence": 0.8,
            "hate": 0.8,
            "harassment": 0.8,
            "sexual_minors": 0.5,
            "sexual_explicit": 0.8,
            "self_harm": 0.5,
            "illicit_instructions": 0.8,
            "cyber_abuse": 0.8,
        }
    )

    @property
    def effective_moderation_backend(self):
        return (
            "llm"
            if self.moderation_backend == "llm" or self.moderation_backend == "auto" and self.mode == "live"
            else "local"
        )

    @model_validator(mode="after")
    def validate_configuration(self):
        import ipaddress
        import math
        from urllib.parse import urlsplit

        from app.safety.policy import CATEGORIES

        if self.vision_model:
            endpoint = urlsplit(self.vision_base_url)
            if (
                endpoint.scheme not in {"http", "https"}
                or not endpoint.hostname
                or endpoint.username
                or endpoint.password
                or endpoint.query
                or endpoint.fragment
            ):
                raise ValueError("视觉模型需要有效的 HTTP(S) API 根地址")
            if not self.vision_api_key.get_secret_value().strip():
                raise ValueError("视觉模型需要单独配置 vision_api_key")

        if self.rerank_provider != "none" and not self.rerank_model.strip():
            raise ValueError("启用精排需要配置 rerank_model")
        if self.rerank_provider == "api":
            endpoint = urlsplit(self.rerank_api_url)
            if (
                endpoint.scheme not in {"http", "https"}
                or not endpoint.netloc
                or endpoint.username
                or endpoint.query
                or endpoint.fragment
            ):
                raise ValueError("精排 API 必须配置完整的 HTTP(S) 接口地址")
            # Anonymous endpoints are only useful for locally hosted rerank servers.
            if (
                endpoint.hostname not in {"localhost", "127.0.0.1", "::1"}
                and not self.rerank_api_key.get_secret_value()
            ):
                raise ValueError("远程精排 API 需要独立的 rerank_api_key")

        if set(self.moderation_thresholds) != set(CATEGORIES) or any(
            type(v) not in (float, int) or not math.isfinite(v) or not 0 < v <= 1
            for v in self.moderation_thresholds.values()
        ):
            raise ValueError("moderation_thresholds 必须包含全部8个类别，阈值范围 (0,1]")
        if self.moderation_base_url:
            endpoint = urlsplit(self.moderation_base_url)
            if (
                endpoint.scheme not in {"http", "https"}
                or not endpoint.netloc
                or endpoint.username
                or endpoint.query
                or endpoint.fragment
            ):
                raise ValueError("内容审查地址必须为有效 HTTP(S) API 根地址")
            if not self.moderation_api_key.get_secret_value():
                raise ValueError("独立内容审查地址必须配置独立 API Key")
        if self.effective_moderation_backend == "llm" and (
            not (self.moderation_model or self.llm_model)
            or not (self.moderation_api_key.get_secret_value() or self.llm_api_key.get_secret_value())
        ):
            raise ValueError("语义内容审查需要模型与 API Key")

        origin = urlsplit(self.public_origin)
        if (
            origin.scheme not in {"http", "https"}
            or not origin.netloc
            or origin.path not in {"", "/"}
            or origin.username
            or origin.query
            or origin.fragment
        ):
            raise ValueError("public_origin 必须是有效的 HTTP(S) 源地址")
        for network in self.trusted_proxy_hosts:
            ipaddress.ip_network(network)
        if self.graph_enabled and not self.neo4j_password.get_secret_value():
            raise ValueError("启用图谱需要 Neo4j 密码")
        if self.langfuse_enabled and (
            not self.langfuse_public_key or not self.langfuse_secret_key.get_secret_value()
        ):
            raise ValueError("启用 Langfuse 需要 public key 和 secret key")
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap 必须小于 chunk_size")
        if self.mode == "live":
            if not self.llm_api_key.get_secret_value() or not self.llm_model:
                raise ValueError("live 模式必须配置 LLM API Key 和模型")
            if self.embedding_provider == "hash":
                raise ValueError("live 模式必须使用真实 embedding 模型")
        if self.embedding_provider == "api" and not self.embedding_api_key.get_secret_value():
            raise ValueError("API embedding 需要独立配置 API Key")
        if self.web_search_enabled:
            if (
                self.web_search_provider in {"tavily", "hybrid"}
                and not self.tavily_api_key.get_secret_value().strip()
            ):
                raise ValueError("所选联网搜索模式需要 Tavily API Key")
            if (
                self.web_search_provider in {"exa", "hybrid"}
                and not self.exa_api_key.get_secret_value().strip()
            ):
                raise ValueError("所选联网搜索模式需要 Exa API Key")
        if self.environment == "production":
            if self.effective_moderation_backend != "llm":
                raise ValueError("生产环境必须启用语义内容审查，不能只使用本地规则")
            if not self.encryption_key_file.is_file():
                raise ValueError("生产环境必须挂载独立的 encryption_key_file")
            if not self.public_origin.startswith("https://"):
                raise ValueError("生产环境必须配置 HTTPS public_origin")
            if self.trace_capture_content:
                raise ValueError("生产环境安全模式禁止上传模型正文追踪")
            if (
                len(self.jwt_secret.get_secret_value()) < 32
                or "local-demo" in self.jwt_secret.get_secret_value()
            ):
                raise ValueError("生产环境必须设置至少 32 字符的随机 JWT 密钥")
            if (
                len(self.admin_password.get_secret_value()) < 12
                or self.admin_password.get_secret_value() == "rag-e-demo-2026"
            ):
                raise ValueError("生产环境必须设置独立的管理员密码")
            if self.mode != "live" or not self.database_url.startswith("postgresql"):
                raise ValueError("生产环境要求 live 模式和 PostgreSQL")
            if self.auto_migrate:
                raise ValueError("生产环境先执行迁移，再关闭 auto_migrate 启动")
        return self
