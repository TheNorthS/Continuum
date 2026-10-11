# 本地开发约定

先按 README 在本仓库安装独立依赖，配置文件使用项目根目录的 `.env` 或指定的环境入口。

首次创建生产 / 评测配置请从公开 `.example` 模板复制；测试直接使用模板，不依赖开发者的个人配置。项目采用 [MIT](../LICENSE)。

## 修改入口

- HTTP 参数与权限放在 `backend/app/api` 和 `dependencies.py`。
- 业务流程放在 `services`，LangGraph 边与节点放在 `agents`。
- SQL 数据访问集中在 `infrastructure/repositories.py`；不在路由中拼 SQL。
- 模型或搜索供应商接入放在适配器，不在模块顶层加载权重或发起网络连接。
- 前端页面调用 `services/api.ts`；来源统一由 `AnswerCard` 渲染，不直接注入检索到的 HTML。

## 数据库迁移

修改 SQLAlchemy 模型后，在后端目录运行：

```bash
uv run --inexact alembic revision --autogenerate -m "describe the change"
```

审阅生成的迁移，确认 upgrade/downgrade、默认值和历史数据兼容性，再在空库和已有测试库执行 `alembic upgrade head`。
迁移不得引用运行时模型来动态创建整套表，以保证历史版本稳定。生产发布之前先备份并单独执行迁移。

## 提交前检查

```bash
# backend
uv sync --frozen --extra dev --extra milvus --extra evaluation
uv run --inexact ruff check app tests
uv run --inexact ruff format --check app tests
uv run --inexact pytest -q
# frontend
npm run format:check
npm test
npm run build
```

项目根目录执行 `python scripts/check_publication.py`，检查待公开文件、Markdown 本地链接、常见凭据格式与体积；它不上传或修改 Git 暂存区。提交说明应写清问题、最终行为和实际验证结果，Issue / PR 附上脱敏复现步骤。

修改外部适配器时同时运行相关协议测试；有独立 Milvus 时启用 external 测试。
不要提交 `.env`、实际 Secret、数据库、模型权重、日志或构建缓存。变更运行行为时同步更新配置示例与部署文档。

## 文档维护

当前使用指南位于 `docs/`，按实际代码和公开模板描述行为。历史实验位于 `docs/archive/`，保留当时的范围、参数和成绩，不作为当前安装默认值。更新功能时同步检查架构图、配置表、CLI 工作目录和文档导航；个别机器的路径和运行状态不写成通用前提。

CI 支持 `workflow_dispatch` 手动执行，并保留 PR 创建、重新打开和提交更新时自动运行；普通 push 不单独触发。推送新提交到已有 PR 分支仍会触发 PR 检查。操作入口与发布边界见[发布说明](PUBLISHING.md)。
