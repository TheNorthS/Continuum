# 多模态问答与个人空间

附件与知识库文档分开管理。附件不做 Embedding、不写 Milvus、不抽取知识图谱；上传只是保存原文件和解析结果，在聊天中选择并发送后才进入模型上下文。知识库导入仍沿用原来的分块、向量化和索引流程。

## 使用方式

1. 在“空间管理”上传文件，或点击聊天输入框的附件按钮。聊天上传成功后自动选中附件，同时保存到个人空间。
2. 通过“从空间选择附件”复用文件；从输入框移除附件只取消选择，不删除文件。
3. “使用知识库”开启时，结合所选知识库和附件作答；关闭时跳过知识检索。关闭且没有附件时使用普通模型对话，不声称经过资料验证。
4. 附件选择立即保存到会话。刷新页面、切换后返回或重新登录后打开原会话，都会恢复该会话的附件；每次追问重新读取内容，含图文件仍会重新调用视觉模型。已发送消息记录当轮附件名称及引用。
5. 空间支持文件名搜索、文本 / 图片预览、原文件下载、重命名和确认删除。重命名保留扩展名。

关闭知识库不自动启用联网。切换知识库开关或选库范围时，后续提问使用新的历史上下文边界；既有记录保留展示。没有知识库的新用户也可以普通聊天或使用附件。

新对话首次上传并选中附件，或首次从空间选择附件时，会自动创建会话并保存选择，即使尚未发送问题也能保留。已有会话勾选或移除后立即保存，保存失败会保留原选择并提示；保存中禁用发送和重复选择。移除只取消该会话对附件的选择，不删除空间文件。附件列表变化后刷新后续模型的上下文边界，避免继续带入已移除附件的旧回答；同一列表下追问保持连续历史。

当前浏览器标签页会按登录用户记住正在打开的会话 ID，刷新后恢复它；附件列表本身以服务端为准。新浏览器或新的浏览器会话可从“最近会话”打开原会话恢复附件。删除或失去访问权的已选附件显示为失效，系统不静默跳过，需点击“移除失效附件”后才能继续提问。历史记录不会反向决定当前选择，防止移除后又自动加回。

## 会话接口

- 创建会话可带 `attachment_ids`，按顺序保存选中的私人附件。
- `GET /api/v1/conversations/{id}` 返回 `attachment_ids`、可用附件摘要 `attachments` 和 `unavailable_attachment_ids`；摘要不含文件正文或图片数据。
- `PATCH /api/v1/conversations/{id}/attachments` 接受 `{"attachment_ids":["附件ID"]}`，空数组明确清空。检查本人归属、数量和上下文预算，生成中返回 409；无效选择不会覆盖原选择。
- 问答 `/messages` 和 `/stream` 中省略 `attachment_ids` 或传 `null` 会沿用已保存选择；传列表会保存替换，传 `[]` 才清空。每轮仍复核附件存在性和授权，接口语义一致。

## 格式与限制

| 格式 | 处理方式 |
|---|---|
| UTF-8 TXT / Markdown | 解析文本直接作为来源，不分块建索引 |
| PNG / JPEG / WebP | 校验、纠正方向、缩放后交给视觉模型；不支持动画 |
| PDF | 提取每页文本并渲染页面图片，支持扫描页和图表，需视觉模型 |
| DOCX | 提取主文档段落与表格文本，读取 PNG / JPEG / WebP 内嵌图片；含图时需视觉模型 |

DOCX 页眉页脚、批注、公式等复杂内容未完整提取；不支持的嵌入图片会明确报错并建议转 PDF。旧 `.doc`、音视频和表格文件不属于本版范围。下载保留原文件字节；在线预览只显示纯文本或图片。

默认单文件最多 `RAGE_MAX_UPLOAD_BYTES`（5 MiB），每用户最多 100 个文件、100 MiB 空间；空间按加密和预览数据估算占用，通常大于原文件大小。每轮最多 4 个附件，总正文最多 24,000 字符、总图片 / PDF 页面最多 12 张。单个 PDF 最多 12 页，图片最多 2000 万像素，送模型前长边最多 1600 像素。超限明确报错，不静默截断正文。

## 配置视觉模型

视觉服务须兼容 Chat Completions 的 `image_url` 输入和 JSON 输出。端点、模型、Key 独立设置，不继承 DeepSeek 的 Key：

```dotenv
RAGE_VISION_BASE_URL=https://your-vision-provider.example/v1
RAGE_VISION_MODEL=your-vision-model
RAGE_VISION_API_KEY=your-vision-key
RAGE_VISION_TIMEOUT=45
RAGE_VISION_MAX_CONCURRENCY=2
```

写入实际运行的 `.env` 或 `.env.production` 并重启后端。Kubernetes 将 Key 放 Secret，端点和模型名放后端环境配置。即使使用共享 RAG JSON，视觉配置仍独立放环境文件；现有文字语料评测不覆盖附件链路。

未配置视觉模型时可上传、管理附件，纯文本附件可问答；含图附件返回 `vision_not_configured`，空间页有提示。图片识别即使在 demo 模式也需真实视觉服务；demo 主 LLM 仍是摘录演示。

```dotenv
RAGE_ATTACHMENT_SPACE_BYTES=104857600
RAGE_ATTACHMENT_MAX_FILES=100
RAGE_ATTACHMENT_MAX_PER_MESSAGE=4
RAGE_ATTACHMENT_MAX_CHARS=24000
RAGE_ATTACHMENT_MAX_IMAGES=12
```

提问时将所选图片和当前问题发送给视觉服务，按序号整理为文字证据，再由主 LLM 作答。视觉请求同时返回内容风险分类，格式错误、超时、输出不完整或风险越阈值均终止本轮；识别文本继续经过内容审查。附件回答保留相关性、Grounding、反馈修正及最终检查。视觉误读仍可能影响答案，需核对原图；本次只做模拟视觉协议验证，未配置真实视觉模型验证效果。

## 存储与权限

- `attachments` SQL 表记录用户归属、类型、大小和时间，文件名及包含原文件、文本和预览图片的 payload 使用现有 AES-256-GCM 字段加密，纳入密钥轮换。
- 随业务 SQL 数据库持久化，无公开文件目录或匿名下载地址。备份仍需分别保管数据库和密钥。此版适合有配额的小附件，未接对象存储。
- 接口要求 `chat.use`、有效登录和请求签名，仅允许本人读写；管理员也不能通过这些接口查看其他用户附件。
- 配额检查在数据库事务中串行化，避免同用户并发上传超过容量或数量上限。
- 删除移除空间文件及解析 payload，历史消息仍保留加密记录，但 API 隐藏涉及该附件的消息和引用；不会从旧备份中擦除数据。生成结束前复核归属及存在性，删除冲突时本轮不保存。
- 图片、识别正文和 Key 不进入追踪正文，仅记录模型、图片数、耗时和状态。解析使用工作线程，PDFium 调用通过互斥锁限制并发。

## 迁移与测试

附件空间迁移为 `0007`，会话附件选择持久化增加迁移 `0008`。旧会话 `use_knowledge` 默认 `true`，`attachment_ids` 默认空数组；升级不擅自根据历史回答选择附件，旧会话首次需重新选一次，之后自动恢复。生产备份并停止写入后，在配置好的后端执行：

```bash
uv sync --frozen --extra dev --extra milvus --extra evaluation
uv run --inexact alembic upgrade head
uv run --inexact alembic current
```

不需要重新生成知识库向量。实际业务数据库迁移由部署者执行，本次测试使用隔离数据库。

```bash
uv run --inexact pytest -q tests/test_conversation_attachments.py tests/test_attachments.py tests/test_knowledge_scope.py tests/test_security_complete.py
```

测试涵盖真实解析器、私有性、加密轮换、配额、无 Embedding 问答、知识库混合来源、删除冲突及视觉协议。解析接口依据：[PDFium Python API](https://pypdfium2.readthedocs.io/en/stable/python_api.html)、[Pillow Image](https://pillow.readthedocs.io/en/stable/reference/Image.html)。
