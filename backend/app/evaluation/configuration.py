"""Load the explicit evaluation configuration without reusing business stores."""

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import SecretStr, ValidationError

from app.common.errors import AppError
from app.config import ROOT, Settings
from app.pipeline_profile import pipeline_fingerprint


class EvaluationConfigError(ValueError):
    pass


def load_settings(config: Path | None = None, *, workspace: Path | None = None, backend: str | None = None):
    config = (config or ROOT / ".env.eval").resolve()
    if not config.is_file():
        raise EvaluationConfigError("缺少 .env.eval；请复制 .env.eval.example 并填写模型配置。")
    # File selection is explicit. SQL, keys and collection identity remain isolated.
    # The configured vector service endpoint/token may be shared, never its collection.
    workspace = (workspace or ROOT / ".cache/evaluation/server").resolve()
    configured = dotenv_values(config)
    profile_selected = bool(os.getenv("RAGE_PIPELINE_PROFILE") or configured.get("RAGE_PIPELINE_PROFILE"))
    identity = hashlib.sha256(workspace.as_posix().encode()).hexdigest()[:24]
    vector_options = {"vector_backend": backend} if backend else {}
    # Legacy profiles retain their isolated baseline behavior. Shared production
    # profiles govern graph/web behavior instead of silently turning those paths off.
    baseline_options = {} if profile_selected else {"graph_enabled": False, "web_search_enabled": False}
    try:
        settings = Settings(
            _env_file=config,
            environment="test",
            mode="live",
            database_url="sqlite:///" + (workspace / "evaluation.db").as_posix(),
            encryption_key_file=workspace / "keyring.json",
            jwt_secret=SecretStr(secrets.token_urlsafe(48)),
            auto_migrate=True,
            **vector_options,
            **baseline_options,
            milvus_collection="rag_e_eval_" + identity,
            knowledge_namespace="eval_" + identity,
            redis_url=SecretStr(configured.get("RAGE_REDIS_URL") or ""),
            langfuse_enabled=False,
            trace_capture_content=False,
            public_origin="http://localhost:8001",
        )
    except ValidationError as exc:
        # Pydantic's default representation may include a whole settings input dict.
        fields = sorted({str(part) for error in exc.errors(include_input=False) for part in error["loc"]})
        raise EvaluationConfigError(
            "真实模型配置不完整或数值无效。请核对 LLM/Embedding/Rerank 模型、Key、地址和维度"
            + ("；相关字段：" + ", ".join(fields) if fields else "")
        ) from None
    required = {"RAGE_LLM_BASE_URL": settings.llm_base_url, "RAGE_LLM_MODEL": settings.llm_model}
    if settings.embedding_provider == "api":
        required.update(
            RAGE_EMBEDDING_BASE_URL=settings.embedding_base_url, RAGE_EMBEDDING_MODEL=settings.embedding_model
        )
    if settings.rerank_provider == "api":
        required.update(RAGE_RERANK_API_URL=settings.rerank_api_url, RAGE_RERANK_MODEL=settings.rerank_model)
    for name, value in required.items():
        if not value or "your-" in value or ".example" in value or "{WorkspaceId}" in value:
            raise EvaluationConfigError(f"请填写 {name} 的实际值。")
    for name, url in (
        ("LLM", settings.llm_base_url),
        ("Embedding", settings.embedding_base_url if settings.embedding_provider == "api" else ""),
    ):
        if not url:
            continue
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.query
            or parsed.fragment
        ):
            raise EvaluationConfigError(f"{name} 地址必须是有效 HTTP(S) API 根地址，不能包含凭据或查询参数。")
        if parsed.path.rstrip("/").endswith(("/chat/completions", "/embeddings")):
            raise EvaluationConfigError(f"{name} 请填写 API 根地址，程序会自动拼接具体路径。")
    for name, value in (
        ("rerank_model", settings.rerank_model),
        ("embedding_model", settings.embedding_model),
    ):
        if value.startswith((".", "/")) or Path(value).is_absolute():
            path = Path(value)
            setattr(settings, name, str((path if path.is_absolute() else ROOT / path).resolve()))
    if (
        settings.rerank_provider == "huggingface"
        and Path(settings.rerank_model).is_absolute()
        and not Path(settings.rerank_model).is_dir()
    ):
        raise EvaluationConfigError("本地精排模型目录不存在，请核对 RAGE_RERANK_MODEL。")
    return settings


def summary(settings):
    return {
        "mode": settings.mode,
        "llm_model": settings.llm_model,
        "embedding_provider": settings.embedding_provider,
        "embedding_model": settings.embedding_model,
        "embedding_dimension": settings.embedding_dimension,
        "rerank_provider": settings.rerank_provider,
        "rerank_model": Path(settings.rerank_model).name
        if Path(settings.rerank_model).is_absolute()
        else settings.rerank_model,
        "moderation_backend": settings.effective_moderation_backend,
        "database": settings.database_url,
        "vector_backend": settings.vector_backend,
        "milvus_collection": settings.milvus_collection + "_secure"
        if settings.vector_backend == "milvus"
        else None,
        "isolated": True,
        "pipeline_fingerprint": pipeline_fingerprint(settings),
    }


async def probe(config):
    from app.infrastructure.container import Container

    parent = ROOT / ".cache/evaluation"
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="probe-", dir=parent) as workspace:
        # Connectivity probe has no retrieval corpus; don't create an unused Milvus collection.
        settings = load_settings(config, workspace=Path(workspace))
        # A model-only probe deliberately has no graph/vector index. Full evaluation
        # loads the unmodified shared profile and verifies its fingerprint.
        settings = settings.model_copy(update={"vector_backend": "local", "graph_enabled": False})
        services = Container(settings)
        try:
            await services.start()
            checks = {}
            vectors = await services.embedder.embed(["北京是中国的首都。", "香蕉是一种水果。"])
            checks["embedding"] = {"count": len(vectors), "dimension": len(vectors[0])}
            grade = await services.llm.call(
                "relevance", {"question": "中国首都是哪里？", "document": "北京是中国的首都。"}
            )
            checks["llm_json"] = isinstance(grade["relevant"], bool)
            await services.moderation.guard("北京是中国的首都。", "input")
            checks["moderation"] = "passed"
            answer = await services.llm.call(
                "generate",
                {
                    "original_question": "中国首都是哪里？",
                    "history": [],
                    "sources": [{"id": "sample", "text": "北京是中国的首都。", "metadata": {}}],
                },
            )
            await services.moderation.guard(answer, "output")
            checks["generation"] = "passed" if isinstance(answer, str) and answer.strip() else "empty"
            if services.reranker.enabled:
                ranked = await services.reranker.rank(
                    "中国首都是哪里？",
                    [
                        {"id": "fruit", "text": "香蕉是一种水果。", "score": 0.1, "metadata": {}},
                        {"id": "capital", "text": "北京是中国的首都。", "score": 0.1, "metadata": {}},
                    ],
                )
                if not ranked or any(s["metadata"].get("rerank_status") != "ranked" for s in ranked):
                    raise EvaluationConfigError("精排未返回模型评分，请检查阈值、模型和失败策略。")
                checks["rerank"] = {
                    "returned": len(ranked),
                    "capital_rank": next((i + 1 for i, s in enumerate(ranked) if s["id"] == "capital"), None),
                }
            print(
                json.dumps(
                    {
                        "status": "probe_passed",
                        "checks": checks,
                        "note": "仅验证接口和格式，不代表数据集评测成绩。",
                    },
                    ensure_ascii=False,
                )
            )
        finally:
            await services.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / ".env.eval")
    parser.add_argument(
        "--probe", action="store_true", help="发送少量合成文本验证模型连接，会产生供应商调用用量"
    )
    args = parser.parse_args()
    try:
        settings = load_settings(args.config)
        print(json.dumps({"status": "configuration_valid", **summary(settings)}, ensure_ascii=False))
        if args.probe:
            asyncio.run(probe(args.config))
    except (EvaluationConfigError, AppError) as exc:
        print(str(exc))
        raise SystemExit(1) from None
    except Exception as exc:
        print("连通性检查失败：" + type(exc).__name__ + "。请核对供应商接口、依赖和模型配置。")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
