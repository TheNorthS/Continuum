import json

import httpx
import pytest

from app.evaluation.configuration import EvaluationConfigError, load_settings, summary
from app.infrastructure.container import Container
from app.knowledge.rag.embedding import Embedder
from app.services.llm_service import LLMService


def config_file(tmp_path, extra=""):
    path = tmp_path / ".env.eval"
    path.write_text(
        "RAGE_LLM_BASE_URL=https://api.deepseek.com\nRAGE_LLM_API_KEY=test-secret-must-not-print\n"
        "RAGE_LLM_MODEL=deepseek-flash\nRAGE_EMBEDDING_PROVIDER=huggingface\n"
        "RAGE_EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5\nRAGE_RERANK_PROVIDER=none\n" + extra,
        encoding="utf-8",
    )
    return path


def test_evaluation_forces_isolated_resources_and_summary_has_no_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("RAGE_DATABASE_URL", "sqlite:///business.db")
    monkeypatch.setenv("RAGE_REDIS_URL", "redis://business.invalid")
    monkeypatch.setenv("RAGE_GRAPH_ENABLED", "true")
    monkeypatch.setenv("RAGE_LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("RAGE_MILVUS_COLLECTION", "business_collection")
    settings = load_settings(config_file(tmp_path), workspace=tmp_path / "isolated")
    assert settings.mode == "live" and settings.environment == "test"
    assert settings.database_url.endswith("isolated/evaluation.db")
    assert settings.encryption_key_file == tmp_path / "isolated/keyring.json"
    assert (
        not settings.redis_url.get_secret_value()
        and not settings.graph_enabled
        and not settings.langfuse_enabled
    )
    assert "test-secret" not in json.dumps(summary(settings))
    assert settings.milvus_collection.startswith("rag_e_eval_")
    assert settings.milvus_collection != "business_collection"


def test_evaluation_honors_milvus_and_isolates_collections(tmp_path):
    path = config_file(tmp_path, "RAGE_VECTOR_BACKEND=milvus\nRAGE_MILVUS_URI=http://127.0.0.1:19539\n")
    first = load_settings(path, workspace=tmp_path / "first")
    second = load_settings(path, workspace=tmp_path / "second")
    assert first.vector_backend == "milvus"
    assert first.milvus_uri == "http://127.0.0.1:19539"
    assert first.milvus_collection != second.milvus_collection
    assert load_settings(path, workspace=tmp_path / "first").milvus_collection == first.milvus_collection
    assert load_settings(path, backend="local").vector_backend == "local"


def test_incomplete_configuration_does_not_leak_validation_inputs(tmp_path):
    path = config_file(tmp_path, "RAGE_EMBEDDING_PROVIDER=hash\n")
    with pytest.raises(EvaluationConfigError) as exc:
        load_settings(path)
    assert "test-secret" not in str(exc.value)
    with pytest.raises(EvaluationConfigError):
        load_settings(tmp_path / "missing")


def test_evaluation_qwen_requires_real_workspace_url(tmp_path):
    path = config_file(
        tmp_path,
        "RAGE_RERANK_PROVIDER=api\nRAGE_RERANK_MODEL=qwen3-rerank\nRAGE_RERANK_API_FORMAT=qwen3\n"
        "RAGE_RERANK_API_KEY=qwen-private-key\n"
        "RAGE_RERANK_API_URL=https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-api/v1/reranks\n",
    )
    with pytest.raises(EvaluationConfigError, match="RAGE_RERANK_API_URL"):
        load_settings(path)
    path.write_text(path.read_text(encoding="utf-8").replace("{WorkspaceId}", "workspace"), encoding="utf-8")
    settings = load_settings(path, workspace=tmp_path / "evaluation")
    assert settings.rerank_provider == "api" and settings.rerank_model == "qwen3-rerank"
    assert "qwen-private-key" not in json.dumps(summary(settings))


@pytest.mark.parametrize(
    "url",
    [
        "https://api.deepseek.com/chat/completions",
        "https://user:password@example.com/v1",
        "https://your-provider.example/v1",
    ],
)
def test_evaluation_rejects_incorrect_or_placeholder_base_url(tmp_path, url):
    with pytest.raises(EvaluationConfigError):
        load_settings(config_file(tmp_path, "RAGE_LLM_BASE_URL=" + url + "\n"))


@pytest.mark.parametrize("thinking", ["default", "disabled"])
async def test_chat_adapter_only_sends_explicit_thinking_option(settings, thinking):
    settings.mode = "live"
    settings.llm_thinking = thinking

    def handler(request):
        body = json.loads(request.content)
        if thinking == "default":
            assert "thinking" not in body
        else:
            assert body["thinking"] == {"type": "disabled"}
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"relevant":true}'}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert (await LLMService(settings, client).call("relevance", {"question": "q", "document": "d"}))[
            "relevant"
        ]


async def test_moderation_can_disable_deepseek_thinking(settings):
    from app.safety.policy import CATEGORIES

    settings.moderation_thinking = "disabled"
    services = Container(settings)
    try:

        def handler(request):
            assert json.loads(request.content)["thinking"] == {"type": "disabled"}
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": json.dumps(dict.fromkeys(CATEGORIES, 0.0))},
                        }
                    ]
                },
            )

        await services.moderation.http.aclose()
        services.moderation.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        assert await services.moderation._remote("测试", "input") == dict.fromkeys(CATEGORIES, 0.0)
    finally:
        await services.close()


async def test_openai_embedding_uses_independent_key_endpoint_and_native_dimension(tmp_path):
    path = config_file(
        tmp_path,
        "RAGE_EMBEDDING_PROVIDER=api\nRAGE_EMBEDDING_BASE_URL=https://api.openai.com/v1\n"
        "RAGE_EMBEDDING_API_KEY=openai-embedding-secret\nRAGE_EMBEDDING_MODEL=text-embedding-3-small\n"
        "RAGE_EMBEDDING_DIMENSION=1536\n",
    )
    settings = load_settings(path, workspace=tmp_path / "evaluation")

    def handler(request):
        assert str(request.url) == "https://api.openai.com/v1/embeddings"
        assert request.headers["Authorization"] == "Bearer openai-embedding-secret"
        assert json.loads(request.content)["model"] == "text-embedding-3-small"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0] + [0.0] * 1534},
                    {"index": 0, "embedding": [1.0] + [0.0] * 1535},
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        vectors = await Embedder(settings, client).embed(["北京", "上海"])
        assert len(vectors) == 2 and len(vectors[0]) == 1536
        assert vectors[0][0] == 1.0 and vectors[1][1] == 1.0
    assert "openai-embedding-secret" not in json.dumps(summary(settings))
