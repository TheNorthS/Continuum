# README 图示

| 图 | SVG | PNG | 可编辑源文件 |
|---|---|---|---|
| 系统架构 | [architecture.svg](architecture.svg) | [architecture.png](architecture.png) | [architecture.dot](architecture.dot) |
| 选库问答流程 | [qa-flow.svg](qa-flow.svg) | [qa-flow.png](qa-flow.png) | [qa-flow.dot](qa-flow.dot) |

使用 Graphviz 生成，字体为 Microsoft YaHei；其他平台可改为已安装的中文字体后重新生成。在项目根目录运行：

```bash
dot -Tsvg docs/assets/diagrams/architecture.dot -o docs/assets/diagrams/architecture.svg
dot -Tpng -Gdpi=160 docs/assets/diagrams/architecture.dot -o docs/assets/diagrams/architecture.png
dot -Tsvg docs/assets/diagrams/qa-flow.dot -o docs/assets/diagrams/qa-flow.svg
dot -Tpng -Gdpi=160 docs/assets/diagrams/qa-flow.dot -o docs/assets/diagrams/qa-flow.png
```

架构图表达主要职责与依赖，省略了逐项数据访问和埋点连接；观测贯穿应用调用链。Neo4j、Redis L2、Langfuse 均按配置启用，本地索引与 Milvus 为替代后端。

问答图限定为“已选择知识库”的路径，不包含未限定范围时的 Web / direct 路由。绿色为通过路径，橙色为纠正分支；返回步骤使用编号标记，避免回连线穿过主流程。无可用资料或答案有效性失败时尝试改写；依据检查失败时使用当前来源重新生成，各自受次数限制。安全或基础设施失败独立终止，不进入质量重试。资料不足结果与安全拦截是不同状态，不能合并计数。

依据：[Orchestrator](../../../backend/app/agents/orchestrator.py)、[ChatService](../../../backend/app/services/chat_service.py)、[KnowledgeService](../../../backend/app/services/knowledge_service.py)。
