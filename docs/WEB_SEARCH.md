# Tavily / Exa 联网检索

联网服务接入现有 LangGraph 的 `web` 节点，支持单独使用 Tavily、单独使用 Exa，以及两者并行检索。默认关闭，无需安装供应商 SDK。

## 启用方式

未设置 `RAGE_PIPELINE_PROFILE` 时，在根目录 `.env` 中配置并重启后端：

```dotenv
RAGE_WEB_SEARCH_ENABLED=true
RAGE_WEB_SEARCH_PROVIDER=hybrid
RAGE_TAVILY_API_KEY=your-tavily-key
RAGE_EXA_API_KEY=your-exa-key
```

| Provider | 请求方式 | 必需凭据 |
|---|---|---|
| `tavily` | Tavily Search，`basic` 或 `advanced` | `RAGE_TAVILY_API_KEY` |
| `exa` | Exa Search，`type=auto`，请求正文 | `RAGE_EXA_API_KEY` |
| `hybrid` | 两个供应商并发查询，同 URL 合并 | 两个 Key |

启用后会校验所选供应商的密钥是否非空，未使用的供应商可以不填。密钥有效性及额度在实际调用时验证。演示模式也可以调用真实搜索 API；完整模型问答仍需按[真实模型指南](QUICKSTART.md)配置 `live`。

使用生产 / 评测共享 profile 时，在 `config/rag.production.json` 的 `settings` 内修改：

```json
"web_search_enabled": true,
"web_search_provider": "hybrid",
"web_search_failure_mode": "error"
```

以上是需要修改的字段片段，保留 JSON 内其余字段。对应运行环境文件只放两个 Key，不再用环境变量覆盖策略。生产模板和封闭语料评测默认保持联网关闭；开启会改变评测条件及参数指纹，后续测试使用新 run。

## 何时联网

- 未限定知识库的兼容 API / CLI 问答：模型可路由到 `web`；库内资料不足且有限次改写后也可进入 `web`。每轮最多进入一次网络检索节点。
- 显式选择一个或多个知识库：只检索所选资料；工作流和搜索适配器都阻止联网。网页目前采用选库问答，没有绕过库范围的联网开关。
- 搜索查询来自当前问题或结合历史改写的问题，发送到选定的第三方搜索服务；不会把知识库原始文档批量发送给搜索接口。

流程为：检索 → 统一来源并去重 / 融合 → 来源内容审查 → 生成 → 候选答案审查 → 事实依据与有效性检查 → 最终审查及引用保存。Web 来源不经过知识库的模型精排节点。搜索服务自身生成的答案不作为系统答案。

## 参数

环境变量统一加 `RAGE_` 前缀并大写；共享 JSON 使用表中小写字段。

| 字段 | 默认值 | 含义 |
|---|---|---|
| `web_search_enabled` | `false` | 联网总开关 |
| `web_search_provider` | `tavily` | `tavily` / `exa` / `hybrid` |
| `web_search_max_results` | `3` | 每路请求上限和最终来源上限，范围 1–10 |
| `web_search_timeout` | `20` | 每路总预算（秒），含排队及重试，范围 (0, 60] |
| `web_search_max_concurrency` | `4` | 每个后端 worker 同时发起的供应商请求上限，范围 1–16 |
| `web_search_content_max_chars` | `5000` | 每条返回正文截取上限，范围 500–12000 字符 |
| `web_search_failure_mode` | `error` | `error` 或 `partial` |
| `tavily_search_depth` | `basic` | Tavily `basic` / `advanced` |

复用后端异步 HTTP 连接池；429、5xx 和暂时性网络故障最多尝试 3 次，受总超时约束。整轮仍受 `request_timeout` 限制。取消问答时会取消尚未完成的供应商任务。双路模式会产生两家服务的调用费用；`advanced` 按 Tavily 对应计费规则使用。

## 来源与失败行为

返回来源统一包含 `id`、`text`、`score` 和 `metadata`。Tavily 使用 `content`，Exa 使用 `text`，缺少正文时尝试 `highlights`。跳过无正文或无合法 HTTP(S) URL 的条目；不额外抓取搜索结果链接。

URL 去除片段、统一主机大小写和默认端口后去重，由 URL 的 SHA-256 生成稳定的 `web-…` ID；查询参数保留。双路相同 URL 优先保留 Tavily 正文，记录两路命中。两路均有有效结果时以 `1 / (60 + rank)` 做 RRF；只有一路有结果时保留其顺序和分数。上游未返回分数时使用排名分数，并标注 `score_type=rank`。这些分数不代表答案置信度。

- `error`：任何选定供应商失败都返回 `web_search_unavailable`（503），不把故障当成零命中。
- `partial`：仅在另一路有可用来源时继续；来源标记 `web_status=partial` 和失败供应商，前端提示来源不完整。单路失败、双路全失败或成功一路没有有效来源，仍返回错误。
- 两路正常但都没有有效来源：按资料不足拒答。
- 内容审查独立执行；允许部分搜索结果不会跳过安全审查，来源被全部过滤后也按资料不足处理。

`metadata.web_providers`、`web_ranks` 保留渠道与原始排名 / 分数；前端引用可展开摘要、查看渠道并打开原网址。原始供应商错误正文、请求密钥不会进入公开错误信息。

## 检查与测试

登录后查看系统页或受保护的 `/api/v1/system`：`web_search` 显示开关、供应商、密钥是否已配置、来源上限及故障策略。这里是配置状态，不代表已通过连通性测试，也不会因刷新状态页产生付费搜索。

在 `backend` 运行离线协议与工作流测试：

```bash
uv run --inexact pytest -q tests/test_web_search.py tests/test_pipeline_profile.py tests/test_workflow.py tests/test_knowledge_scope.py
```

测试通过 MockTransport 验证请求协议、并发、去重、取消、超时、故障策略、知识库边界、来源审查和问答引用。真实 API 额度、可用性和搜索质量需在配置后单独验证：可在配置好的开发实例通过 `uv run --inexact python -m app.cli chat` 提出实时信息问题，确认执行过程进入“查询网络资料”且引用包含对应供应商。CLI 会初始化应用服务，应使用已完成迁移的开发 / 测试数据库。

监控使用 `web_search`、`web_search.tavily`、`web_search.exa` spans，继承现有 Profiler、Prometheus 和可选 Langfuse；供应商出错会记录子 span 错误。部分成功时外层搜索成功，失败子 span 仍可定位。搜索 span 不上传查询正文或 API Key。

接口依据：[Tavily Search 官方文档](https://docs.tavily.com/documentation/api-reference/endpoint/search)、[Exa Search 官方文档](https://exa.ai/docs/reference/search)。
