import json

import pytest
from pydantic import ValidationError

from app.config import ROOT, Settings
from app.evaluation.audit import audit
from app.evaluation.configuration import load_settings
from app.pipeline_profile import effective_pipeline, pipeline_fingerprint


def profile_config(tmp_path, extra=""):
    path = tmp_path / "profile.json"
    path.write_bytes((ROOT / "config/rag.production.example.json").read_bytes())
    env = tmp_path / ".env"
    env.write_text(
        f"RAGE_PIPELINE_PROFILE={path.as_posix()}\n"
        "RAGE_LLM_API_KEY=private-test\nRAGE_EMBEDDING_API_KEY=private-test\nRAGE_RERANK_API_KEY=private-test\n"
        "RAGE_NEO4J_PASSWORD=private-test\nRAGE_REDIS_URL=redis://localhost:6379/2\n" + extra,
        encoding="utf-8",
    )
    return env, path


def test_production_and_evaluation_share_every_pipeline_field(tmp_path):
    env, _ = profile_config(tmp_path)
    prod = Settings(_env_file=env, environment="test")
    evaluation = load_settings(env, workspace=tmp_path / "evaluation")
    assert effective_pipeline(prod) == effective_pipeline(evaluation)
    assert prod.semantic_chunking and prod.chunk_size == 1500
    assert not prod.graph_enabled and not evaluation.graph_enabled
    assert evaluation.knowledge_namespace.startswith("eval_")
    assert evaluation.redis_url.get_secret_value() == "redis://localhost:6379/2"
    assert pipeline_fingerprint(prod) == pipeline_fingerprint(evaluation)
    assert audit(env, env)["aligned"]


def test_environment_conflict_does_not_print_keys(tmp_path):
    env, _ = profile_config(tmp_path, "RAGE_CHUNK_SIZE=1000\n")
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=env)
    assert "private-test" not in str(exc.value)


def test_profile_fingerprint_tracks_every_algorithm_setting(tmp_path):
    env, _ = profile_config(tmp_path)
    s = Settings(_env_file=env)
    assert pipeline_fingerprint(s) != pipeline_fingerprint(s.model_copy(update={"chunk_size": 1000}))
    # Run-level isolation must not change the algorithm signature.
    assert pipeline_fingerprint(s) == pipeline_fingerprint(
        s.model_copy(update={"knowledge_namespace": "other", "milvus_collection": "other"})
    )


@pytest.mark.parametrize(
    "extra",
    [
        "RAGE_CHUNK_SIZE=1000\n",
        "RAGE_GRAPH_ENABLED=true\n",
        "RAGE_RERANK_CANDIDATES=48\n",
        "RAGE_EMBEDDING_MODEL=another-model\n",
    ],
)
def test_profile_rejects_environment_drift(tmp_path, extra):
    env, _ = profile_config(tmp_path, extra)
    with pytest.raises(ValidationError, match="冲突"):
        Settings(_env_file=env)


def test_equivalent_env_values_allowed_and_secret_profile_forbidden(tmp_path):
    env, profile = profile_config(tmp_path, "RAGE_CHUNK_SIZE=1500\nRAGE_SEMANTIC_CHUNKING=true\n")
    assert Settings(_env_file=env).chunk_size == 1500
    content = json.loads(profile.read_text(encoding="utf-8"))
    content["settings"]["llm_api_key"] = "should-not-be-here"
    profile.write_text(json.dumps(content), encoding="utf-8")
    with pytest.raises(ValidationError, match="不允许"):
        Settings(_env_file=env)


async def test_changed_chunk_rules_reject_reusing_existing_profile_index(settings, tmp_path):
    from app.infrastructure.container import Container

    settings.pipeline_profile = tmp_path / "profile.json"
    first = Container(settings)
    try:
        await first.start()
        await first.knowledge.ingest("sample.txt", "现有资料不能被静默换成其他分块规则。".encode())
    finally:
        await first.close()
    settings.chunk_size = 1000
    changed = Container(settings)
    try:
        from app.common.errors import AppError

        with pytest.raises(AppError) as exc:
            await changed.start()
        assert exc.value.code == "embedding_space_changed"
    finally:
        await changed.close()
