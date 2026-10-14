# 验证与测试

本页提供当前测试方法，已执行结果见[2026-10-11 发布验收](VERIFICATION.md)。历史记录按日期保留，不能将旧测试数量或服务状态视为当前机器状态。普通回归与真实模型质量评测分别运行。

## 公开数据集评测

正式参数与评测参数统一维护于 `config/rag.production.json`。运行 `python -m app.evaluation.audit` 检查两边指纹；`tests/test_pipeline_profile.py` 覆盖全字段一致性、冲突启动拒绝、禁止凭据写入共享文件及旧分块索引保护。见 [共享规则](SHARED_PIPELINE.md)。

按 [评测配置](EVALUATION.md) 填写三个服务的 Key，安装 `evaluation` extra。使用 `python -m app.evaluation.run` 运行 T2Retrieval 检索与 CRUD-RAG 问答的固定子集对照；命令、指标和限制见 [完整方案](EVALUATION_PLAN.md)。`tests/test_evaluation.py` 校验标准指标、空结果分母、数据标签不入索引、固定采样、共享候选池与恢复逻辑。

## 事实依据反馈修正

在 `backend` 运行 `uv run --inexact pytest -q tests/test_grounding_feedback.py tests/test_workflow.py tests/test_moderation.py`。覆盖 Grounding JSON 协议、失败原因传入下一轮、同一资料下修正后重新评分、次数上限、切换资料 / 新一轮清空反馈，以及反馈审查、API / SSE / 历史和正文追踪隔离。

这些是模拟模型响应的工程回归，不证明真实模型的幻觉率下降。真实质量对照需使用新评测 run，保留历史成绩。

## 模型精排验收

检索缓存版本专项：`tests/test_retrieval_revision.py` 覆盖原地片段变更、事务回滚、并发递增、跨库范围、撤权重授、固定长度指纹及旧库迁移；`tests/test_retrieval_revision_postgres.py` 在配置 `RAGE_TEST_POSTGRES_URL` 后验证生产 SQL 方言，使用隔离测试数据库，不使用业务库。

`tests/test_rerank.py` 覆盖候选扩充、精排后 Top-K、阈值、稳定排序、单/多库、授权前后过滤、图谱融合、加密缓存接入、API 响应索引校验、超时、并发、取消与回退标记。配置 `RAGE_TEST_RERANK_MODEL` 为已下载的本地 CrossEncoder 路径，可额外运行真实中文推理。

Milvus 评测默认读取 `.env.eval` 的后端配置，生成按 run 隔离的 collection；`tests/test_evaluation_milvus.py` 验证旧数据只读、分块/向量逐值复用与模型空间一致性，HNSW 的 `ef` 至少覆盖请求候选数。真实服务回归设置 `RAGE_TEST_MILVUS_URI=http://127.0.0.1:19539`。

在项目根目录执行 `backend/.venv/Scripts/python.exe scripts/verify_rerank.py`，可使用已准备的本地 BGE 运行合成中文片段与 32 条候选负载验证，不写入业务文档。Linux / macOS 使用 `backend/.venv/bin/python`。脚本采用独立的本地精排参数，需在未启用冲突 profile 的测试环境运行。精排配置见 [RERANK.md](RERANK.md)。

## 知识库范围与界面验收

`tests/test_conversation_attachments.py` 覆盖会话附件的创建/更新、重启恢复、省略与空数组语义、失效提示、锁冲突、授权和 `0008` 升级；前端附件测试覆盖刷新恢复、保存失败和延迟响应不会串到其他会话。

附件与多模态：`tests/test_attachments.py` 覆盖私有空间 CRUD、加密轮换、配额、PDF/DOCX/图片解析、不调用 Embedding 的附件问答、混合知识库来源、删除冲突及独立视觉模型协议。前端 `attachments.test.tsx` 验证空间文件操作、聊天上传 / 选择及知识库开关。真实视觉模型效果需配置后另行验收，见[附件指南](ATTACHMENTS.md)。

`backend/tests/test_knowledge_scope.py` 覆盖单库、多库、范围缓存、保存与恢复、切换范围后不带旧上下文、空列表/无权限/撤权拒绝、SSE、并发隔离与最终引用归属检查。配置 `RAGE_TEST_NEO4J_URI` 和 `RAGE_TEST_REDIS_URL` 后还验证真实图谱与 Redis L2 的范围隔离。

`frontend/src/components/knowledge-scope.test.tsx` 覆盖多选搜索、范围请求、失败回退、权限失效、空状态、当前库文档和检索测试联动。操作说明见 [知识库范围](KNOWLEDGE_SCOPE.md)。

独立演示前后端启动并导入示例后，可从项目根目录运行 `backend/.venv/Scripts/python.exe scripts/verify_knowledge_scope.py` 验证前端代理上的签名 HTTP 流程；使用现有知识库，只创建并清理自己的临时会话。该脚本使用默认演示账号和端口，不适用于直接验证已改密的生产库。

## 内容安全审查验收

`tests/test_moderation.py` 覆盖输入/上下文拦截、检索/Web 片段过滤、普通回复与生成候选、SSE/引用泄漏、历史不落库、八类阈值、非法 JSON、服务错误、超时取消、正常教育语境、审计无正文和未审核模型输出不导出追踪。
在运行的本机服务上执行 `backend/.venv/Scripts/python.exe scripts/verify_moderation.py` 可验收签名 HTTP 流程（默认演示账号）。脚本仅删除自己创建的临时会话。

## 安全升级验收

后端 `test_security_complete.py`、`test_security_migration.py`、`test_security_external.py`、`test_postgres_security.py` 覆盖权限、会话、重放、加密、迁移、轮换、审计和真实服务。
所有 HTTP 回归使用 `tests/signed_client.py` 生成真实 P-256 签名，不通过关闭鉴权来让测试通过。
前端 `proof.test.ts` 使用真实 WebCrypto 和 IndexedDB CryptoKey；若要运行跨语言 HTTP 项，先启动本机演示账号后设置 `RAGE_TEST_HTTP_URL=http://127.0.0.1:8000/api/v1`。
未设置该变量时只跳过运行中服务的互通项，其余前端测试仍离线可执行。

## 本地自动化

```bash
cd backend
uv sync --frozen --extra dev --extra milvus --extra evaluation
uv run --inexact ruff check app tests
uv run --inexact ruff format --check app tests
uv run --inexact pytest -q --cov=app --cov-report=term-missing
cd ../frontend
npm ci
npm run format:check
npm test
npm run build
```

后端测试使用独立临时数据库，覆盖：认证/角色/归属、导入去重、检索和来源、持久化、删除缓存失效、SSE 正常/错误、租约并发、输入限制、超时、分块边界、模型 JSON/embedding 协议、HTTP 重试和工作流终止。
普通测试对外部模型使用协议模拟响应，不消耗 API 额度。真实依赖通过 external 测试单独验收，最近结果见[发布验收](VERIFICATION.md)，早期结果见[历史记录](archive/VERIFICATION.md)；模型质量评测和 Kubernetes 验收需在目标环境另外执行。
前端测试覆盖跨网络块的 SSE 帧解析、登录/知识库导航、问答与引用、切换会话加载状态，以及不可信 Markdown 渲染；生产构建执行 TypeScript 类型检查。

工程测试覆盖 Neo4j 图谱、LRU/Redis 分层与并发加载、批量模型并发/取消、事件循环心跳、Profiler 分位数、后台告警状态机、监控鉴权与 Langfuse 真实 SDK 导出；前端覆盖图谱证据/路径交互及监控面板。
准备独立 Neo4j/Redis 服务后，设置地址再运行对应测试：

```powershell
cd backend
$env:RAGE_TEST_NEO4J_URI='bolt://127.0.0.1:17687'
$env:RAGE_TEST_REDIS_URL='redis://127.0.0.1:16379/0'
uv run --inexact --extra dev --extra milvus pytest -q
```

图谱测试使用随机 namespace，结束后只清理自己的实体关系，保留业务图谱。

真实 Milvus 可在独立测试环境运行：

```powershell
# RAG-E 根目录
docker compose -p rag-e-validation -f docker-compose.yml -f docker-compose.test.yml --profile milvus up -d --wait etcd minio milvus
cd backend
$env:RAGE_TEST_MILVUS_URI='http://127.0.0.1:19539'
uv run --inexact --extra dev --extra milvus pytest -q -m external
```

测试只创建随机命名的 `rag_e_test_*` collection，并在结束时删除这一临时 collection，不使用业务 collection。

## 人工验收流程

1. 默认演示模式登录，确认界面明确标记“本地演示模式”。
2. 导入 `data/documents/examples/` 中的合成资料；检索 `青禾项目`、`Milvus`，展开命中来源。
3. 分别使用自适应 RAG 和 Agent 模式提问，观察节点进度、最终来源和历史会话。
4. 刷新页面、重新登录，历史仍存在。切换普通用户，确认只能管理获授权的资料，不能查看他人会话。
5. 删除测试文档后重试相同检索，确认不会返回已删除来源。
6. 接入真实模型后检查领域路由、改写、拒答、Tavily / Exa 来源 URL 和业务质量；演示模式通过不代表模型质量通过。联网检索专项测试见 [WEB_SEARCH.md](WEB_SEARCH.md)。

## 外部集成

CI 验证依赖锁、格式、测试、前端构建和两个容器镜像构建；后端作业提供临时 PostgreSQL 16，图谱集成作业提供 Neo4j/Redis。触发器为 `workflow_dispatch` 与 `pull_request`：普通 push 不单独运行；创建、重新打开或更新 PR 提交时自动运行。工作流进入默认分支后，可从 **Actions → RAG-E CI → Run workflow** 手动运行。
Docker/Kubernetes/真实模型的实际运行结果应与单元测试结果区分记录，不能用模拟测试替代外部服务验收。
