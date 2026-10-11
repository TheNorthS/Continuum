# 有害内容审查

本模块是独立于答案事实依据评分的安全门控，适用于问答 API、SSE 和 CLI 问答。
默认开启，没有关闭开关；管理员账号同样受审查。

## 审查流程

1. 验证用户对会话的访问权并取得会话锁。
2. 审查当前问题，存在上下文时再结合实际进入模型的最近历史检查；拦截后不执行问题路由、检索或答案生成。
3. 知识库/图谱融合检索与 Web 搜索返回的片段逐条检查，再检查保留片段组合。被拦截片段不会进入回答生成和引用面板。
4. 检查每个候选回答，覆盖 `adaptive`、`agentic` 的检索路径和 `direct` 普通回复分支。幻觉评分与有害内容评分独立，事实有据不代表内容安全。
5. 在保存与返回前再次检查最终答案及所有引用的正文、标题、文件名等字段。只有通过才保存本轮消息。

SSE 只发送不含正文的执行步骤，以及通过审查的最终结果；候选答案不会先流出再撤回。生成与直接回复的未审核输入/输出也不写入 Langfuse 正文。

## 分类与阈值

| 类别 | 含义 | 默认拦截阈值 |
|---|---|---|
| violence | 暴力实施、威胁、恐怖或武器制作等帮助 | 0.8 |
| hate | 针对受保护群体的仇恨、迫害或去人化 | 0.8 |
| harassment | 严重针对性骚扰、威胁、人肉、羞辱 | 0.8 |
| sexual_minors | 未成年人性化或剥削内容 | 0.5 |
| sexual_explicit | 以性刺激为目的的露骨色情内容 | 0.8 |
| self_harm | 自伤方法、鼓励或当前个人自伤危机 | 0.5 |
| illicit_instructions | 犯罪/诈骗等具体实施与逃避侦查帮助 | 0.8 |
| cyber_abuse | 凭据窃取、恶意软件、未授权入侵帮助 | 0.8 |

任一类别分数 **>=** 阈值即拦截。配置 `RAGE_MODERATION_THRESHOLDS` 必须包含全部类别，且每项在 `(0,1]` 内。
分类提示词明确区别安全教育、医学、历史/新闻分析、防御安全和实际促成伤害；本地规则也包含常见预防/拒绝表达的上下文处理，而不是“出现敏感词就拦截”。
分数是分类器的策略分数，未经业务标注集校准，不是准确概率。部署后需要用自己的合法/违规样本评估误判和漏判。

## 后端选择与配置

- `RAGE_MODERATION_BACKEND=auto`：`demo` 使用离线规则；`live` 在规则之外调用独立的语义分类请求。规则明确命中时直接阻断。
- `local`：仅离线规则，覆盖有限的中英文显式风险、Unicode NFKC/零宽字符规避。不是完整语义识别器；隐喻、跨语言变体、复杂上下文可能漏判。生产配置不允许只使用该后端。
- `llm`：独立分类请求，不使用回答生成的自由文本当判定。默认复用现有 LLM 端点、模型和密钥，也可设置独立分类模型/端点/密钥。

以下为代码默认配置示例，适用于未启用共享 RAG JSON 的环境：

```dotenv
RAGE_MODERATION_BACKEND=auto
RAGE_MODERATION_MODEL=
RAGE_MODERATION_BASE_URL=
RAGE_MODERATION_API_KEY=
RAGE_MODERATION_TIMEOUT=12
RAGE_MODERATION_CHECK_TIMEOUT=35
RAGE_MODERATION_MAX_CONCURRENCY=4
RAGE_MODERATION_CHUNK_CHARS=12000
RAGE_MODERATION_MAX_CHARS=200000
```

当前[共享模板](../config/rag.production.example.json)使用 `moderation_timeout=30` 秒、`moderation_check_timeout=60` 秒、并发 4，思考模式为 `disabled`；整轮问答预算为 150 秒。启用 profile 后修改 JSON 中的对应策略字段，不在环境文件写入冲突值；API Key 单独保存在环境文件。

独立 endpoint 不会自动继承主 LLM 的密钥，必须填写 `MODERATION_API_KEY`，避免把已有密钥发送到不同服务。
服务使用兼容 `/chat/completions` 的 JSON object 输出，严格要求八个类别全部存在、数值有限且在 `[0,1]`，禁止字符串/布尔代替数值、额外字段，以及 `finish_reason` 非 `stop` 的不完整结果。
没有静默截断：长文本按 12000 字符分段并有 300 字符重叠，全部片段取最大类别分数。超过总上限返回错误，不放行未检查的剩余内容。分段可能丢失远距离语义联系，这是必须用业务评测关注的限制。

独立分类调用有独立的 HTTP 连接池、并发上限和超时；仍受整轮问答 150 秒总预算限制。新增分类调用会增加延迟和模型用量，需要据实际负载调整。

## 拦截与异常

- 拦截返回 `status=blocked`、固定安全替代回复、空 `sources` 和 `safety` 元数据。自伤危机请求使用支持性回复，不提供方法。
- 被拦截本轮的问题和候选答案不写入聊天历史，客户端会显示“本轮未保存”。原先已保存的历史保留，后续进入模型前再次审查。
- 审查超时、网络错误、无效 JSON、缺少类别等返回 `moderation_unavailable`（普通接口 503，SSE `error` 事件）。本轮不落库、不返回未经审核答案，释放会话锁。
- 取消请求传播取消，不转成“安全通过”。

审计事件记录 `moderation.allowed/blocked/error/limit`，仅包含阶段、类别分数、阈值、后端和策略版本，不含用户正文、危险候选、模型理由或服务端原始响应。
Prometheus 提供 `rag_e_moderation_decisions_total`、`rag_e_moderation_blocks_total`，已有 Profiler/告警加入 moderation 操作。系统页显示当前规则/语义后端，管理员监控页显示策略阈值。

## 边界与验证

此改动保护**问答输入、上下文、进入生成的资料和问答输出**。它不删除原始上传文档，不替代知识库访问授权，也不对管理员直接检索/查看源文件接口实施内容封禁。
本地规则不是成熟的全语言内容模型；语义分类器也可能被对抗提示绕过，不能保证百分百检测。源码中的策略和规则是可审阅的基线，不宣称第三方认证或零漏报。
协议与异常回归使用 MockTransport，不消耗真实模型额度。公开数据问答评测包含实际语义审查调用，但其结果不等同于独立内容分类准确率；部署前仍需用目标业务的标注样本评估误判和漏判。

专项测试：在 `backend` 执行 `uv run --inexact pytest -q tests/test_moderation.py`。
设计依据：[OWASP RAG 安全指南](https://cheatsheetseries.owasp.org/cheatsheets/RAG_Security_Cheat_Sheet.html) 与 [OWASP Prompt 注入防护指南](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html)。
