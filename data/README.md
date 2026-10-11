# 数据目录

`documents/examples/` 提供随项目发布的合成演示资料，默认初始化脚本可直接导入。示例中的流程、项目和指标仅用于体验问答与图谱，不代表真实组织。

自行添加资料时，上传 UTF-8 Markdown / TXT；公开前确认有权分享资料内容。不要提交真实业务资料、账号数据或模型凭据。

以下目录仅在开发机器保留，已被 Git 忽略：

- `documents/md/`：仅本机保留的历史 Markdown 资料。
- `legacy/`：原始解析 JSON 与 PDF，未自动导入索引。
- `runtime/`、SQLite 数据库：本地服务生成的数据。

评测数据下载到项目 `.cache/evaluation/datasets/`，逐题输出位于 `test-results/evaluation/`，均不随仓库发布。数据集范围见[评测方法](../docs/EVALUATION_PLAN.md)，提交范围见[发布说明](../docs/PUBLISHING.md)。数据集、模型及导入资料适用各自条款。
