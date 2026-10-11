> 历史记录：本页记录当时的环境、配置和测试结果，不代表当前安装状态或最终成绩。当前入口见[文档导航](../README.md)，最终结果见[公开数据集评测](../FULL_EVALUATION.md)。原始日志仅在本地生成。

# 生产与评测参数统一：重建验收

## 最终确定的配置

当时生产和评测共同读取本机 `config/rag.production.json`，包括全部 **63 个 RAG 策略字段**。公开仓库仅提供[配置模板](../../config/rag.production.example.json)，模板端点与历史本机配置有差异。模型密钥、SQL/向量库地址、collection、域名与安全凭据分别存放，不混入参数文件。

| 项目 | 最终共同值 |
|---|---|
| 分块 | 语义分块开启，章节内上限 1500 字符，组内长度切分重叠 150 |
| 检索 | Milvus 2.6.9，HNSW/COSINE + BM25 |
| 候选 / 生成 Top-K | 32 / 4 |
| 精排 | 硅基流动 `Qwen/Qwen3-Reranker-0.6B` API |
| 回答/评分/语义审查 | DeepSeek `deepseek-flash`，非思考模式 |
| Embedding | `text-embedding-3-small`，1536 维，沿用用户此前成功运行的兼容服务地址 |
| 图谱融合 | 该轮生产和评测均关闭 |
| Web | 关闭 |
| 缓存 | 同一套 L1/L2 策略；评测使用隔离 Redis 数据库 |

最后校验指纹：

```text
dc851fcb803abde8096a4f42b581d9c8b4361eff072de8ae24cad0e9a230bb1b
```

以下四处一致：生产配置加载结果、评测配置加载结果、最终两个 run 的 manifest、已构建后端镜像内配置。差异列表为空。证据见 参数审计（本地 `test-results/pipeline-alignment.json`）。

## 实际改动

- 移除评测脚本强制设置 `semantic_chunking=False`、`chunk_size=1000`、候选 32、Top-K 4 等覆盖，直接使用共同参数。
- 明确参数优先级：启用共享文件后，其他来源出现不同算法值会拒绝启动，不允许静默漂移；运行前再次检查有效值。
- 生产 Compose / Kubernetes 引用同一 JSON，Dockerfile 实际打包该文件；环境密钥未进入镜像。
- 新增 `app.evaluation.audit`，不调用模型即可检查一致性；`/system` 和评测报告提供参数指纹。
- 索引签名加入语义分块规则。旧分块或旧模型索引不兼容时拒绝启动，要求新数据库/collection 重建，避免混用。
- Qwen 实验组使用应用真实 `KnowledgeService.search`；RRF 组仅作为明确的去精排消融对照，不冒充正式配置。

## 新索引与重跑

旧索引及报告保留。按照语义分块 1500/150 从原文重新调用 Embedding，T2 的分块从旧版 **293** 增至 **538**；CRUD 的资料都较短，仍为 **100** 个分块。

收到用户图谱关闭选择前，已按原生产模板完成了包含图谱的重建试跑。其 run 已标记为 `superseded`，不作为最终部署配置成绩。最终无图谱实验只复用那批**新语义分块及向量**，候选、精排和回答重新计算，避免重复嵌入费用；没有复用最早 1000 字符分块向量或图谱排名。

| 最终 run | 文档 / chunks | Milvus 集合 |
|---|---:|---|
| `t2-production-nograph-20` | 200 / 538 | `rag_e_eval_811741e0d8c249eb6a0a70f5_secure` |
| `crud-production-nograph-12` | 100 / 100 | `rag_e_eval_8bacb49891a87553d7e7f2e8_secure` |

Milvus 数量、全部 chunk ID 和 HNSW/BM25 完成状态已核验；新 run 的向量与重建后的语义向量摘要完全相同。最终库中全部文档 `graph_status=disabled`，没有参与图谱抽取或召回。

## 最终配置的小样本结果

| 数据 / 指标 | RRF 消融对照 | 正式参数：RRF + Qwen |
|---|---:|---:|
| T2 Recall@4 | 70.30% | 70.30% |
| T2 Recall@10 | 95.80% | 97.80% |
| T2 nDCG@10 | 0.9789 | 0.9932 |
| CRUD 来源 Recall@4 | 94.44% | 94.44% |
| CRUD 来源 nDCG@10 | 0.9132 | 0.9806 |
| CRUD 字符 F1 | 54.79% | 53.05% |
| CRUD 字符 ROUGE-L | 46.02% | 46.70% |
| CRUD 正常回答 / 资料不足 | 11 / 1 | 11 / 1 |

仍然是 T2 20 题/200 文档、CRUD 12 题/100 文档的子集试跑，不代表全量榜单或业务准确率。字符分数不是语义正确率；指标有升有降，参数统一的目的是让测试对象正确，而不是保证分数升高。

完整证据：

- T2 最终报告（本地 `test-results/evaluation/t2-production-nograph-20/report.md`）、参数快照（本地 `test-results/evaluation/t2-production-nograph-20/pipeline-profile.json`）、索引核验（本地 `test-results/evaluation/t2-production-nograph-20/milvus-index.json`）。
- CRUD 最终报告（本地 `test-results/evaluation/crud-production-nograph-12/report.md`）、逐题答案（本地 `test-results/evaluation/crud-production-nograph-12/queries.jsonl`）、参数快照（本地 `test-results/evaluation/crud-production-nograph-12/pipeline-profile.json`）。

## 验证与真实边界

- 最终全量后端回归 **184 passed、2 skipped**，包含真实 Milvus、Neo4j、Redis；跳过 PostgreSQL 和本地 CrossEncoder 外部测试。结果见 `test-results/shared-profile-regression.xml`。
- 生产 Compose 解析通过，后端镜像 `rag-e-backend:shared-profile` 构建通过，并在禁网容器中验证最终参数指纹、语义开关、分块长度及图谱关闭。
- `.env.eval` / `.env.production` 与密钥均不在镜像中，JSON 不含 API Key。
- 早期共享模板误将用户 Embedding 兼容端点改成官方地址，曾出现两次索引前请求拒绝；已按旧成功索引记录恢复端点，真实探针重新通过。失败 run 保留，不将其成绩混入最终结果。
- RAG 参数一致不等于生产基础设施已部署。当前质量评测元数据仍用隔离 SQLite，生产模板使用 PostgreSQL；域名、HTTPS、网关、并发容量和恢复演练仍需在目标服务器验收。
- 原工作台 `.env`、业务数据库及现有 `admin` 密码未修改，生产服务器/Kubernetes 未发布。

日常修改及审计命令见 [共享配置说明](../SHARED_PIPELINE.md)。后续改规则只改这一份 JSON，使用新的 run 和索引，不覆盖已完成的历史实验。
