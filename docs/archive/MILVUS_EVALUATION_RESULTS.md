> 历史记录：本页记录当时的环境、配置和测试结果，不代表当前安装状态或最终成绩。当前入口见[文档导航](../README.md)，最终结果见[公开数据集评测](../FULL_EVALUATION.md)。原始日志仅在本地生成。

# Milvus 部署后端：同样本复测结果

后续部署与评测以 **Milvus** 为准。已将 `.env.eval` 默认后端改为 Milvus，运行器支持 `--backend`，并按实验目录自动生成隔离 collection。早期 local 结果保留为基线。

## 已完成的实际配置

- 独立 Docker 项目：`rag-e-evaluation`，包含 Milvus、etcd、MinIO；本机 Milvus 入口为 `http://127.0.0.1:19539`，仅绑定回环地址。
- Milvus 实测版本：**2.6.9**。
- 稠密索引：HNSW / COSINE，`M=16`、`efConstruction=128`。
- 稀疏索引：原生 BM25，`k1=1.2`、`b=0.75`，SPARSE_INVERTED_INDEX。
- Strong 一致性；搜索 `ef=max(配置值, 单路候选数)`，当前 32 条精排候选对应实际 `ef=256`。
- 查询前 flush/load 并核验数量、全部 chunk ID；实际 dense/sparse 索引均为 Finished，无 pending rows。

| 实验 | 固定问题 / 文档 / chunk | 独立 collection |
|---|---|---|
| T2 | 20 / 200 / 293 | `rag_e_eval_1d072e26743b1231dd1c5a0d_secure` |
| CRUD | 12 / 100 / 100 | `rag_e_eval_6a6f18fcf41244948cfc0aa9_secure` |

文档的文本、分块、Embedding 值从旧评测库只读加载，重新使用新实验密钥写入目标 SQL 与 Milvus。源/目标全部文档的向量摘要一致；**文档 Embedding API 调用为 0**。问题向量重新调用 OpenAI，候选排名重新查询 Milvus，Qwen 精排和 DeepSeek 回答也重新运行。

SQL 仍保存文档、权限、分块及加密向量副本，Milvus 执行实际稠密与 BM25 检索。工作台 `.env` 和业务数据库未自动迁移；此次切换的是隔离评测环境，正式业务部署使用自己的 Milvus 地址、Token、集合和数据库。

## T2 检索结果

| 指标 | local RRF | Milvus RRF | local + Qwen | Milvus + Qwen |
|---|---:|---:|---:|---:|
| Recall@4 | 68.47% | 68.47% | 70.30% | 70.30% |
| Recall@10 | 94.25% | 94.25% | 96.80% | 96.80% |
| nDCG@10 | 0.9663 | 0.9663 | 0.9879 | 0.9879 |
| MRR@10 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |

主要指标一致，但逐题排名不完全一致：RRF 的 Top-10 顺序完全相同为 15/20 题，精排后为 14/20；两组后端 Top-10 文档集合的平均重合率都是 99.5%。指标相同不代表候选顺序相同。

这批题的 Recall@4 理论均值上限就是 70.30%，原因是部分问题有超过 4 篇正例。Qwen 在此小语料子集达到每题的该指标上限；不代表完整 T2 语料达到相同水平。

证据：Milvus T2 报告（本地 `test-results/evaluation/t2-milvus-pilot-20/report.md`）、后端对照（本地 `test-results/evaluation/t2-milvus-pilot-20/backend-comparison.md`）、索引详情（本地 `test-results/evaluation/t2-milvus-pilot-20/milvus-index.json`）、向量复用核验（本地 `test-results/evaluation/t2-milvus-pilot-20/vector-reuse.json`）。

## CRUD 问答与来源覆盖

| 指标 | local RRF | Milvus RRF | local + Qwen | Milvus + Qwen |
|---|---:|---:|---:|---:|
| 来源 Recall@4 | 91.67% | 94.44% | 94.44% | 94.44% |
| 来源 nDCG@10 | 0.9419 | 0.9292 | 0.9806 | 0.9806 |
| 来源 MRR@10 | 0.9444 | 0.9028 | 1.0000 | 1.0000 |
| 字符 F1 | 53.61% | 55.82% | 53.15% | 53.35% |
| 字符 ROUGE-L | 45.88% | 48.35% | 46.53% | 46.44% |
| 正常回答 / 资料不足 | 11 / 1 | 11 / 1 | 11 / 1 | 11 / 1 |

Milvus 原始融合排名发生变化：RRF 的 Top-10 顺序仅 1/12 题完全相同，平均文档集合重合率 92.5%；精排后提升为 6/12 题、98.33%。

在 Milvus 这轮中，精排改善 MRR/nDCG，但 Recall@4 不变，回答字符重合分低于该轮 RRF。不能据此认定精排无用或有害：样本只有 12 题，答案生成存在重复运行差异，来源顺序与过滤也影响表达；这些字符指标并不是语义准确率。

上轮“召回完整资料却拒答”的三文档题在两组中均再次发生。它仍被保留在分母中，没有删除后重算。全部查询和问答没有接口错误；拒答属于应用输出状态。

证据：Milvus CRUD 报告（本地 `test-results/evaluation/crud-milvus-pilot-12/report.md`）、后端对照（本地 `test-results/evaluation/crud-milvus-pilot-12/backend-comparison.md`）、逐题答案（本地 `test-results/evaluation/crud-milvus-pilot-12/queries.jsonl`）、索引详情（本地 `test-results/evaluation/crud-milvus-pilot-12/milvus-index.json`）。

## 性能与新增用量

| 项目 | T2 Milvus | CRUD Milvus |
|---|---:|---:|
| 初次候选召回 p50 / p95 | 0.82 / 2.37 秒 | 1.72 / 10.24 秒 |
| 精排增量 p50 / p95 | 0.62 / 0.96 秒 | 0.75 / 0.94 秒 |
| 新 Embedding 请求数 / Token | 20 / 312 | 12 / 665 |
| Qwen 精排请求数 / 输入 Token | 20 / 345489 | 12 / 161486 |
| DeepSeek（含审查评分）请求 / 总 Token | 未调用 | 386 / 347054 |

候选召回计时包括在线问题 Embedding，问答时间又复用了候选缓存；这些数据**不能用于宣称 Milvus 本身比 local 更快，也不是 QPS 压测**。生产容量需另做固定查询向量、不同语料规模及并发条件下的纯存储性能测试。

## 工程验证与下一步

- 后端全量 **174 passed、2 skipped**，真实 Milvus/Neo4j/Redis 均启用；跳过 PostgreSQL 与本地 CrossEncoder 外部项。
- 源数据库改为 SQLite `mode=ro` 之后，向量复用专项测试另通过；最终代码规范检查通过。
- 两个 Milvus run 的 TREC 导出已由 `ir_measures` 离线重新计分，与报告 Recall@4/nDCG@10 完全一致。
- 部署参数已落入配置：`RAGE_MILVUS_HNSW_M / EF_CONSTRUCTION / EF`；建索引参数变更应使用新集合。
- 本次 Milvus 容器与评测集合保留，便于继续扩样；不需要重新生成现有文档向量。

后续优先在 Milvus 上扩大题目与困难负例，并测量真实业务规模的纯检索延迟。所有基线、质量变化和部署后端分别记录；不再用 local 成绩代替目标部署成绩。

复现命令见 [评测配置](../EVALUATION.md)。离线后端比较命令：

```powershell
# backend 目录，不调用模型
.\.venv\Scripts\python.exe -m app.evaluation.compare_backends --local t2-pilot-20 --milvus t2-milvus-pilot-20
.\.venv\Scripts\python.exe -m app.evaluation.compare_backends --local crud-pilot-12 --milvus crud-milvus-pilot-12
```
