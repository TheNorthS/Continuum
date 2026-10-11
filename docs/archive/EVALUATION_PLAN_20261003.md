> 历史记录：本页记录当时的环境、配置和测试结果，不代表当前安装状态或最终成绩。当前入口见[文档导航](../README.md)，最终结果见[公开数据集评测](../FULL_EVALUATION.md)。原始日志仅在本地生成。

# RAG 公开数据集评测方案

当前组合为 **DeepSeek `deepseek-flash` + OpenAI `text-embedding-3-small`（1536 维）+ 硅基流动 `Qwen/Qwen3-Reranker-0.6B`**。测试分为检索专项和完整问答两类。CMRC2018 仅保留为此前下载的阅读理解参考资料，不再作为本轮主数据集。

首轮真实模型运行已完成：[实测汇总与失败分析](EVALUATION_RESULTS.md)。

**部署对齐：后续正式评测使用 Milvus。** 首轮 local 实验保留为小语料基线；Milvus 使用独立集合和同一批文档向量重新检索、精排及回答。配置与复跑命令见 [Milvus 评测配置](../EVALUATION.md)。

Milvus 的同样本复测已完成：[最新 Milvus 结果](MILVUS_EVALUATION_RESULTS.md)。

**更新：当前生产对齐版本改为读取 `config/rag.production.json`，不再强制 1000 字符/关闭语义分块。** 以下参数中涉及 1000/150 的是首轮消融基线的历史设计；正式规则为语义分块 1500/150、共享缓存策略，该轮图谱融合两边关闭。新分块已从原始文档重新嵌入建索引，关闭图谱不会改变这些向量。生产参数管理、差异审计与当前重建命令见 [共享配置](../SHARED_PIPELINE.md)。

## 1. 先检查连通性

```powershell
cd <project-root>\backend
.\.venv\Scripts\python.exe -m app.evaluation.configuration --probe
```

已完成真实 API 探针：OpenAI 返回两条 1536 维向量；DeepSeek 生成、JSON 评分和内容审查通过；Qwen 精排返回有效索引/分数，相关合成资料排名第 1。记录为 `test-results/evaluation-connectivity.json`。它只是连接检查，不是质量评测成绩。

## 2. 公开测试集与首轮规模

| 数据集 | 原始用途 | 首轮问题 / 资料 | 使用方式 |
|---|---|---|---|
| [T2Retrieval](https://huggingface.co/datasets/C-MTEB/T2Retrieval) + [qrels](https://huggingface.co/datasets/C-MTEB/T2Retrieval-qrels) | 中文检索，有多相关文档标注 | 20 题 / 200 篇 | 全部 117 篇标注相关资料 + 固定随机干扰资料 |
| [CRUD-RAG](https://github.com/IAAR-Shanghai/CRUD_RAG) 的问答任务 | 专门用于中文 RAG | 12 题 / 100 篇 | 单/双/三文档各 4 题，24 篇关联新闻 + 固定随机干扰新闻 |

首轮是明确标注的 **subset/pilot**，不是完整榜单。小语料和随机干扰通常比全量检索简单，不能把其高分泛化为系统总体性能。

### 固定数据版本

- T2Retrieval：`8731a845f1bf500a4f111cf1070785c793d10e64`。
- T2 qrels：`1c83b8d1544e529875e3f6930f3a1fcf749a8e97`。
- CRUD-RAG：`1aace383994e1f68efa12cf2a8e2dadfb4102ceb` 的 `data/crud_split/split_merged.json`。
- 原文件缓存于 `.cache/evaluation/datasets`，每次 run 保存 SHA-256 和来源 URL。

### 采样和来源规则

T2 从有正例标注的问题中固定 seed `42` 抽样，所有抽中问题的正例完整保留。语料完全相同的重复文档归并为同一证据项，qrels 同步映射，别名保存在 manifest 中。干扰资料从完整 T2 语料池以 `seed+1` 固定抽取。

CRUD 轮流抽取 `questanswer_1doc / questanswer_2docs / questanswer_3docs`，避免一个事件重复选取多个任务变体。只将 `news1/news2/news3` 作为语料；`questions / answers / thoughts / event` 都不进入索引。参考答案只供评分器使用。

CRUD 首轮干扰新闻来自其他 QA 关联新闻，不是官方 8 万篇完整语料。将 `news1/2/3` 关联视作预期来源，是项目内诊断标签，不是穷尽所有相关资料的人工 qrels；其来源 Recall 应单列解释。

## 3. 标准指标

### 检索

使用 `ir_measures` 根据 qrels 和结果排名计算：

- **Recall@1/4/5/10/32**：命中的标注相关文档占全部正例的比例。
- **Precision@1/4/5/10/32**：前 K 位中相关结果所占比例，返回不足 K 仍按固定 K 计算。
- **Hit@K**：前 K 位是否至少命中一条正例。
- **MRR@10**：首个相关结果的倒数排名，截断到前 10，未命中为 0。
- **nDCG@10**：衡量前 10 位整体相关性排序。

同一文档多个 chunk 先按首次出现去重，然后对文档排名评分；模型实际获得的是前 4 个 chunk，原始 chunk 列表另行保留。文档级命中不代表答案所在片段一定进入了生成上下文。

### 回答

CRUD 参考答案多为说明性文字，首轮输出：

- **normalized EM**：规范化后完全匹配，不应期待改写答案取得很高分。
- **character F1**：按中文字符重合计算，用于诊断内容覆盖。
- **character ROUGE-L**：由 `rouge-score` 使用字符 tokenizer 计算顺序重合。
- **回答状态分布**：正常回答、拒答、内容拦截、超时/其他错误。
- **回答耗时与提供商 Token 用量**：注明缓存与计时范围。

评分文本只清理已知数字引用、空白、标点并做 Unicode 归一化。拒答、拦截、空结果和错误都保留在主分母中，重合指标计 0；原始输出仍保存。字符级 F1/ROUGE **不是语义准确率**，也不同于原 CRUD 的 jieba 分词口径，不能声称复现了官方成绩。

## 4. 精排对照如何公平

核心对照组为：

1. **RRF**：真实 BM25 + OpenAI 向量混合召回，RRF 候选池取前 32。
2. **RRF + Qwen**：对完全相同的 32 条候选评分重排，再取前 4 进入回答。

公共 `KnowledgeService.candidates()` 是实际问答使用的融合候选接口，评测也调用它。每题候选池持久化并复用，避免精排开关同时改变候选召回数量而造成混淆。不同问题保持相同 chunk size=1000、overlap=150，首轮关闭语义切分以固定切块行为；真实 Embedding 用于切块后的索引与检索。

CRUD 经原有 `ChatService → Orchestrator` 执行，保留输入/来源/输出审查、LLM 相关性过滤、有限次改写/生成与最终引用权限复核。每题每组使用独立会话，固定评测知识库；两组的先后顺序交替。

首轮不启动 Web、Neo4j、共享 Redis 或 Langfuse，不读取工作台数据库。精排失败不能伪装为 RRF 成功，实验配置固定 `failure_mode=error`。

配对 Recall、MRR、nDCG 差值附 bootstrap 区间；小样本、同类查询相关性及子集设计会限制统计结论。先检查排名改善/退化案例，再决定是否扩大规模或调整候选池。

## 5. 第三方框架的角色

| 工具 | 本轮状态 | 作用 |
|---|---|---|
| 自有运行器 | 已接入 | 调用应用实际服务，管理隔离数据、固定采样、断点恢复和逐题结果 |
| `ir_measures` | 已接入并锁定依赖 | 标准检索指标，不用 LLM 当裁判 |
| `rouge-score` | 已接入并锁定依赖 | 明确字符口径的 ROUGE-L |
| BEIR/TREC 数据格式 | 导出 | 保存 corpus/query/qrels 和排名，便于复核 |
| Ragas | 首轮未调用 | 第二阶段辅助判断 Faithfulness / FactualCorrectness，不能替代 qrels 与标准答案指标 |

Ragas 的事实与忠实性评分需要指定评委模型、额外调用量和人工校准。当前只配置了生成模型，没有指定独立裁判，因此本轮不将同一个 DeepSeek 自评分当作客观准确率。后续可对代表性样本增加独立裁判或带明确偏差说明的同源裁判。

指标参考：[ir_measures](https://ir-measur.es/en/latest/measures.html)、[Ragas Faithfulness](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/faithfulness/)、[Ragas FactualCorrectness](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/factual_correctness/)。

## 6. 运行命令

后端目录安装评测依赖（已有本地模型扩展可保留）：

```powershell
uv sync --extra dev --extra milvus --extra local-models --extra evaluation

# 只准备数据清单，不调用模型
.\.venv\Scripts\python.exe -m app.evaluation.run --dataset t2 --questions 20 --documents 200 --run-name t2-pilot-20 --prepare-only

# 真实检索与在线精排
.\.venv\Scripts\python.exe -u -m app.evaluation.run --dataset t2 --questions 20 --documents 200 --run-name t2-pilot-20

# 单/多文档真实问答对照
.\.venv\Scripts\python.exe -u -m app.evaluation.run --dataset crud --questions 12 --documents 100 --run-name crud-pilot-12

# 只重生成已保存结果的报告，不调用模型
.\.venv\Scripts\python.exe -m app.evaluation.run --dataset crud --questions 12 --documents 100 --run-name crud-pilot-12 --report-only
```

再次执行相同 run 会跳过已导入文档与已完成题目，复用候选池；不能复用同名 run 更换模型、数据或主要参数。更改配置请换 `--run-name`。中途失败题仍保留在本次成绩中，需要重试实验时另建 run，避免选择性删除失败导致虚高。

首轮命令会调用已配置的 OpenAI、DeepSeek 与硅基流动服务，产生相应 API 用量。费用需按各自账单和实际 Token 计算，报告不捏造单价。

## 7. 输出与后续步骤

每个实验的结果位于 `test-results/evaluation/<run-name>/`：

```text
manifest.json          数据版本、SHA、配置、题目 ID、包版本（无密钥）
dataset.json           固定的问题、标准答案和子集语料
document-map.json      数据集来源 ID 到独立索引 document ID
candidate-cache/       真实候选池与模型分数，便于复核对照
results.json           断点恢复用的逐题记录
queries.jsonl          每题排名、答案、引用、状态与错误
metrics.json / .csv    检索、回答指标、分母、耗时与用量
usage.json             提供商返回的数值用量与请求状态，不存 API Key
qrels.tsv              正例标注
rrf.run / rerank.run   各自独立的标准 TREC 排名记录
report.md              可阅读的实验结果与限制
.runtime/              与业务服务隔离的数据库及密钥
```

下一阶段依据试跑结果修正明确的系统问题，再固定参数扩大到 100–200 题。更大语料会显著提高索引与向量查询成本，需要改用独立 Milvus 实验配置后再做完整 T2/CRUD 全库验证。CMRC 小型阅读理解检查、图谱开关和独立 LLM 裁判可作为后续专项，均单独标记，不混成一个总分。
