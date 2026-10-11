# Kubernetes 部署模板

这里提供单副本应用、PostgreSQL PVC、Redis、探针、资源限制、迁移 Job 和 HTTPS Ingress。
Milvus 使用独立管理的 2.6.x 实例（原生 BM25），不把开发用单机对象存储集群伪装成高可用部署。

1. 构建后端/前端镜像并推到你自己的 registry；替换两个 Deployment 和 migration-job 中的 image。
2. 构建前从 `config/rag.production.example.json` 创建并配置本机 `config/rag.production.json`；此文件不提交 Git，构建时进入后端镜像，`configmap.yaml` 通过 `RAGE_PIPELINE_PROFILE` 引用。只在 ConfigMap 设置服务地址和域名；复制 `secrets.yaml.example` 到仓库外，填写 DeepSeek、Embedding、硅基流动及存储凭据。详细说明见[共享配置](../docs/SHARED_PIPELINE.md)。
3. `kubectl apply -f k8s/namespace.yaml`，再应用 ConfigMap 和私有 Secret。另创建 `rag-e-encryption` Secret（键名 `keyring.json`，内容来自离线安全初始化的密钥文件），不要提交实际密钥。按实际入口修改 `RAGE_PUBLIC_ORIGIN` 和 `RAGE_TRUSTED_PROXY_HOSTS`。
4. 应用 `data-services.yaml`、`networkpolicy.yaml`，等待 postgres/redis 可用，并确认外部 Milvus 可连接。当前共享配置关闭图谱，无需部署 Neo4j；将来两边共同启用图谱时再应用 `neo4j.yaml`，并保持 Secret 的 `NEO4J_AUTH` 与 `RAGE_NEO4J_PASSWORD` 一致。
5. 应用 `migration-job.yaml`，执行 `kubectl -n rag-e wait --for=condition=complete job/rag-e-migrate-v1 --timeout=120s`。
6. 应用 `backend.yaml`、`frontend.yaml`。配置 TLS Secret 和域名后再应用 `ingress.yaml`。
7. 使用浏览器管理员登录后上传原项目文档，或在带数据挂载的一次性 Job 中执行 `python -m app.cli ingest /data/documents`。

每次发布先评估数据库迁移并备份。单独 Job 执行迁移，业务副本不开启自动迁移。
当前 HTTP 限流在进程内实现，反向代理后会按代理 IP 聚合；面向公网或扩容时须在网关配置按用户/IP 的全局限流。
PostgreSQL 清单用于可控的小规模部署；生产高可用、备份与恢复需接入平台托管能力。
本目录是可审阅的模板，未向任何 Kubernetes 集群部署。
