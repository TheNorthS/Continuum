"""One versioned, credential-free RAG policy shared by production and evaluation."""

import hashlib
import json
from pathlib import Path

from pydantic import TypeAdapter

WEB_PIPELINE_FIELDS = frozenset(
    "web_search_provider web_search_max_results web_search_timeout web_search_max_concurrency "
    "web_search_content_max_chars web_search_failure_mode tavily_search_depth".split()
)

PIPELINE_FIELDS = (
    frozenset(
        """
mode llm_base_url llm_model llm_thinking llm_timeout request_timeout
llm_max_connections llm_max_concurrency llm_batch_size
embedding_provider embedding_model embedding_dimension embedding_device embedding_base_url
vector_backend milvus_hnsw_m milvus_hnsw_ef_construction milvus_hnsw_ef
knowledge_domain graph_enabled graph_timeout neo4j_pool_size graph_extract_max_chunks
graph_max_entities graph_retrieval_hops web_search_enabled
cache_l1_size cache_l1_ttl cache_ttl
semantic_chunking chunk_size chunk_overlap max_chunks max_upload_bytes retrieval_top_k
rerank_provider rerank_model rerank_revision rerank_device rerank_local_files_only
rerank_api_url rerank_api_format rerank_candidates rerank_batch_size rerank_max_concurrency
rerank_max_length rerank_query_max_chars rerank_document_max_chars rerank_timeout
rerank_min_score rerank_failure_mode max_rewrites max_generations
moderation_backend moderation_model moderation_base_url moderation_thinking
moderation_timeout moderation_check_timeout moderation_max_concurrency
moderation_chunk_chars moderation_max_chars moderation_thresholds
""".split()
    )
    | WEB_PIPELINE_FIELDS
)


def apply_profile(model, values, root):
    if not isinstance(values, dict) or not values.get("pipeline_profile"):
        return values
    path = Path(values["pipeline_profile"])
    path = path if path.is_absolute() else root / path
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("无法读取 RAG pipeline_profile 文件") from exc
    if (
        set(document) != {"version", "settings"}
        or document["version"] != 1
        or not isinstance(document["settings"], dict)
    ):
        raise ValueError("RAG 配置文件须包含 version=1 和 settings")
    profile = document["settings"]
    if set(profile) - PIPELINE_FIELDS or (PIPELINE_FIELDS - WEB_PIPELINE_FIELDS) - set(profile):
        raise ValueError("RAG 配置字段不完整或包含不允许的字段；不得包含凭据和存储地址")
    # v1 profiles predating dual-provider web search inherit explicit policy defaults.
    # Environment overrides still go through the same conflict checks.
    profile = {
        **{key: model.model_fields[key].default for key in WEB_PIPELINE_FIELDS},
        **profile,
    }
    conflicts = []
    for key, configured in profile.items():
        if key in values:
            try:
                current = TypeAdapter(model.model_fields[key].annotation).validate_python(values[key])
            except ValueError:
                conflicts.append(key)
                continue
            if current != configured:
                conflicts.append(key)
    if conflicts:
        raise ValueError("环境设置与共享 RAG 配置冲突，请删除重复覆盖项：" + ", ".join(sorted(conflicts)))
    return {**values, **profile, "pipeline_profile": path.resolve()}


def effective_pipeline(settings):
    """No secrets, collection identities or business database paths are included."""
    return {key: getattr(settings, key) for key in sorted(PIPELINE_FIELDS)}


def pipeline_fingerprint(settings):
    payload = json.dumps(
        effective_pipeline(settings), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()
