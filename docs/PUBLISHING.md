# GitHub 发布说明

发布名称为 **知恒 · Continuum**，GitHub 仓库名建议使用 `Continuum`。仓库根目录应为包含 `backend/`、`frontend/` 和本项目 README 的独立项目目录；现有本地文件夹名可继续使用，无需移动文件。不要把包含其他实验和 `.env` 的父目录作为仓库。README 面向首次访问者；`docs/` 适合一起发布，详细指南与图表使用仓库相对链接。

## 文件边界

| 随仓库发布 | 仅本机保留 |
|---|---|
| 后端、前端、测试、迁移、锁文件 | `.venv/`、`node_modules/`、缓存、构建输出 |
| README、当前指南、带历史标记的归档 | `docs/local/` 个人运行记录 |
| 评测汇总 JSON、PNG / SVG 图 | 数据集正文、逐题答案、原始日志、数据库 |
| `.env.*.example`、`config/*.example.json` | `.env`、`.env.eval`、`.env.production`、实际 JSON 配置 |
| 合成演示资料 | 未授权公开的历史资料、PDF / 解析 JSON、真实业务文档 |
| MIT 与第三方版权说明、CI 和部署模板 | 数据加密密钥、真实 Kubernetes Secret、指标令牌 |

已撤下的另机汇总图和一次性绘图脚本也已排除。README 仅展示本机 T2 检索结果，完整历史实验仍在文档中单独保存。

`.gitignore` 不会取消已经跟踪的文件。检查器会同时查看已跟踪文件与未忽略的新文件，因此已误入索引的受保护文件仍会报错。不要用 `git add -f` 绕过排除规则。

## 发布前检查

安装 Git 与 Python 后，在项目根目录执行：

```bash
python scripts/check_publication.py
```

检查器不修改暂存区；没有 Git 仓库时使用临时 Git 元数据读取同一 `.gitignore`。检查内容包括受保护路径、常见 API Token / 私钥格式、较大文件，以及 Markdown 链接是否指向真正随仓库提供的文件。报告只打印位置和类别，不回显疑似凭据。

这不是完整安全审计，也不会扫描尚未创建的远程仓库或 Git 历史。已有历史的仓库需要另外检查历史提交；发现真实凭据泄露，应先撤销和轮换，而非仅删除当前文件。

按[测试指南](TESTING.md)运行相关测试。首次公开前核对 README、LICENSE、第三方声明、配置示例，以及不应被误称为生产验收的实验结果。当前 Kubernetes 是模板，远程 CI 结果以 GitHub 实际执行为准。

最近一次本地发布验收见[验证记录](VERIFICATION.md)。测试结论对应注明的时间与源码快照；修改代码后应重新运行受影响检查。旧压缩包不会随着目录修改自动更新，直接发布时以当前项目目录为准。

## 首次提交

在 GitHub 创建空仓库后，可从本项目根目录执行：

```bash
git init -b main
git add .
git diff --cached --stat
python scripts/check_publication.py
git commit -m "Initial public release of Continuum"
git remote add origin https://github.com/<owner>/<repository>.git
git push -u origin main
```

将占位地址替换为实际仓库。已有仓库使用现有分支和 remote，不重复初始化或覆盖历史。提交前人工检查 `git diff --cached`；共享评测图表和汇总即可，不需要上传整个 `test-results/`。

CI 不因普通 push 自动执行。工作流文件进入默认分支后，可在 **Actions → Continuum CI → Run workflow** 手动运行；PR 创建、重新打开或更新提交时仍自动检查，包括向已有 PR 的分支推送新提交。

## 后续维护

- README 保留项目定位、功能、启动、模型配置入口和评测摘要。
- 当前指南放在 `docs/`；过时实验放在 `docs/archive/` 并注明历史范围。
- 本地运行记录放在被忽略的 `docs/local/`，不在公开指南写机器用户名或绝对盘符。
- 评测发布器只更新指定标记区与图表；正文的方法、日期和范围随实验变更人工审阅。
- 公开模板修改后校验干净环境能够运行；本机实际参数不自动覆盖。
