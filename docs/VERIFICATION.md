# 发布验收记录

本页记录 **2026-10-11** 已完成的本地源码发布验收。测试使用不含个人配置、数据库和数据集的源码副本，并复用已安装依赖；不是全新操作系统安装、远程 GitHub CI 或生产容量验收。

## 检查结果

| 项目 | 已记录结果 |
|---|---|
| 后端静态检查 | Ruff、118 个文件的格式检查、uv 离线锁文件检查通过 |
| 干净源码普通回归 | 221 通过、6 个外部服务项跳过 |
| 真实依赖补测 | Neo4j / Redis / Milvus 相关 4 项通过、2 项跳过；合计 225 个不同后端测试通过 |
| 后端未执行项 | PostgreSQL 与本地 CrossEncoder 的测试环境未配置 |
| 前端 | 格式通过，33 通过、1 项跨语言 HTTP 测试跳过，TypeScript / Vite 生产构建通过 |
| 依赖声明 | 前端 package.json 与 package-lock.json 根声明一致 |
| Compose | 开发、生产、验证与图谱共 4 组配置解析通过 |
| Kubernetes | 17 个 YAML 对象解析通过，未部署到集群 |
| Dockerfile | 前后端 `docker build --check` 通过；该轮未完整重建镜像 |
| 启动脚本 | Linux `bash -n` 与 PowerShell 语法检查通过 |
| 空库演示 | 初始化成功；2 份合成文档、5 个分块导入；CLI verify 通过 |
| 公开文件 | 相对文件链接、个人路径、常见凭据格式及本机真实凭据比对通过 |
| 图表 | PNG 完整性与 SVG 解析通过，架构及流程图经过视觉检查 |

图像验收指文档图示，不代表本轮完成了真实浏览器中的应用界面验收。公开源码中不包含 API Key、业务数据库、加密密钥或原始评测正文。

## CI 与文档更新

验收后，CI 触发器已改为手动 `workflow_dispatch` 与 PR 自动运行；工作流作业保持不变，触发 YAML 与公开链接另行检查通过。普通 push 不单独触发，已有 PR 更新提交仍自动检查。远程结果以 GitHub Actions 实际运行记录为准。

本次文档整理统一了当前指南、共享模板默认值、架构图、升级步骤与导航；这些文档变更不代表重新运行了上述全套测试。后续代码和依赖变更需要按[测试指南](TESTING.md)重新验证。

## 可复核记录与边界

原始记录在验收机器本地的 `test-results/release-final/final-check.json`、`backend-clean-export.xml`、`backend-external.xml` 与 `report.md`，不随 GitHub 仓库分发。表中的数量属于注明时间的结果，不是持续变化的“当前通过数”。

该轮未完整重建 Docker 镜像、未部署 Kubernetes、未重新执行付费模型质量评测；已完成的公开数据实验另见[实验报告](FULL_EVALUATION.md)。更早阶段的记录见[历史归档](archive/README.md)，不能用当时的小样本或测试计数替代本轮结果。
