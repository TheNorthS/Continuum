# 公开数据集评测：配置与运行

先阅读[评测方法](EVALUATION_PLAN.md)，历史完成结果见[最终报告](FULL_EVALUATION.md)。本页命令中的模型探针、建库和问答会调用真实 API 并产生费用；配置审计和 `--report-only` 不调用模型。

## 1. 准备模型与独立配置

按[共享参数指南](SHARED_PIPELINE.md)从模板创建本机 `config/rag.production.json`、`.env.eval`、`.env.production`。在 JSON 中配置 DeepSeek、Embedding 和硅基流动精排的模型名与端点，在 `.env.eval` 填写各自 Key。

| 服务 | 模板端点 | Key |
|---|---|---|
| DeepSeek 回答与检查 | `https://api.deepseek.com` | `RAGE_LLM_API_KEY` |
| OpenAI Embedding | `https://api.openai.com/v1` | `RAGE_EMBEDDING_API_KEY` |
| 硅基流动 Qwen 精排 | `https://api.siliconflow.cn/v1/rerank` | `RAGE_RERANK_API_KEY` |

LLM / Embedding 填 API 根地址，由程序拼接 `/chat/completions`、`/embeddings`；精排填写完整路径。兼容服务必须使用对应端点与 Key。`embedding_dimension` 校验实际返回维度，不是请求降维参数。

## 2. 安装与连通性

在 `backend` 目录运行：

```bash
uv sync --frozen --extra dev --extra milvus --extra evaluation
uv run --inexact python -m app.evaluation.configuration
uv run --inexact python -m app.evaluation.audit
# 调用真实模型；只检查连接与协议，不给出质量成绩
uv run --inexact python -m app.evaluation.configuration --probe
```

三个模型环节均可在线运行，不需要安装 `local-models`。探针使用隔离存储，不建立完整评测索引。

## 3. 准备检索依赖

按[部署指南](DEPLOYMENT.md)准备 Milvus 与 Redis，并将它们的地址写入 `.env.eval`。若根目录尚无 `.env`，先从 `.env.example` 复制一份供 Compose 读取；已有文件保持原样。仓库的验证栈可从项目根目录启动（覆盖文件要求 Compose 2.24.4 或更新版本）：

```bash
docker compose -p rag-e-evaluation -f docker-compose.yml -f docker-compose.test.yml --profile milvus up -d --wait etcd minio milvus redis
```

该覆盖文件将 Milvus 暴露到本机 `19539`、Redis 到 `16379`，与评测环境模板一致。首次运行需要构建 MinIO 镜像；图谱关闭时不需要 Neo4j。不要把评测 collection 或密钥配置成业务资源。

## 4. 先跑小规模验证

回到 `backend`：

```bash
uv run --inexact python -m app.evaluation.run --dataset t2 --questions 20 --documents 200 --run-name t2-smoke
uv run --inexact python -m app.evaluation.run --dataset crud --questions 12 --documents 100 --run-name crud-smoke
```

这只是独立的小语料试跑。CRUD 子集会从 QA 附带新闻构建可控检索库，与 `--full` 新闻库的语料构建方式不同，不可直接比较分数。

## 5. 运行计划与断点恢复

公开计划为 [config/evaluation.example.json](../config/evaluation.example.json)：T2 10,000 文档 / 1,000 题，CRUD 45 个所选分片 / 2,394 QA。计划不要求预先存在可复用的旧 run。

```bash
# backend 目录；运行会下载公开数据并调用模型
uv run --inexact python -u -m app.evaluation.suite --query-concurrency 2
```

可复制模板为本机 `config/evaluation.current.json`，修改 suite / run 名称与规模，再通过 `--plan ../config/evaluation.current.json` 指定。该文件被 Git 忽略。恢复原运行必须使用原计划、配置和 run 名称；配置变化需要新 run。

大规模运行可显式传 `--runtime-root <有足够空间的目录>`，把 SQLite 与密钥放在其他磁盘；省略时使用默认隔离目录。不要照抄他人机器的盘符。

| 本地路径 | 内容 |
|---|---|
| `.cache/evaluation/datasets/` | 下载的数据集与预处理缓存 |
| `test-results/evaluation/<suite>/progress.json` | 套件进度 |
| `test-results/evaluation/<run>/progress.json` | 入库、索引核验与问题进度 |
| `test-results/evaluation/<run>/manifest.json` | 数据来源、ID、配置和处理口径 |
| `test-results/evaluation/<run>/queries.jsonl` | 逐题排名、答案、状态与耗时 |
| `test-results/evaluation/<run>/metrics.json` | 汇总指标 |

在 suite 目录创建 `STOP` 文件可请求安全停止；程序等待工作边界并保留事务断点。恢复前移除自己创建的 suite / run `STOP` 文件，再执行同一命令。系统重启不会自动续跑。磁盘或依赖故障要先修复，再恢复；错误题保留在分母，不自动覆盖成成功答案。

`--query-concurrency 2` 只控制同时处理的问题数；各题共享模型、精排与审查并发上限，不保证耗时减半。报告按并发阶段分别统计，缓存与阶段组成影响耗时。

## 6. 重新生成报告与图表

已有完成结果时，在 `backend` 执行：

```bash
uv run --inexact python -m app.evaluation.run --dataset t2 --questions 1000 --documents 10000 --seed 42 --run-name t2-10k-production-1000 --report-only
uv run --inexact python -m app.evaluation.run --dataset crud --full --run-name crud-full-production --report-only
uv run --inexact python -m app.evaluation.publish
```

发布器先检查计划覆盖、索引数量、参数及实现指纹，再更新文档：README 只展示 T2 检索摘要和图表，完整任务指标写入 `docs/FULL_EVALUATION.md` 与 `docs/assets/evaluation/metrics.json`。自定义计划需给发布器传同一 `--plan`。这里的“发布”只写本地文件，不上传 GitHub。

旧的已完成 run 可仅从保存的逐题结果补充“正常回答题质量与覆盖率”：

```bash
uv run --inexact python -m app.evaluation.answer_scope --run ../test-results/evaluation/crud-full-production
uv run --inexact python -m app.evaluation.publish
```

该操作不调用模型、不修改答案和原全量指标，新增 `answer_answered_only`、明确分母的 `answer_population`，以及记录原始逐题文件 SHA-256 的 `answer-scopes.json`。只有最终 `status=answered` 进入条件分母；原始失败仍保留在结果文件和状态统计中。

`--crud-official-directory` 配合 `--dataset crud --full` 可另建包含全部 48 文件的诊断运行，必须使用新 run；不能用它覆盖既有 45 文件基线。套件默认计划仍明确记录 45 文件口径。

公开仓库仅保存图表、聚合指标和方法说明。原始问题、新闻、答案和运行日志不自动提交，详见[发布规则](PUBLISHING.md)。
