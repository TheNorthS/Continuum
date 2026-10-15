# 部署与维护

## Docker Compose

根目录先复制 `.env.example` 为 `.env`。默认开发演示：

```bash
docker compose up -d --build
docker compose exec backend python -m app.cli ingest /app/data/documents
docker compose logs -f backend
```

前端 8080、后端 8000 只绑定宿主机回环地址；PostgreSQL/Redis/Milvus/MinIO/etcd 不暴露宿主机端口。
数据库保存在命名卷。普通 `docker compose down` 不删除持久卷；不要把 `down -v` 当成常规重启命令。

## 会话租约

`RAGE_CONVERSATION_LEASE_SECONDS=180` 控制有效期，`RAGE_CONVERSATION_LEASE_RENEW_INTERVAL=30` 控制续租间隔；间隔必须不超过有效期的三分之一。全部 API worker 使用同一数据库判定时间与归属，不依赖单机内存锁。SQL 最终提交同时校验 token 和有效期，并消费提交资格；无需新增 schema 或重建知识库索引。

续租失败会停止生成。数据库不可用导致释放失败时，租约到期后可被新请求接管；生产应监控数据库连接、锁等待和 `conversation_lease_unavailable` 错误。续租不是延长模型执行预算，`RAGE_REQUEST_TIMEOUT` 仍约束问答生成。客户端在提交时断开可能已有结果持久化，应先查看会话历史再重试；跨请求幂等不由租约代替。

## 启用 Milvus

设置 `RAGE_VECTOR_BACKEND=milvus`。真实模型使用 `RAGE_MODE=live` 和 API embedding 配置（容器默认安装 Milvus 扩展，不内置重型本地模型）。
先启动并等候 Milvus 及其依赖，随后启动应用：

```bash
docker compose --profile milvus up -d --wait etcd minio milvus
docker compose --profile milvus up -d --build backend frontend
docker compose exec backend python -m app.cli ingest /app/data/documents
```

Compose 的 `RAGE_MILVUS_URI` 自动指向容器内 `milvus:19530`。远程服务请改该 environment 项。
仓库的 `deploy/minio` 从 [固定版本发布资产](https://github.com/minio/minio/releases/tag/RELEASE.2024-12-18T13-15-44Z) 获取二进制，并核对预先记录的 SHA-256，支持 amd64/arm64。下载使用固定资产 ID，校验失败立即拒绝构建。
该镜像用于独立单机开发部署；正式环境可接入你维护的 S3 兼容对象存储。

若 Docker/WSL 内下载很慢，Windows amd64 主机可先在宿主机预下载，再从缓存构建（两步均校验固定 SHA-256）：

```powershell
python deploy/minio/download.py amd64 .cache/minio/minio-amd64
docker build -f deploy/minio/Dockerfile.cached -t rag-e-minio:2024-12-18 .cache/minio
docker compose --profile milvus up -d --no-build --wait etcd minio milvus
```

arm64 主机将下载参数与文件名中的 `amd64` 改为 `arm64`。缓存只包含官方二进制，已被 Git 忽略。
如要使用本地 BGE，扩展后端镜像安装 `local-models` 依赖并挂载模型目录；不要在每次启动中下载权重。

## 生产配置

首次部署从 `config/rag.production.example.json` 复制本机 `config/rag.production.json`，从 `.env.production.example` 复制 `.env.production`；两个实际文件均不提交 Git。生产与评测共用 JSON 中的模型、分块和检索规则，凭据、域名与存储位置独立配置。Compose 入口为 `docker-compose.production.yml`，说明及校验命令见[共享 RAG 配置](SHARED_PIPELINE.md)。

1. 设置独立管理员密码、至少 32 字符随机 JWT 密钥、模型密钥、数据库密码。
2. `RAGE_ENVIRONMENT=production`、`RAGE_MODE=live`、PostgreSQL、`RAGE_AUTO_MIGRATE=false`；启动时会校验。
3. 使用下方生产组合命令启动依赖、单独迁移，再启动应用；生产配置关闭自动迁移。
4. 部署 HTTPS 入口。根据部署环境在网关设置全局限流、访问控制；内置进程限流不取代公网网关。
5. 根据数据规模选择 Milvus，并持久化其 etcd/MinIO/数据卷；为所有有状态服务配置备份和恢复演练。
6. Prometheus `/api/v1/metrics` 支持管理员 Bearer token 或 `RAGE_METRICS_TOKEN` 专用抓取令牌；专用令牌不能调用业务 API。图谱和监控部署详见 [专项指南](GRAPH_PERFORMANCE_OBSERVABILITY.md)。

数据库密码拼入 URI 时应使用 URL 安全字符，或自行对保留字符做百分号编码。
生产迁移是部署步骤，不让多个应用副本同时修改 schema。K8s 清单和顺序见 [k8s README](../k8s/README.md)。

```bash
# 根目录，已准备实际配置与凭据；HTTPS 入口需另行部署
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml --profile milvus config --quiet
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml --profile milvus build backend frontend
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml --profile milvus up -d --wait postgres redis etcd minio milvus
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml --profile milvus run --rm backend alembic upgrade head
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml --profile milvus up -d backend frontend
```

生产覆盖文件使用 Compose `!override`，要求 Compose 2.24.4 或更新版本。不能用只读取 `.env` 的开发命令替代上述生产组合。

数据库升级、切换模型和索引重建见[升级指南](MIGRATION.md)。代码默认值与生产共享模板可能不同，例如模型并发为 8 / 2、精排超时为 30 / 60 秒；生产有效参数以挂载的 JSON 为准。

## 数据备份与恢复

- SQLite：停止本地后端后备份 `data/rag_e.db`；或者使用 SQLite 在线 backup API。不要只复制正在写入的主文件而遗漏 WAL。
- PostgreSQL：使用 `pg_dump` / `pg_restore`，单独保存知识文档原文件。
- Milvus：SQL 保存全部分块文本和元数据；恢复完整索引还需要匹配同一模型版本重新导入原文件，或使用 Milvus 官方备份工具恢复全部有状态依赖。
- 修改 embedding 模型：使用新数据库和 collection 重新导入，保留旧数据作为回退。当前没有在线跨模型重建索引功能。

## 生命周期

FastAPI lifespan 管理 HTTP、Redis、SQL 与 Milvus 客户端关闭。
Uvicorn 正常退出会等待在途请求；K8s 给出 180 秒终止宽限期，覆盖默认 150 秒问答上限。
实时生成过程失败不会保存半条会话。浏览器取消会断开 SSE 并取消任务；已被外部提供商接受的调用可能仍产生费用。
