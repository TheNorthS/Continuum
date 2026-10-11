# 评测发布产物

本目录可随 GitHub 仓库发布，包含经过审阅的汇总结果，不包含数据集全文、逐题答案、API 凭据或运行数据库。

| 文件 | 内容 |
|---|---|
| [metrics.json](metrics.json) | 两组聚合指标、范围、状态与延迟；CRUD 同时包含全量及正常回答题口径与分母 |
| [diagnosis-summary.json](diagnosis-summary.json) | 离线覆盖诊断与 12 题 × 3 条件对照的聚合数值 |
| `retrieval.png` / [SVG](retrieval.svg) | T2 检索指标和 Recall@K |
| `rerank-delta.png` / [SVG](rerank-delta.svg) | Qwen 精排配对增益与 bootstrap 区间 |
| `answers.png` / [SVG](answers.svg) | CRUD 全量文本重合、正常回答题条件质量及回答状态 |
| `latency.png` / [SVG](latency.svg) | 分阶段 p50 / p95 / p99 |

[结果与限制](../../FULL_EVALUATION.md) · [质量诊断](../../QUALITY_DIAGNOSIS.md) · [复现方法](../../EVALUATION.md)

图表由 `python -m app.evaluation.publish` 在完整性校验后从本地运行结果生成。诊断汇总对应 2026-10-11 的已完成诊断，不是新评测成绩。新运行应另存标识并同步方法与范围说明，不覆盖旧实验的口径。

