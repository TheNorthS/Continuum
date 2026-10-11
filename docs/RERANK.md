# 模型精排

精排位于公共知识检索链路中，聊天问答和检索试验台使用同一套处理：

```text
知识库范围与 ACL
  → BM25 / 向量 / 图谱召回
  → RRF 融合与去重
  → 权限过滤 + 候选池
  → Cross-Encoder 或精排 API 逐对评分
  → 按精排分数排序 / 可选阈值过滤
  → 再次校验权限
  → 最终 Top-K
  → 聊天来源安全检查、LLM 相关性过滤与回答生成
```

这一步会重新排列候选文档，不能用 LLM 的 `relevant=true/false` 过滤或 RRF 排名替代。`demo` / `live` 与精排开关独立：可以使用演示回答器配合真实本地精排，结果仍保留演示回答标记。

下文 `.env` 示例适用于未启用共享 profile 的开发环境。生产与评测使用 [RAG JSON](SHARED_PIPELINE.md) 时，模型、端点、候选数和超时在 JSON 修改，只有精排 Key 放在各自环境文件。当前共享模板使用硅基流动 `Qwen/Qwen3-Reranker-0.6B`、`standard` 格式、32 候选和 60 秒精排预算。

## 本地模型

在后端目录安装已锁定的模型扩展：

```bash
uv sync --frozen --extra dev --extra milvus --extra local-models
```

先下载或准备模型，再启用离线加载。以下命令在 `backend` 目录执行，将官方模型保存到本项目缓存；只选择 safetensors 和分词配置，不下载重复的 PyTorch / ONNX 权重：

```bash
uv run --extra local-models python -c "from huggingface_hub import snapshot_download; snapshot_download('BAAI/bge-reranker-base', local_dir='../.cache/models/bge-reranker-base', allow_patterns=['config.json','model.safetensors','tokenizer.json','tokenizer_config.json','special_tokens_map.json','sentencepiece.bpe.model'])"
```

`.env` 配置示例：

```dotenv
RAGE_RERANK_PROVIDER=huggingface
RAGE_RERANK_MODEL=../.cache/models/bge-reranker-base
RAGE_RERANK_DEVICE=cpu
RAGE_RERANK_LOCAL_FILES_ONLY=true
RAGE_RERANK_CANDIDATES=32
RAGE_RERANK_BATCH_SIZE=8
RAGE_RERANK_MAX_LENGTH=512
RAGE_RERANK_TIMEOUT=60
RAGE_RERANK_FAILURE_MODE=error
```

这里的相对模型路径以启动目录 `backend` 为基准，也可以配置绝对路径或已缓存的 Hugging Face 模型 ID。使用兼容的单分数 CrossEncoder 模型；已配置 CUDA 环境时可将 device 改为 `cuda`。本地加载禁用远程自定义代码，并要求 safetensors 权重。更换模型版本时同时更新 `RAGE_RERANK_REVISION`，重启后端，让缓存区分版本。

已有启动脚本使用 `uv run --inexact` 保留已安装的可选依赖。若另行运行 `uv sync`，请继续加上所需的 `--extra local-models`，避免移除精排依赖。

从后端目录启动：

```bash
uv run --extra local-models uvicorn app.main:app --host 127.0.0.1 --port 8000
```

默认 `local_files_only=true`，请求期间不自动从网络下载权重。模型按需首次加载、每进程复用，本地推理在工作线程执行。同一进程的本地模型调用串行化；请求超时或取消后，已进入原生推理的工作不能强制终止，其并发许可保持到真实计算结束，避免重复加载或显存并发失控。

本地原始 logits 经过 Sigmoid 变成 `[0,1]` 分数。这只是当前模型的相关性分数，未经校准，不能解释为正确答案概率。

## 精排 API

使用完整的精排 endpoint，不自动拼接路径，不复用主 LLM / Embedding 密钥：

```dotenv
RAGE_RERANK_PROVIDER=api
RAGE_RERANK_MODEL=Qwen/Qwen3-Reranker-0.6B
RAGE_RERANK_API_URL=https://api.siliconflow.cn/v1/rerank
RAGE_RERANK_API_FORMAT=standard
RAGE_RERANK_API_KEY=your-rerank-key
RAGE_RERANK_CANDIDATES=32
RAGE_RERANK_MAX_CONCURRENCY=2
RAGE_RERANK_TIMEOUT=60
RAGE_RERANK_FAILURE_MODE=error
```

适配 `model/query/documents/top_n/return_documents` 请求，以及 `results[{index,relevance_score}]` 响应格式。兼容接口不代表所有模型供应商的路径或模型名相同，按所用服务填写。外部 API 会收到本轮查询和授权候选片段；本地模型不会发送这些资料。

阿里云百炼的 `qwen3-rerank` 使用 `RAGE_RERANK_API_FORMAT=qwen3`，请求省略不适用的 `return_documents`，仍解析顶层 `results`。完整 URL 按业务空间和地域配置，例如 `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-api/v1/reranks`。该格式不适用于需要嵌套 `input/parameters` 和 `output.results` 的其他百炼模型。详见 [官方协议](https://help.aliyun.com/zh/model-studio/text-rerank-api)。

示例响应：

```json
{
  "results": [
    {"index": 1, "relevance_score": 0.92},
    {"index": 0, "relevance_score": 0.31}
  ]
}
```

服务请求全部候选的分数，要求返回完整、唯一、范围有效的索引及 `[0,1]` 有限数值；缺项、重复、布尔/字符串分数和无效 JSON 都作为失败。根据索引映射到原始片段，不采用供应商返回的文档正文或元数据。分数并列时保持原 RRF 顺序。

API 并发有独立信号量，复用应用 HTTP 连接池。总超时包含等待缓存锁、排队、网络和重试。外部凭据只发送到配置的精排端点；匿名接口仅允许配置为本机回环地址。

## 配置与失败策略

下表列出未加载 profile 时的代码默认值；共享模板覆盖 provider、模型、端点及超时等字段。请以实际加载后的配置为准。

| 配置 | 默认 | 含义 |
|---|---|---|
| `RAGE_RERANK_PROVIDER` | `none` | `none` / `huggingface` / `api`；全新安装默认不引入模型下载依赖 |
| `RAGE_RERANK_MODEL` | `BAAI/bge-reranker-base` | 本地模型 ID/路径或供应商模型名 |
| `RAGE_RERANK_REVISION` | 空 | 本地 Hub 模型 revision，同时参与精排缓存版本 |
| `RAGE_RERANK_DEVICE` | `cpu` | 本地设备 |
| `RAGE_RERANK_LOCAL_FILES_ONLY` | `true` | 本地加载不联网 |
| `RAGE_RERANK_API_URL` | 空 | 完整接口 URL |
| `RAGE_RERANK_API_FORMAT` | `standard` | `standard` 兼容通用 rerank；`qwen3` 对应百炼 qwen3-rerank 平铺协议 |
| `RAGE_RERANK_API_KEY` | 空 | 独立 API 凭据 |
| `RAGE_RERANK_CANDIDATES` | `32` | RRF 后最多保留的精排候选数，实际不小于本次 Top-K，最大配置 100 |
| `RAGE_RERANK_BATCH_SIZE` | `8` | 本地模型内部批次大小 |
| `RAGE_RERANK_MAX_CONCURRENCY` | `2` | API 同时处理的计算请求数，本地模型始终单个工作执行 |
| `RAGE_RERANK_MAX_LENGTH` | `512` | 本地模型问题/文档对的 Token 上限 |
| `RAGE_RERANK_QUERY_MAX_CHARS` | `1000` | 送入精排的问题字符上限 |
| `RAGE_RERANK_DOCUMENT_MAX_CHARS` | `6000` | 每个候选用于评分的字符上限 |
| `RAGE_RERANK_TIMEOUT` | `30` | 单次精排总预算秒数 |
| `RAGE_RERANK_MIN_SCORE` | 不设置 | 可选 `[0,1]` 阈值；低于阈值的片段不进入后续 Top-K |
| `RAGE_RERANK_FAILURE_MODE` | `error` | `error` 中止并返回明确错误；`rrf` 保留融合顺序并显示回退标记 |

字符与 Token 上限只截取用于评分的文本，返回引用保留原始片段。较长资料尾部信息可能不参与评分；可调整分块/长度/候选数并按自己的语料评测。API 服务还可能施加自己的 Token 限制。

`error` 下问答不保存失败轮次；普通请求返回 503，SSE 返回 `error` 事件。`rrf` 是显式可选降级，不补造精排分数，也不把失败结果缓存成成功。取消不会触发回退。若设置阈值后全部候选被剔除，按资料不足继续执行原工作流。

## 缓存、权限与观测

- 精排前后均检查就绪片段及当前权限，单库/多库范围在检索层保持不变。
- 复用加密 L1/L2，缓存仅保存分数数组；键包含模型、revision、端点、有效查询与候选 ID/评分文本摘要。改变候选集或模型版本不会命中旧结果。
- `score` 仍为原有召回/RRF 分数，新增 `rerank_score` 为模型分数。成功来源包含 `metadata.rerank_status=ranked`；回退为 `fallback`，无伪造模型分数。
- 系统概览显示 provider、模型、配置/加载/错误状态、候选池、阈值和失败策略。配置状态不是供应商健康验证。
- Prometheus / Profiler 的 `rerank` 操作记录耗时和错误，缓存指标增加 `rerank` namespace，告警评估包括精排错误率和 p95。
- Langfuse Trace 增加精排 span，仅带模型、provider、候选数量，不发送查询或文档正文。

本地加载状态属于当前 worker。多 worker 分别加载模型，需要按内存/显存容量配置。默认容器镜像包含 API 所需依赖；本地模型部署需额外安装 `local-models` 并挂载模型目录。

## 验证

```bash
# backend：独立精排、检索链路、安全与异常测试
uv run --extra dev pytest -q tests/test_rerank.py
```

测试包括低召回排名片段经模型晋升 Top-K、图谱/向量融合、权限前后过滤、单/多库、缓存、API 协议、阈值、稳定排序、失败提示、并发、原生推理取消和状态指标。设置 `RAGE_TEST_RERANK_MODEL` 为完整本地模型路径，可启用真实中文 CrossEncoder 推理测试；未配置则该项跳过。

接口依据：[Sentence Transformers CrossEncoder](https://www.sbert.net/docs/package_reference/cross_encoder/model.html)、[BAAI 模型仓库](https://huggingface.co/BAAI/bge-reranker-base)、[SiliconFlow 精排协议](https://docs.siliconflow.cn/docs/api/rerank-post)。
