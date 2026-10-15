# 生产与评测共用 RAG 配置

模型、分块、检索、精排、审查与重试规则集中在同一 JSON；生产与评测分别配置凭据和存储。参数相同不代表基础设施、容量或 HTTP 并发性能相同。

## 首次配置

从项目根目录执行，只在目标文件不存在时复制：

```powershell
Copy-Item config/rag.production.example.json config/rag.production.json
Copy-Item .env.eval.example .env.eval
Copy-Item .env.production.example .env.production
```

macOS / Linux 使用 `cp`。公开模板为 [rag.production.example.json](../config/rag.production.example.json)；本机 `rag.production.json` 和环境文件已被 Git 忽略。不要把 API Key 写进 JSON。

| 文件 | 用途 |
|---|---|
| `config/rag.production.json` | 当前机器实际使用的 RAG 算法与模型参数 |
| `.env.eval` | 评测 Key、依赖连接与独立测试账号 |
| `.env.production` | 生产 Key、存储、HTTPS / 代理与安全凭据 |
| `.env.eval.example` / `.env.production.example` | 无真实密钥的公开环境模板 |

两个环境文件均设置：

```dotenv
RAGE_PIPELINE_PROFILE=config/rag.production.json
RAGE_LLM_API_KEY=your-deepseek-key
RAGE_EMBEDDING_API_KEY=your-embedding-provider-key
RAGE_RERANK_API_KEY=your-siliconflow-key
```

## 模板基线

| 环节 | 配置 |
|---|---|
| 回答与语义检查 | DeepSeek `deepseek-flash`，非思考模式 |
| Embedding | OpenAI API、`text-embedding-3-small`、1536 维 |
| 精排 | 硅基流动 `Qwen/Qwen3-Reranker-0.6B` 在线 API |
| 分块 | Markdown 章节 → 长章节语义分组 → 1500 字符长度兜底；组内长度切分重叠 150 |
| 检索 | Milvus HNSW / COSINE + BM25，候选 32，生成 Top-K 4 |
| 图谱 / 联网 | 关闭；需要时显式开启并重新评测 |
| 缓存 | L1 256 条、TTL 10 秒；L2 TTL 300 秒 |
| 模型 / 精排 / 审查并发 | 2 / 2 / 4 |
| 超时 | LLM 60 秒、精排 60 秒、审查单请求 30 秒 / 单次检查 60 秒、整轮问答 150 秒 |
| 内容审查 | live 语义审查与八类阈值；故障不放行 |
| 失败策略 | 精排失败报错；改写和生成次数均有上限 |

模板不是已证明最优的超参数。公开模板使用 OpenAI 官方地址；已发布的历史评测通过兼容服务调用相同名称的 Embedding 模型，端点与参数指纹不同。模板不会覆盖已有本机配置，也不保证重现历史结果的全部数值。使用兼容服务时，在本机 JSON 配置其地址并使用对应凭据。

## 配置一致性

上下文窗口、压缩预算与记忆检索策略也纳入共享参数指纹；旧 profile 自动补齐默认值，变更需在 JSON 显式设置。tokenizer 本地路径、计数接口与 Key、后台调度参数按部署单独配置。历史评测未覆盖此记忆实现，新对照应使用新 run，不能直接改写旧报告。

启用共享 JSON 后，不要在环境文件重复定义模型名、分块大小等字段。重复项与 JSON 冲突时启动会拒绝，而非静默覆盖。API Key、数据库地址、collection、namespace 不属于共享算法参数。

后端目录执行以下离线检查，不调用模型或连接数据库：

```bash
uv run --inexact python -m app.evaluation.audit --output ../test-results/pipeline-alignment.json
```

`aligned=true` 且两个 SHA-256 指纹相同表示有效 RAG 参数一致。API `/system` 也提供 `pipeline_fingerprint`，评测 manifest 保存参数和实现指纹。审计通过不表示 HTTPS、备份或生产数据库已经部署验收。

联网策略也纳入指纹，Tavily / Exa 密钥仍仅放环境文件。旧版 `version=1` JSON 缺少新增联网字段时，按[联网默认值](WEB_SEARCH.md#参数)补齐，原有联网开关保持不变；如需改动，在 JSON 中显式添加字段。升级后的策略 / 实现指纹与历史 run 不同，后续评测使用新 run，不改写历史报告。仅增加或修改联网配置无需重建知识库向量索引。

## 隔离与重建

- 评测独立使用 SQLite、数据密钥、账号与 JWT；生产模板使用 PostgreSQL。
- 每个评测 run 生成独立 Milvus collection 和 Neo4j namespace，不写入生产集合。
- 评测和业务使用独立 Redis 数据库；评测不启用外部 Langfuse 正文上传。
- 修改 Embedding 模型、端点、维度或分块后，使用新 run / 数据库 / collection 重新处理文档。旧索引不兼容时拒绝启动，不自动删除业务数据。
- 只有原文、模型空间、分块与实现兼容时才能显式复用既有向量；新安装不需要历史 run。

生产 Compose 通过 [docker-compose.production.yml](../docker-compose.production.yml) 挂载本机 JSON；Kubernetes 使用构建时放入镜像的 JSON。详情见[部署](DEPLOYMENT.md)与 [Kubernetes](../k8s/README.md)。

以上表格描述公开模板；未启用 profile 时的代码默认值可能不同。版本升级、密钥和索引切换另见[升级指南](MIGRATION.md)。
