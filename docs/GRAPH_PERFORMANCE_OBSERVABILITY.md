# 通用图谱、性能工程与全链路可观测性

## 通用知识平台

系统面向用户上传的各领域资料，可用 `RAGE_KNOWLEDGE_DOMAIN` 描述知识范围。网页选库问答始终限定在所选且获授权的资料中。
随仓库提供的合成资料位于 `data/documents/examples/`，独立图谱示例 `data/sample/knowledge_graph_demo.md` 包含企业、科学研究、项目管理三个领域的关系。

## Neo4j 知识图谱

以下命令用于读取 `.env` 的开发 Compose 栈。先从 `.env.example` 创建 `.env`、配置 Neo4j 密码，并准备或导入文档，再在根目录执行：

```bash
docker compose --profile graph -f docker-compose.yml -f docker-compose.graph.yml up -d --build
docker compose --profile graph -f docker-compose.yml -f docker-compose.graph.yml exec backend python -m app.cli graph-rebuild
```

`.env` 设置 `RAGE_NEO4J_PASSWORD`；Compose 图谱覆盖文件会启用图谱并使用内部 Bolt 地址。前端“知识图谱”可以搜索实体、拖动/缩放节点、查看关系原文、选择起终点探索最多三跳的路径，管理员可以重建每份文档的图谱。

使用共享 RAG JSON 时，需在 JSON 中设置 `graph_enabled=true`，环境文件保留服务地址和密码。共享模板默认关闭图谱，不能用冲突的环境变量直接覆盖；开启后若需要生产/评测参数一致，应建立新的评测运行。生产部署方式见[部署指南](DEPLOYMENT.md)。

本机开发可单独启动 Neo4j/Redis，使用测试端口（Compose ≥2.24.4 支持 `!override`）：

```bash
docker compose -p rag-e-engineering --profile graph -f docker-compose.yml -f docker-compose.test.yml up -d neo4j redis
```

对应后端配置：

```dotenv
RAGE_GRAPH_ENABLED=true
RAGE_NEO4J_URI=bolt://127.0.0.1:17687
RAGE_NEO4J_USERNAME=neo4j
RAGE_NEO4J_PASSWORD=rag-e-local-neo4j
RAGE_NEO4J_DATABASE=neo4j
RAGE_REDIS_URL=redis://127.0.0.1:16379/0
```

已有 Neo4j 卷的密码不会因改环境变量自动重置。生产环境使用自己的密码与受控网络。

### 抽取、推理与溯源

- `live` 模式由模型识别实体类型、实体名称、关系、逐字证据。名称和证据必须在原文出现；不合格关系丢弃。此校验能约束依据，但不能保证模型对语义的理解完全正确。
- `demo` 模式仅提取 `实体 --关系--> 实体` 三元组，明确标记为演示；普通自然语言抽取需要真实 LLM。
- 每条边保存 document_id、chunk_id、filename、evidence；文档删除会清理自己的边，共享实体仍保留。
- Neo4j 事务原子替换文档关系并更新图谱版本。SQL 单独记录 `graph_status`，失败可重建，不把向量入库成功伪装成图谱成功。
- 图查询仅使用有边界的参数化模板；不提供任意 Cypher 执行接口。缓存 key 包括图谱版本、SQL 就绪文档集合、查询和参数，重建/删除自动失效。
- 路径查询返回边方向与证据。只有同方向 `子类于` / `subclass_of` 链应用传递规则并单独返回推理结论；其他关系只作为关联路径。
- 检索时命中实体后扩展 1–3 跳，取回原始 chunk 并与向量/BM25 排名融合。图数据库临时不可用时保留向量检索，Cypher 错误指标和依赖告警显示异常。

图谱抽取上限为 200 块/文档，批量 32；模型并发上限在代码默认配置中为 8，在当前生产/评测模板中为 2。整次图谱索引受请求超时控制，单进程的文档级图谱索引串行执行。`RAGE_KNOWLEDGE_NAMESPACE` 用于独立部署/测试图谱隔离；用户和知识库授权由 RBAC / ACL 另外检查。

## 性能工程

`Container` 在 FastAPI lifespan 创建：每进程一个检索服务、Embedder、图谱 driver、HTTP client、缓存与监控实例；请求复用这些资源，关闭时统一释放。不同 worker 有各自实例，Redis 和 Neo4j 共享。

缓存实际顺序是 L1 命中直接返回 → L2 命中回填 L1 → 都未命中才执行查询。L1 有容量/TTL/LRU 淘汰；回填 TTL 不超过 Redis 剩余有效期。Redis 故障保留 L1 服务，同 key 并发 miss 在进程内合并加载。

| 设置 | 代码默认 | 共享模板 | 含义 |
|---|---:|---:|---|
| `RAGE_CACHE_L1_SIZE` | 256 | 256 | 每进程 L1 条目上限，单条最多 1 MB |
| `RAGE_CACHE_L1_TTL` | 10 | 10 | L1 有效秒数 |
| `RAGE_CACHE_TTL` | 300 | 300 | Redis 有效秒数 |
| `RAGE_LLM_MAX_CONNECTIONS` | 32 | 32 | HTTP 连接池大小 |
| `RAGE_LLM_MAX_CONCURRENCY` | 8 | 2 | 跨请求共享的模型并发上限 |
| `RAGE_LLM_BATCH_SIZE` | 32 | 32 | 单批 payload 上限，结果按输入排序 |
| `RAGE_NEO4J_POOL_SIZE` | 16 | 16 | 异步 Bolt 连接池 |

共享模板指 [rag.production.example.json](../config/rag.production.example.json)。启用 profile 后以实际 JSON 为准，修改上述策略字段应在 JSON 中完成。

批量推理使用兼容模型服务的有界并行请求，非供应商离线 Batch API。文档评分和图谱抽取都实际使用批量接口；批次取消或失败会回收尚未完成的任务。
SQL、Milvus、本地模型和主要 CPU 分块/哈希计算放到线程池，HTTP/Redis/Neo4j 使用异步 I/O。监控事件循环调度延迟，不承诺所有硬件/负载下绝对零延迟或未测量的吞吐提升。

## Prometheus、Profiler 与告警

管理员进入“监控与追踪”。接口 `/api/v1/admin/observability` 返回当前进程最近 300 秒的操作样本、p50/p95/p99、缓存统计、依赖状态、告警变化和追踪。
Profiler 每操作最多 2000 样本，使用 nearest-rank 分位数；多副本总体分位数请通过 Prometheus histogram 聚合，而不是直接平均各进程的 p95。

后台任务每 5 秒独立检查；无需打开监控页面才能触发告警：

- 请求/LLM/检索/Cypher 错误率达到阈值或 p95 超时。
- 事件循环延迟、数据库/Redis/Neo4j/Milvus 不可用。
- 首次异常 `normal → pending`，持续 30 秒 `pending → firing`，恢复 `→ normal`。默认最少 5 个业务样本才评估错误率/延迟。
- 当前进程保存最近 200 条状态变化，Prometheus 导出状态和 firing 次数；告警通知渠道可接外部 Alertmanager，本版不自动发消息。

指标端点 `/api/v1/metrics` 支持管理员 JWT，或仅用于抓取的 `RAGE_METRICS_TOKEN`。该专用 token 不能访问图谱、用户或监控 API。
如启用内置 Prometheus profile，把同一 token 写入 `.env` 的 `RAGE_METRICS_TOKEN` 和 `deploy/prometheus/metrics.token`（此文件已 Git 忽略）：

```bash
docker compose --profile monitoring up -d prometheus
```

Prometheus 本机入口为 `http://localhost:9090`，不会公开绑定其他网卡。首次设置 token 后使用 `docker compose up -d --force-recreate backend` 重新创建容器以读取环境变量；仅修改环境变量无需重建镜像。生产使用对应的 `.env.production` 与覆盖文件组合。

## Langfuse

```dotenv
RAGE_LANGFUSE_ENABLED=true
RAGE_LANGFUSE_BASE_URL=https://cloud.langfuse.com
RAGE_LANGFUSE_PUBLIC_KEY=你的项目公钥
RAGE_LANGFUSE_SECRET_KEY=你的项目私钥
RAGE_TRACE_CAPTURE_CONTENT=false
```

可替换为自行托管的 Langfuse 地址。使用 SDK 4 的 observation API 和后台批量导出，退出时在线程池 flush/shutdown。
请求的 `X-Request-ID` 即 trace_id，工作流节点、检索、Embedding、Cypher、Web 搜索和 LLM generation 关联到同一 trace；LLM 记录模型、任务、耗时、错误类型和提供商返回的 token 用量。
默认不向 Langfuse 发送问题、文档和答案正文。非生产环境可显式设置 `RAGE_TRACE_CAPTURE_CONTENT=true` 采集适配器允许且已脱敏的内容；未经输出审查的生成候选仍不导出。生产配置禁止正文追踪。监控页的“已启用”表示 SDK 配置开启，不等价于远端已收到全部数据，应在实际 Langfuse 项目核验网络与凭据。

实现依据：[Neo4j 官方异步驱动](https://neo4j.com/docs/python-manual/current/concurrency/)、[Langfuse 官方 SDK](https://langfuse.com/docs/observability/sdk/overview)。
