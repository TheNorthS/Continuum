# 知恒 · Continuum 架构与算法图示

| 图 | SVG | PNG | 可编辑源文件 |
|---|---|---|---|
| 系统架构 | [architecture.svg](architecture.svg) | [architecture.png](architecture.png) | [architecture.dot](architecture.dot) |
| 问答与上下文流程 | [qa-flow.svg](qa-flow.svg) | [qa-flow.png](qa-flow.png) | [qa-flow.dot](qa-flow.dot) |
| AHC 长会话压缩 | [context-compaction.svg](context-compaction.svg) | [context-compaction.png](context-compaction.png) | [context-compaction.dot](context-compaction.dot) |
| EMC 个人记忆与生命周期 | [memory-lifecycle.svg](memory-lifecycle.svg) | [memory-lifecycle.png](memory-lifecycle.png) | [memory-lifecycle.dot](memory-lifecycle.dot) |

图示依据当前代码绘制，使用 Graphviz 生成。默认中文字体为 Microsoft YaHei；在其他平台可指定已安装的中文字体。修改 DOT 源文件后，在项目根目录运行：

```bash
python scripts/render_diagrams.py
# Linux 示例（需要先安装对应字体）
python scripts/render_diagrams.py --font "Noto Sans CJK SC" --dpi 160
```

脚本不安装或下载工具；需要 `dot` 在 PATH 中。SVG 用于 README，PNG 用于下载和外部展示。`--dpi` 只控制 PNG 清晰度；SVG 固定使用 72 DPI 的坐标尺度，保持图形与 `viewBox` 一致，避免预览裁切。源文件与两种渲染结果应一起提交，避免只改图片、源图失同步。

## 图示边界

- **系统架构**：展示工作台、授权、问答、知识检索、附件、上下文和后台记忆。图中省略重复数据库访问及模型调用；虚线代表补充依赖或横向说明，不表示这些调用一定异步。SQL 是业务与记忆权威源，知识库向量仍使用 Milvus，记忆检索在有用户配额的授权 SQL 集合上执行。
- **问答流程**：展示知识库／附件资料问答与普通对话。普通对话不做资料 Grounding，所有分支仍受内容审查与请求预算限制。图中的②、③等编号也用于指明重试返回位置。Web / direct 为未限定范围的兼容路径，范围约束写在图底部。
- **AHC 压缩**：真实代码先构造引用化历史投影，再检查完整请求压力，安全切点与摘要检查通过后才提交。无收益或无安全切点不会写入无效检查点；自动压缩还要求达到目标。图中公式表示配置规则，不是性能评测结果；默认保守预算不是精确 Token 数。
- **EMC 记忆**：自动提炼基于用户明确陈述，证据验证后按两阶段持久任务整合。争议记忆暂停注入，用户明确更正创建新版本；遗忘、到期及来源失效影响后续检索。团队共享、任意工具执行重放和百万级记忆索引不在当前实现范围内。

颜色统一表达模块性质：蓝色为入口及上下文，绿色为可用证据／有效结果，紫色为模型处理，橙色为补救／争议／后台工作，灰色为持久化和工程约束。图中信息可在 Markdown 正文中找到等价说明。

依据：[Orchestrator](../../../backend/app/agents/orchestrator.py)、[ChatService](../../../backend/app/services/chat_service.py)、[ContextService](../../../backend/app/memory/context.py)、[TokenMeter](../../../backend/app/memory/tokens.py)、[MemoryService](../../../backend/app/memory/service.py)、[MemoryRetriever](../../../backend/app/memory/retrieval.py)。
