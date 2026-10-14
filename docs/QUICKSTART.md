# 从演示模式切换到真实 RAG

首次安装与演示启动见[项目首页](../README.md#快速开始)。本页说明模型、检索依赖与资料导入；仅配置文件中的模型名称不能证明供应商可用，需要完成连通性与业务验证。

## 选择配置入口

| 场景 | 入口 |
|---|---|
| 本地开发 | 根目录 `.env`，启动脚本默认读取它 |
| 生产部署 | `.env.production` + `config/rag.production.json`，通过生产 Compose 组合加载 |
| 隔离评测 | `.env.eval` + 同一 RAG JSON，由评测运行器加载 |

本地开发可以直接在 `.env` 配置下列字段。若已设置 `RAGE_PIPELINE_PROFILE`，模型、分块与检索字段应在该 JSON 中修改，环境文件只保存凭据和部署连接；冲突值会被拒绝。模板复制与字段边界见[共享参数](SHARED_PIPELINE.md)。

## 在线模型组合

以下示例在未启用共享 JSON 的开发 `.env` 中使用：

```dotenv
RAGE_MODE=live

# 回答、相关性、事实依据与默认语义审查
RAGE_LLM_BASE_URL=https://api.deepseek.com
RAGE_LLM_MODEL=deepseek-flash
RAGE_LLM_THINKING=disabled
RAGE_LLM_API_KEY=your-deepseek-key

# 文档及问题向量
RAGE_EMBEDDING_PROVIDER=api
RAGE_EMBEDDING_BASE_URL=https://api.openai.com/v1
RAGE_EMBEDDING_MODEL=text-embedding-3-small
RAGE_EMBEDDING_DIMENSION=1536
RAGE_EMBEDDING_API_KEY=your-embedding-key

# 在线模型精排
RAGE_RERANK_PROVIDER=api
RAGE_RERANK_API_FORMAT=standard
RAGE_RERANK_API_URL=https://api.siliconflow.cn/v1/rerank
RAGE_RERANK_MODEL=Qwen/Qwen3-Reranker-0.6B
RAGE_RERANK_API_KEY=your-siliconflow-key
RAGE_RERANK_CANDIDATES=32
RAGE_RERANK_TIMEOUT=60

# 新的检索空间
RAGE_VECTOR_BACKEND=milvus
RAGE_MILVUS_URI=http://localhost:19530
RAGE_MILVUS_COLLECTION=rag_e_live_v1
RAGE_GRAPH_ENABLED=false
RAGE_WEB_SEARCH_ENABLED=false
```

三个 Key 分别对应实际服务。LLM / Embedding 填 API 根地址，程序拼接 `/chat/completions` 或 `/embeddings`；精排填写完整路径。模型需在账号可用列表中；使用兼容网关时替换为匹配的端点和凭据。

在 `backend` 安装依赖：

```bash
uv sync --frozen --extra dev --extra milvus
```

在线方案不需要安装本地模型。Milvus 部署与端口见[部署指南](DEPLOYMENT.md#启用-milvus)：容器内地址、默认端口和验证栈的宿主机 `19539` 端口不能混用。开发演示库已含 hash 向量时，应配置新的 SQL 数据库及 collection，再重新导入资料；迁移步骤见[索引重建](MIGRATION.md#更换-embedding-或分块规则)。

## 可选本地 Embedding

保留有效的真实 LLM 配置，将 Embedding 改为：

```dotenv
RAGE_EMBEDDING_PROVIDER=huggingface
RAGE_EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5
RAGE_EMBEDDING_DIMENSION=512
RAGE_EMBEDDING_DEVICE=cpu
```

在 `backend` 安装 `uv sync --frozen --extra dev --extra milvus --extra local-models`。首次使用需要下载权重，也可指定已准备好的模型目录；使用 GPU 需有匹配的 CUDA 环境。后续 `uv run --inexact` 保留已经安装的可选依赖。

维度必须与模型实际输出一致。当前适配器校验返回数量、维度和有限值；只修改 `embedding_dimension` 不会请求 API 降维。模型、端点、维度或分块变化后使用新索引。

本地 CrossEncoder 或其他在线精排模型的配置见[精排指南](RERANK.md)。

## 初始化与首次问答

确认目标数据库、模型和服务连接后，在 `backend` 执行：

```bash
uv run --inexact python -m app.cli init
uv run --inexact python -m app.cli ingest ../data/documents/examples
uv run --inexact python -m app.cli verify
```

`live` 模式导入会调用真实 Embedding，语义分组也可能产生额外请求；启用图谱后还会调用抽取模型。CLI 将资料导入默认库；从网页导入时先选择目标知识库。

随后使用首页脚本启动前后端，登录、选择知识库并提问。管理员可在“账号与权限”创建用户和角色。CLI 创建用户可运行 `uv run --inexact python -m app.cli create-user alice --role user`，密码以交互方式输入。配置中的初始管理员密码不会覆盖已有账号。

当前原生格式为 UTF-8 `.md`、`.markdown`、`.txt`。PDF、Office、图片和扫描件需先转换；原生解析与 OCR 尚未提供。数据与文件范围见[数据目录](../data/README.md)。

## 可选联网补充

```dotenv
RAGE_WEB_SEARCH_ENABLED=true
RAGE_WEB_SEARCH_PROVIDER=hybrid
RAGE_TAVILY_API_KEY=your-tavily-key
RAGE_EXA_API_KEY=your-exa-key
```

未使用共享 profile 时将以上配置写入 `.env`；使用 profile 时，在共享 JSON 中设置 `web_search_enabled=true`、`web_search_provider="hybrid"`，环境文件仅放密钥。可单独选择 `tavily` 或 `exa`，只需对应的 Key。

联网仅用于没有固定知识库范围的兼容 API / CLI 路径；网页选库问答始终使用所选资料。默认任一路失败会报错；允许部分结果、超时和来源规则见 [Tavily / Exa 联网检索](WEB_SEARCH.md)。

## 排查入口

| 现象 | 检查项 |
|---|---|
| 401 / 403 | 登录会话、请求签名、角色权限、知识库授权或首次改密要求 |
| 409 | 会话并发生成、一次性证明重用、用户名或资源冲突 |
| 413 / 422 | 文件大小、UTF-8 编码、空正文、参数与知识库范围 |
| 502 / 503 | 模型端点、JSON / 工具调用协议、额度、精排服务、审查或存储依赖 |
| 索引签名冲突 | 是否更换模型、端点、维度、后端或共享分块策略；使用新空间重建 |
| 图谱失败 | 启用状态、Neo4j 凭据、文档分块数与抽取预算；图谱失败不等于向量入库失败 |

整个问答默认 150 秒预算，单服务超时受有效配置控制。生产不能沿用演示凭据，需完成 HTTPS、独立密钥和数据库迁移，详见[安全](SECURITY.md)与[部署](DEPLOYMENT.md)。
