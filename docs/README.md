# 文档导航

[返回项目首页](../README.md)

## 安装与使用

| 文档 | 适用场景 |
|---|---|
| [快速开始](../README.md#快速开始) | 在本地或 Docker 中体验演示模式 |
| [真实模型接入](QUICKSTART.md) | 配置 LLM、Embedding、Milvus 和文档导入 |
| [知识库范围](KNOWLEDGE_SCOPE.md) | 单 / 多库问答、会话范围与授权 |
| [模型精排](RERANK.md) | 在线 API、本地 CrossEncoder、候选与异常策略 |

## 设计、部署与安全

| 文档 | 内容 |
|---|---|
| [系统架构](ARCHITECTURE.md) | 分层、数据流、工作流与能力边界 |
| [图谱、性能与可观测性](GRAPH_PERFORMANCE_OBSERVABILITY.md) | Neo4j、缓存、并发、指标、告警与追踪 |
| [账号与安全](SECURITY.md) | JWT、RBAC、资源授权、防重放、审计和加密 |
| [内容审查](MODERATION.md) | 审查阶段、分类阈值、拦截与故障处理 |
| [Docker 部署与维护](DEPLOYMENT.md) | 生产配置、持久化、迁移和备份 |
| [Kubernetes 模板](../k8s/README.md) | 清单使用方式和未验收边界 |
| [版本升级与索引重建](MIGRATION.md) | schema 迁移、模型切换、重建与恢复核验 |

## 评测

| 文档 | 内容 |
|---|---|
| [共享 RAG 参数](SHARED_PIPELINE.md) | 模型、分块与检索规则如何在生产和评测之间对齐 |
| [评测配置与运行](EVALUATION.md) | 配置模板、连接探针、小样本、正式运行和恢复 |
| [评测方法](EVALUATION_PLAN.md) | 数据范围、对照设计、第三方指标库和评分限制 |
| [2026-10-11 最终结果](FULL_EVALUATION.md) | T2 一万篇子集、CRUD QA 全部问题、四组图表 |
| [质量诊断](QUALITY_DIAGNOSIS.md) | 覆盖审计、失败样本和 36 组真实模型对照 |

图表与汇总数值位于 [assets/evaluation](assets/evaluation/README.md)。原始数据集、逐题答案、日志和本地数据库由运行者自行生成，不随仓库发布。文档中的 `test-results/...` 是本地输出路径，不是可下载的仓库附件。

## 开发与发布

- [测试说明](TESTING.md)
- [最近一次发布验收](VERIFICATION.md)
- [贡献与开发约定](CONTRIBUTING.md)
- [GitHub 发布与文件边界](PUBLISHING.md)
- [历史记录](archive/README.md)：早期小样本、存储后端对照和阶段验收，不代表当前最终成绩。

`docs/local/` 留给个人运行记录，已被 Git 忽略。公开文档使用相对链接和可复现命令，不依赖某台电脑的盘符、用户名或已有运行结果。
