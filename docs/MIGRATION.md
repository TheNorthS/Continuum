# 版本升级与索引重建

本页说明 RAG-E 现有部署如何升级数据库、切换模型和重建索引。首次安装见[快速开始](../README.md#快速开始)，生产操作顺序见[部署指南](DEPLOYMENT.md)。

## 升级前准备

1. 记录当前代码版本、依赖锁文件、环境配置、RAG 参数指纹及索引名称。
2. 备份业务数据库、原始资料和有状态服务数据。加密密钥独立备份，不与数据库放在同一信任域。
3. 停止 API worker、导入任务及其他写入进程，在独立环境验证升级和恢复流程。
4. 阅读待应用的数据库迁移，确认字段变化、数据转换和回退条件。

普通配置或版本更新不应删除数据库卷。密钥丢失不能通过重新生成文件恢复已有密文。

## 数据库 schema 升级

RAG-E 使用 Alembic，迁移位于 [backend/app/database/migrations/versions](../backend/app/database/migrations/versions)。当前依次包含基础表、索引配置记录、图谱状态、安全与知识库授权、会话范围五个版本。

在 `backend` 目录检查开发或独立测试数据库：

```bash
uv run --inexact alembic current
uv run --inexact alembic heads
uv run --inexact alembic upgrade head
uv run --inexact alembic check
```

这些命令默认读取项目 `.env` 与进程环境。不要把开发命令误用于生产；生产应通过指定 `.env.production` 的 [Compose 组合命令](DEPLOYMENT.md#生产配置)或迁移 Job 执行。

开发模式仅在 `RAGE_AUTO_MIGRATE=true` 时启动自动升级。生产必须关闭自动 schema 迁移，由单个维护任务先升级，再启动应用。已有账号不会因为修改初始化管理员配置而重置密码。

旧版明文字段、数据密钥和安全索引的转换涉及数据重写，按[安全升级说明](SECURITY.md#6-现有数据迁移与生产部署)执行。SQLite 可使用项目根目录的 `scripts/security_upgrade.py`；执行前确认目标配置并停止写入。脚本不是 PostgreSQL 备份工具。

## 更换 Embedding 或分块规则

以下变更需要重新处理文档并建立兼容索引：

- Embedding 模型、供应商端点、向量维度变化。
- 语义分块开关、分块长度或重叠规则变化。
- 检索后端或影响索引结构的参数变化。

建议流程：

1. 保留现有数据库和索引，准备新数据库、Milvus collection 和所需密钥配置。
2. 修改本地 `config/rag.production.json`，同步引用它的服务配置。
3. 从原始 Markdown / TXT 重新导入，核对文档数、分块数、索引状态与检索结果。
4. 在独立评测 run 中验证质量，确认后切换服务；保留旧配置与数据作为恢复方案。

系统通过配置签名阻止不兼容向量混用。当前不提供在已有业务库上自动完成的跨模型在线迁移；不要为通过签名检查而手改数据库记录。

## 图谱与缓存

图谱构建状态与向量索引状态独立。启用 Neo4j 后，可以在界面重建单份文档，或在配置正确的后端目录执行：

```bash
uv run --inexact python -m app.cli graph-rebuild --document-id <文档ID>
```

省略 `--document-id` 会处理所有就绪文档。`live` 模式的图谱抽取会调用模型；调整抽取规则后核对原文证据和权限范围。更换图谱 namespace 时保留原 namespace，避免不同环境共用业务图谱。

缓存版本包含相关资料和模型信息，删除或重建资料后通过版本变化失效。多 worker 需统一重启以加载新配置；不使用清空业务数据库来代替缓存管理。

## 升级后核验

- 迁移版本为预期 head，`alembic check` 无结构漂移。
- 使用签名客户端验证登录、知识库授权、导入、检索和会话引用。
- 检查 `/api/v1/health/ready`、文档 `ready` 状态及启用时的图谱状态。
- 生产与评测继续共用算法参数时，在 `backend` 执行 `uv run --inexact python -m app.evaluation.audit`，核对两边指纹。
- 恢复测试使用同一版本的数据库、索引和密钥；不假定数据库 downgrade 可以逆转数据加密或模型空间变更。
