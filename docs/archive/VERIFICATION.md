> 历史记录：本页记录当时的环境、配置和测试结果，不代表当前安装状态或最终成绩。当前入口见[文档导航](../README.md)，最终结果见[公开数据集评测](../FULL_EVALUATION.md)。原始日志仅在本地生成。

# 验证记录

本页汇总截至 2026-10-06 的阶段记录。各节数值和运行状态属于当时环境；最近一次源码发布验收见[2026-10-11 记录](../VERIFICATION.md)。

## 生产/评测参数统一与新分块复测

- 生产和评测共用 `config/rag.production.json` 的 63 个 RAG 参数，语义分块 1500/150、Milvus、候选 32、Top-K 4、DeepSeek/OpenAI 兼容 Embedding/硅基流动 Qwen，该轮两边关闭图谱。配置、镜像及最终 run 指纹一致。
- 已从原文重新生成语义分块向量，T2 为 538 chunks，CRUD 为 100 chunks；最终无图谱 run 全部图谱状态 disabled，索引已核验。
- 最终 T2/CRUD 精排后 Recall@4 为 70.30% / 94.44%，CRUD 每组 11 回答/1 拒答，完整报告见 [参数一致性验收](SHARED_PIPELINE_RESULTS.md)。
- 全量回归 184 项通过、2 项外部测试跳过，生产 Compose 解析与后端镜像构建通过。没有部署生产服务器或迁移工作台业务库。

## Milvus 部署后端评测

- 评测默认使用 Milvus、按 run 隔离 collection，源 local 索引只读复用分块与向量；配置、候选池、模型密钥均保留既有边界。
- 已实际启动独立 Milvus 2.6.9，完成 T2 293 chunks 和 CRUD 100 chunks 的 HNSW/BM25 构建、加载、数量及 ID 核验；文档向量摘要与原基线完全一致。
- T2 20 题和 CRUD 12 题均完成真实查询/精排，CRUD 重新执行完整问答；精排后 Recall@4 为 70.30% / 94.44%，CRUD 每组 11 回答、1 拒答。完整记录见 [Milvus 结果](MILVUS_EVALUATION_RESULTS.md)。
- 全量回归 174 项通过、2 项跳过（PostgreSQL/本地 CrossEncoder），含真实 Milvus/Neo4j/Redis；报告 `test-results/milvus-evaluation-regression.xml`。最终源库只读改动另通过复用测试。

## 公开数据集评测接入与首轮运行

- 已运行 T2Retrieval 20 题/200 文档、CRUD-RAG 12 题/100 文档的固定子集实验，使用 DeepSeek、OpenAI Embedding、硅基流动 Qwen 的真实 API。
- 精排共享相同候选池：T2 Recall@4 为 68.47% → 70.30%，CRUD 来源 Recall@4 为 91.67% → 94.44%。后者每组 11 题回答、1 题拒答；答案重合指标有升有降，不称为语义准确率。
- `ir_measures` / `rouge-score` 实际参与计分，原始语料、采样、qrels、逐题排名、答案、错误和用量保存在 `test-results/evaluation/`。模型 Key 不写报告，业务数据库没有被修改。
- 新增隔离评测运行器、候选池公共接口、断点恢复、纯离线报告、有限网络断开重试。全量回归 171 项通过、3 项外部项跳过；最终报告导出另通过 7 项专项测试。
- 首轮为小语料流程与诊断试跑，不可与官方全量榜单直接比较。详见 [实测报告](EVALUATION_RESULTS.md) 和 [评测方案](../EVALUATION_PLAN.md)。

## 独立模型精排

- 公共检索链路增加 RRF 候选池 → 本地 CrossEncoder / 兼容精排 API → 阈值 → Top-K，问答和检索试验台共用。精排前后均校验知识库范围、就绪状态与 ACL。
- API 使用独立地址/密钥，严格检查完整唯一索引与有限分数，仅按索引映射原始引用，不使用供应商返回的正文。支持超时、受限并发和可选有标记的 RRF 回退；取消不降级。
- 加密 L1/L2 新增精排分数缓存；Profiler、Prometheus、告警和 Trace 加入 `rerank`，不记录查询/片段正文。系统概览显示精排配置/加载/错误状态，引用展示独立的模型相关性分数。
- 该阶段安装了锁定的 `local-models` 依赖，并启用 `BAAI/bge-reranker-base` 的 CPU 精排。官方权重 revision 为 `2cfc18c9415c912f9d8155881c133215df768a70`；仅下载 safetensors 和分词配置到 `.cache/models/bge-reranker-base`，禁用远程自定义代码及请求期间联网下载。
- 后端回归 **153 项通过、3 项跳过**，含真实 Neo4j/Redis；该轮未设置 PostgreSQL/Milvus/本地模型外部测试变量。报告 `test-results/rerank-regression.xml`。本地模型另行真实推理 **1 项通过**，中文相关片段由末位提升首位，报告 `test-results/rerank-model.xml`。
- 实际前端代理上的签名 HTTP 验收通过：临时资料上传 → 真实精排检索 → SSE → 带分数引用 → Profiler/Prometheus → 清理临时资料和会话。报告 `test-results/rerank-http.json`；该次首个检索约 16.5 秒，包含本机加载/查询开销，不是吞吐基准。
- 禁用 Redis 的独立合成资料验收中，首次加载并评分约 16.714 秒，随后 32 条候选精排约 12.679 秒；报告 `test-results/rerank-local.json`。数据规模、长度、CPU 负载和缓存会改变耗时，此结果只对应本机本次输入。
- 前端 **33 项通过、1 项运行中服务签名测试跳过**；新增精排配置状态、分数与回退提示测试，TypeScript/Vite 构建和格式检查通过。未新增真实浏览器视觉验收。
- 主问答器仍为 `demo`，本地精排使用真实模型；供应商精排 API 已完成协议/异常测试，未使用用户凭据调用付费端点。安装、配置和边界见 [精排指南](../RERANK.md)。

## 知识库范围与界面优化

- 聊天页支持搜索、单选/多选知识库、保存范围、每条回答的范围标记、无权限和无知识库提示。切换范围保留历史显示，模型上下文从切换处重新开始。
- Repository 层将任务局部范围与实时 ACL 取交集，覆盖检索、图谱召回和缓存；选库模式禁止网络回退，返回前再次校验每条引用归属。
- 知识库页新增按库列表、当前库统计、文件名搜索和空状态；上传、检索试验台及“向此库提问”使用同一当前库。
- 后端 **120 项通过、2 项跳过**，包含真实 Neo4j 与 Redis L2 选库隔离；PostgreSQL/Milvus 外部项目本轮未配置。报告 `test-results/knowledge-scope-regression.xml`。
- 本地前端代理上的真实签名 HTTP 验收通过，覆盖检索、SSE、范围持久化、无效范围拒绝及引用检查；报告 `test-results/knowledge-scope-http.json`。本地现有库为 1 个，多库用独立测试数据库与图谱 namespace 验证。
- 本地后端迁移至 `0005`，`alembic check` 无结构漂移；迁移及验收前后均保留 1 会话、2 消息、8 文档。仅清理验收脚本自己创建的会话。
- 前端 **30 项全部通过**，含组件交互、WebCrypto 与运行中后端的签名互通；TypeScript/Vite 构建通过。浏览器工具返回 `Codex auth token is unavailable`，本轮未进行真实浏览器视觉验收。
- 该阶段使用演示模式，尚未进行真实模型回答质量验收；之后的真实 API 评测记录见本页前部与[最终报告](../FULL_EVALUATION.md)。操作与接口说明见 [知识库范围](../KNOWLEDGE_SCOPE.md)。

## 有害内容审查升级

- 已实现独立输入/上下文、检索与 Web 资料、生成/直接回复及最终输出审查。SSE 只发送安全步骤和审核后的结果。
- 本地签名 HTTP 验收通过：风险问题拦截、SSE 拦截、本轮历史为空、后续正常问答继续、指标导出；`test-results/moderation-http.json`。
- 内容审查专项 **57 项通过**（`test-results/moderation.xml`），含真实 LLM 适配器的未审核正文追踪隔离。后端/前端镜像实际构建通过。
- 最终整体回归 **108 项通过、2 项跳过**；Neo4j/Redis 真机项目启用，PostgreSQL/Milvus 外部测试本轮未启用（此前记录保留）。完整报告 `test-results/moderation-regression.xml`。
- 两种 RAG 模式和原资料检索的正常 HTTP 流程通过；`test-results/moderation-normal-http.json`。原始样例文件字节校验保持一致。
- 前端 22 项通过，包含拦截状态、无引用泄漏及 WebCrypto 与运行中 Python 服务互通，TypeScript/Vite 构建通过。
- 该阶段为 `demo`，启用本地规则基线；真实模式语义分类已做供应商协议/阈值/格式/失败测试，但没有使用用户真实模型凭据验证分类准确率。详见 [内容安全审查](../MODERATION.md)。

下面保留此前安全和工程改造的历史记录；其中的测试数对应当时版本。

## 安全体系升级

- JWT 认证保留并升级为服务端会话、公钥绑定请求证明、单次 refresh 轮换、停用/改密/撤销即时失效；生产 HTTPS 与数据库中的默认演示密码检查。
- 新增账号管理、自定义 RBAC、私有知识库、用户/角色 ACL、检索/图谱/缓存授权过滤、历史引用撤权隐藏和最后管理员保护。
- SQL 业务字段、Redis/L1、Neo4j 文本与 Milvus payload 加密；Milvus BM25 使用 HMAC 词项，语义向量保留用于 ANN；加密覆盖与元数据泄露边界见 [安全指南](../SECURITY.md)。
- 新增脱敏审计链、完整性验证、浏览器非导出私钥存储、真实 WebCrypto/multipart 签名测试和离线密钥轮换测试。
- 本机已生成并核验加密备份 `data/backups/pre-security-20261001140555.rage-backup`；迁移前后均为 1 用户、8 文档、115 chunks（会话与消息为 0）。密钥位于 `.secrets/keyring.json`，不提交 Git，NTFS ACL 已限定为当前运行账号。
- **后端 53 项全部通过，无跳过**，包括 PostgreSQL、Milvus、Neo4j、Redis 四类真实服务；结果写入 `test-results/security.xml`。前端 **21 项通过**，含真实 WebCrypto、multipart 签名及运行中 Python 服务的跨语言签名互通；TypeScript/Vite 及前后端 Docker 镜像构建通过。Windows 密钥轮换保留 ACL 的新增分支另行通过离线轮换测试。
- 本机签名 HTTP 验收通过：账号/角色/授权接口正常，审计链完整性有效，重复 proof 返回 409、单独 Bearer 返回 401；图谱保留 16 实体/12 关系。报告 `test-results/security-http-final.json`。
- 安全容器栈（Nginx → FastAPI → PostgreSQL/Redis）也已通过签名登录、导入、两种问答与代理验收，独立密钥卷正常；`test-results/security-container-http.json`。验收后关闭 `rag-e-security` 专用栈，保留本机使用的 `rag-e-engineering` Neo4j/Redis。
- 未进行第三方渗透测试；浏览器工具不可连接，页面通过 DOM 交互和真实 WebCrypto 测试，外部接口通过签名 HTTP 客户端验收，不把模拟交互说成真人浏览器验收。

## 图谱交互修复

- 补齐空白画布平移、以鼠标位置为中心的滚轮缩放、平移/缩放一键复位。
- 修复 SVG 捕获指针后 click 被重定向导致节点无法选中：按下/释放使用同一手势状态，4 px 阈值区分点击与拖动；取消、失去捕获及多指针不会残留拖动状态。
- 选中节点高亮并显示名称，原文证据紧邻图谱下方；缩放和窄屏留白情况下，节点拖动仍保持正确坐标和抓取偏移。
- 前端 14 项测试通过，新增 6 项指针交互回归；TypeScript/Vite 构建通过。浏览器连接工具本次返回 `Codex auth token is unavailable`，未完成真实浏览器操作验收。

## 通用图谱与可观测性阶段验收

| 阶段验收项 | 当时证据 |
|---|---|
| 后端回归 | 37 passed，1 项 Milvus 外部测试本轮未启用；包含真实 Neo4j/Redis 测试。报告 `test-results/engineering.xml`；原 Milvus 真机验收记录保留 |
| 前端交互 | 8 passed；新增力导向图、证据/路径、重建操作和监控状态展示；TypeScript + Vite 构建通过 |
| Neo4j 真机 | 官方 Neo4j 5.26.31：跨领域抽取、三元组事务、路径查询、L1/L2 Cypher 缓存、重建去重、删除失效通过 |
| Langfuse SDK | 锁定 4.16.0，实际 SDK 向本地 OTLP 接收端导出 protobuf，校验 Trace ID、父子 Span、usage 属性及不泄露正文；不是 mock SDK。用户的外部 Langfuse 项目尚未配置 |
| 性能与告警 | L1 优先/L2 回填和剩余 TTL、并发 miss 合并、LLM 批量并发上限与取消、检索单例与 BM25 快照、事件循环心跳、分位数/过期窗口、告警 pending/firing/recovery 全部有测试 |
| 实际 HTTP 流程 | 图谱已启用，导入跨领域样例得到 16 个实体/12 条关系；规则推理、图谱问答融合、请求到模型完整 Trace、缓存命中、Profiler 与 Prometheus 指标通过；`test-results/engineering-http.json` |
| 迁移和部署 | 数据库迁移至 `0003`，`alembic check` 无结构漂移；Neo4j/Redis Compose 实际启动，图谱/监控配置和 17 个 K8s YAML 对象可解析 |

本机开发后端 `http://localhost:8000` 已启用图谱和 Redis，前端 `http://localhost:5173` 增加“知识图谱”和管理员“监控与追踪”。
演示抽取识别明确三元组；自然语言实体关系抽取已实现并经过供应商协议测试，需要 `live` 模式及真实模型配置才会调用模型。
本次保留 `rag-e-engineering` 的 Neo4j/Redis 容器供本机功能使用，不自动启用外部 Langfuse 内容上传。

以下是前一版基础工程的历史验收记录。

## 基础工程阶段验收

| 验证项 | 结果 / 证据 |
|---|---|
| 独立安装 | 后端 `uv.lock` + `.venv`，前端 `package-lock.json` + `npm ci` |
| 后端静态检查 | Ruff 检查与格式检查通过 |
| 后端自动测试 | 普通测试 24 passed，另行执行真实 Milvus 测试 1 passed，共 25 项；`test-results/backend.xml`、`milvus.xml` |
| 前端测试 | 6 passed：SSE、登录、知识库、问答引用、切换会话、Markdown 安全渲染 |
| 前端生产构建 | TypeScript 检查 + Vite build 通过 |
| 历史输入完整性 | 当时验收用的 7 个 Markdown、170 个 JSON 逐文件 SHA-256 一致，PDF 字节一致；这些输入不随公开仓库分发 |
| 实际导入 | 7 份文档、111 个分块，本地 SQLite 和容器 PostgreSQL 均成功 |
| 本机 HTTP 流程 | 登录、两种问答模式、来源、历史、健康接口、前端代理均通过；`test-results/local-smoke.json` |
| 前后端镜像 | 两个 Dockerfile 实际构建通过；`test-results/backend-build.log`、`frontend-build.log` |
| 默认容器栈 | Nginx + FastAPI + PostgreSQL + Redis 启动成功；Nginx 配置测试通过 |
| 容器 HTTP 流程 | 同一套导入与问答验证通过；`test-results/container-smoke.json` |
| 真实 PostgreSQL/Redis | 迁移到 `0002`，事务写入、缓存命中、删除失效通过；`scripts/verify_storage.py` |
| 真实 Milvus 2.6.9 | etcd/MinIO/Milvus 健康；随机测试 collection 的建库、入库、原生 BM25 + dense + RRF 检索、删除与缓存失效通过；`test-results/milvus.xml` |
| MinIO 版本校验 | 官方资产 API 的 amd64 二进制长度 116670616 字节，SHA-256 匹配；缓存镜像构建通过，并成功作为 Milvus 对象存储 |
| 数据库迁移一致性 | `alembic check` 返回 No new upgrade operations detected |
| 配置审计 | Compose 可解析，K8s YAML 可解析，文档本地链接无缺失；源码无父目录运行时依赖或硬编码私钥 |

## 验证边界

- 演示模式通过代表工程链路可运行。真实 LLM/Embedding/Tavily 的 HTTP 协议、工具调用、评分解析与失败行为有测试，但该基础工程阶段未调用付费模型服务，模型业务质量需要配置后验收。
- 浏览器工具没有可连接浏览器，未进行视觉截图验收。页面交互已有 DOM 组件测试，生产构建及真实 Nginx 代理通过。
- Kubernetes 清单是可配置部署模板；未向集群部署，需替换镜像、域名、模型端点及 Secret。
- CI 文件已提供，未向远程 GitHub 仓库发布，因而不声称远程 CI 已运行。

本次专用 Docker 栈使用 `rag-e-validation` 项目名及 18000/18080/19539 测试端口，与默认启动入口分离；验收后关闭测试容器，保留命名卷以便复查。
