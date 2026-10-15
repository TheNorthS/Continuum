# 把数据库读取和同步检索计算卸载到线程，配合异步模型调用。
import asyncio

# 提供 BM25 的对数项和记忆新鲜度的指数衰减。
import math

# Counter 统计词频，defaultdict 累加各召回通道的排名分。
from collections import Counter, defaultdict

# 与知识检索复用同一分词方法，支持中英文词项匹配。
from app.knowledge.rag.embedding import tokenize

# 使用稳定配置指纹和文本规范化规则。
from app.memory.policy import digest, normalized


# 标识当前 Embedding 空间，避免把不同模型生成的向量混用。
def embedding_signature(settings):
    # 将影响向量兼容性的配置整体计算为稳定指纹。
    return digest(
        [
            # 供应商或实现类型属于向量空间的一部分。
            settings.embedding_provider,
            # 模型名称变化后旧向量不应直接参与检索。
            settings.embedding_model,
            # 维度必须相同，才能逐元素计算相似度。
            settings.embedding_dimension,
            # 端点也参与签名，区分同名但由不同服务提供的模型。
            settings.embedding_base_url,
        ]
    )


# 把数据库记忆转换成供检索、精排和提示词使用的可读文本。
def render(record):
    # 读取经存储层解密后的业务负载。
    p = record["payload"]
    # 保留主体、属性、值、类别、版本及更新时间，帮助模型理解适用背景。
    return f"{p['subject']} / {p['predicate']}：{p['value']}（{record['category']}；版本{record['version']}；时间{record['updated_at']:.0f}）"


# 在已授权的 SQL 记忆集合上执行精确匹配、BM25 与向量召回，再做 RRF 融合。
def hybrid(query, rows, vector, signature, limit):
    # 对每条记忆的展示文本分词，统计文档内词频。
    frequencies = [Counter(tokenize(render(row))) for row in rows]
    # 每条记忆中的词只计一次，得到文档频率 DF。
    df = Counter(t for freq in frequencies for t in freq)
    # 求各记忆的词项总数，用于 BM25 长度归一化。
    lengths = [sum(f.values()) for f in frequencies]
    # 计算平均长度，并为无数据或全空文本保留非零分母。
    avg = sum(lengths) / max(1, len(rows)) or 1
    # 查询词去重，使重复输入同一个词不会被重复累加。
    query_words = set(tokenize(query))
    # 分别保存精确、词法、稠密三路的候选索引及分数。
    exact, lexical, dense = [], [], []
    # 同步遍历记录与词频，strict=True 防止二者长度错位。
    for i, (row, freq) in enumerate(zip(rows, frequencies, strict=True)):
        # 组合主体和属性并规范化，作为精确通道的匹配文本。
        key = normalized(row["payload"]["subject"] + " " + row["payload"]["predicate"])
        # 查询包含主体加属性或主体本身时，进入精确匹配通道。
        if key in normalized(query) or normalized(row["payload"]["subject"]) in normalized(query):
            # 精确命中统一给 1.0，通道内部同分时再按 ID 排序。
            exact.append((i, 1.0))
        # 累加查询词对当前记忆的 BM25 贡献。
        score = sum(
            # 采用带平滑的 IDF，让较少见的词贡献更多。
            math.log(1 + (len(rows) - df[t] + 0.5) / (df[t] + 0.5))
            # 乘以当前词在此记忆中的出现次数。
            * freq[t]
            # 这里的 2.2 对应 BM25 参数 k1 + 1，k1 固定为 1.2。
            * 2.2
            # 采用 k1=1.2、b=0.75 的词频饱和和长度归一化。
            / (freq[t] + 1.2 * (0.25 + 0.75 * lengths[i] / avg))
            # 只累加去重后的查询词；未出现的词频为零。
            for t in query_words
        )
        # 没有正向词法相关性的记录不进入 BM25 通道。
        if score > 0:
            # 保存当前记录索引及 BM25 分数。
            lexical.append((i, score))
        # 向量召回必须同时满足下列兼容性条件。
        if (
            # 本次查询已经生成非空向量。
            vector
            # 当前记忆存在已保存的非空向量。
            and row["embedding"]
            # 记忆的 Embedding 配置签名与当前服务一致。
            and row["embedding_signature"] == signature
            # 再检查实际维度，防止异常数据导致错位计算。
            and len(vector) == len(row["embedding"])
        ):
            # 计算向量点积；仅在两侧向量已归一化时才等同余弦相似度。
            similarity = sum(a * b for a, b in zip(vector, row["embedding"], strict=True))
            # 过滤点积不大于 0.05 的低相似候选。
            if similarity > 0.05:
                # 将通过阈值的记录加入稠密召回通道。
                dense.append((i, similarity))
    # 为每条记录累计 RRF 分数，默认从零开始。
    scores = defaultdict(float)
    # 记录候选命中的通道，保留可解释的召回证据。
    channels = defaultdict(list)
    # 按三个独立通道进行融合，不直接相加不同尺度的原始分数。
    for name, ranking in (("exact", exact), ("bm25", lexical), ("vector", dense)):
        # 通道内名次从 1 开始，用于倒数排名公式。
        for rank, (index, _) in enumerate(
            # 各通道先按分数降序、ID 稳定排序，再截取 limit 个候选。
            sorted(ranking, key=lambda pair: (-pair[1], rows[pair[0]]["id"]))[:limit],
            # 排名从 1 开始，避免出现零名次。
            1,
        ):
            # 使用 k=60 的 RRF；同一记录在多个通道出现时累加贡献。
            scores[index] += 1 / (60 + rank)
            # 记下本次贡献来自哪个通道。
            channels[index].append(name)
    # 输出融合排序后的完整记录及补充检索字段。
    return [
        # 保留原始记录负载，附带融合分数和命中通道。
        {**rows[i], "score": score, "channels": channels[i]}
        # 按融合分数降序并用 ID 打破同分，最终最多返回 limit 条。
        for i, score in sorted(scores.items(), key=lambda p: (-p[1], rows[p[0]]["id"]))[:limit]
    ]


# 编排授权读取、混合召回、可选精排、MMR 去重与 Token 预算选择。
class MemoryRetriever:
    # 注入共享服务，不为每次查询创建独立模型或连接池。
    def __init__(self, settings, store, embedder, reranker, meter):
        # 将五项依赖绑定到实例，后续查询复用它们。
        self.settings, self.store, self.embedder, self.reranker, self.meter = (
            # 检索开关、候选数、权重及预算配置。
            settings,
            # 负责实时权限、状态和证据检查的数据库存储层。
            store,
            # 负责生成查询向量的共享 Embedding 服务。
            embedder,
            # 负责可选模型精排的服务。
            reranker,
            # 负责统一输入成本计量的服务。
            meter,
        )

    # 为当前用户和会话范围挑选可注入的相关记忆。
    async def search(self, uid, query, scope, budget=None):
        # 先从存储层读取已授权、有效且处于 active 状态的记录。
        rows = await asyncio.to_thread(self.store.list_records, uid, scope)
        # 没有可用记忆时无需调用任何模型。
        if not rows:
            # 直接返回空候选集。
            return []
        # 计算本轮所需的 Embedding 空间签名。
        signature = embedding_signature(self.settings)
        # 默认不做向量召回，直到确认存在可兼容的历史向量。
        vector = None
        # 只有打开向量开关且至少有可用向量时，才生成查询 Embedding。
        if self.settings.memory_vector_enabled and any(
            # 扫描记录，确认存在签名匹配的非空向量。
            r["embedding"] and r["embedding_signature"] == signature
            # 扫描当前授权集合中的所有记忆。
            for r in rows
        ):
            # 批量接口接收一个问题，取回唯一的查询向量。
            vector = (await self.embedder.embed([query]))[0]
        # 在线程中完成本地混合检索，避免 CPU 计算阻塞事件循环。
        candidates = await asyncio.to_thread(
            # 传入授权集合、查询向量、空间签名以及候选数上限。
            hybrid,
            # 传入用户本轮查询文本。
            query,
            # 只在存储层已经授权的记录集合中计算。
            rows,
            # 查询向量可能为 None，此时跳过稠密召回。
            vector,
            # 用配置签名排除不兼容的历史向量。
            signature,
            # 按记忆候选数限制各通道及融合结果。
            self.settings.memory_candidates,
        )
        # 三路检索均无候选时结束流程。
        if not candidates:
            # 保持没有记忆就不注入的行为。
            return []
        # 建立 ID 到完整记录的映射，精排后仍能恢复证据和生命周期字段。
        by_id = {r["id"]: r for r in candidates}
        # 将记忆适配为现有 Rerank 服务的资料格式。
        sources = [
            # 包含可读文本、RRF 分数和 memory 类别标记。
            {"id": r["id"], "text": render(r), "score": r["score"], "metadata": {"category": "memory"}}
            # 为每个混合召回候选生成一条精排输入。
            for r in candidates
        ]
        # 同时满足记忆精排开关和 Rerank 服务可用配置才执行精排。
        if self.settings.memory_rerank_enabled and self.reranker.enabled:
            # 准备汇总各批次的精排输出。
            ranked = []
            # 批次大小至少为 12，并兼容通用精排候选数配置。
            batch_size = max(self.settings.rerank_candidates, 12)
            # 按批次边界遍历全部记忆资料。
            for start in range(0, len(sources), batch_size):
                # 调用共享精排器，并合并本批返回的结果。
                ranked.extend(await self.reranker.rank(query, sources[start : start + batch_size]))
            # 优先使用模型精排分数，全体批次合并后重新排序。
            sources = sorted(ranked, key=lambda s: s.get("rerank_score", s["score"]), reverse=True)
        # 收集带最终综合评分的记忆记录。
        ordered = []
        # 读取当前时间，用于计算最近使用后的衰减天数。
        import time

        # 按精排或 RRF 次序遍历候选，名次从 1 开始。
        for rank, source in enumerate(sources, 1):
            # 取回对应的完整记忆对象。
            row = by_id[source["id"]]
            # 优先从上次使用时间计算年龄，否则从更新时间计算，并避免负天数。
            age = max(0, time.time() - (row["last_used_at"] or row["updated_at"])) / 86400
            # 经历型记忆按每日 0.015 衰减，其他记忆按 0.001 衰减。
            decay = math.exp(-(0.015 if row["category"] == "episode" else 0.001) * age)
            # 有精排分数则直接采用；否则用排名倒数作为相关性。
            relevance = source.get("rerank_score", 1 / rank)
            # 给候选附加用于后续 MMR 的综合分数。
            row["rank_score"] = (
                # 相关性占 55%，置信度占 20%，效用占 15%，时间新鲜度占 10%。
                0.55 * relevance + 0.20 * row["confidence"] + 0.15 * row["utility"] + 0.10 * decay
            )
            # 把已评分记录加入待选择集合。
            ordered.append(row)
        # 分别保存选中记录及其词项集合，供去重比较。
        selected, token_sets = [], []
        # 记忆预算同时受调用方预算和模型窗口比例限制。
        remaining = min(
            # 使用传入的非零预算，否则采用全局记忆预算。
            budget or self.settings.memory_token_budget,
            # 记忆总预算最多占主模型窗口的 8%。
            int(0.08 * self.meter.limits(self.settings.llm_model)["window"]),
        )
        # 逐条挑选候选，直到所有候选都已考察。
        while ordered:
            # 计算最大边际相关性 MMR，兼顾价值和与已选记忆的差异。
            def mmr(row):
                # 将当前记录转换为去重后的词项集合。
                words = set(tokenize(render(row)))
                # 取其与任一已选记忆的最大词项重合度。
                similarity = max(
                    # 用 Jaccard 交并比衡量重复；尚未选中任何记录时重复度为零。
                    (len(words & old) / max(1, len(words | old)) for old in token_sets),
                    # 还没有选中记忆时，最大重复度默认为零。
                    default=0,
                )
                # 返回价值奖励减去重复惩罚。
                return (
                    # eta 控制综合相关性分数的权重。
                    self.settings.memory_mmr_eta * row["rank_score"]
                    # 剩余权重惩罚与已选记录最相似的部分。
                    - (1 - self.settings.memory_mmr_eta) * similarity
                )

            # 挑选当前 MMR 分数最高的记录。
            row = max(ordered, key=mmr)
            # 无论是否能放入预算，都从待选集合移除，保证循环推进。
            ordered.remove(row)
            # 通过统一计量器计算单条记忆作为用户消息时的成本。
            cost = await self.meter.count(
                # 使用主模型及包含该记忆的最小消息请求进行计量。
                {"model": self.settings.llm_model, "messages": [{"role": "user", "content": render(row)}]}
            )
            # 只有单条成本不超过剩余预算才注入。
            if cost <= remaining:
                # 记录已选中的完整记忆。
                selected.append(row)
                # 缓存已选记忆词项，供后续候选计算重复惩罚。
                token_sets.append(set(tokenize(render(row))))
                # 扣减本条成本，约束总注入量。
                remaining -= cost
        # 模型调用和排序期间权限可能变化，返回前再次核对全部引用。
        if not await asyncio.to_thread(
            # 复查当前用户、选中 ID 与会话范围，不沿用最初读取时的授权结论。
            self.store.validate_references,
            # 按本轮用户身份复核引用。
            uid,
            # 只检查预算内真正选择的记忆 ID。
            [r["id"] for r in selected],
            # 使用同一会话资料范围，避免校验口径放宽。
            scope,
        ):
            # 任何引用失效时整批放弃，避免返回部分过时证据。
            return []
        # 返回通过最终来源校验的预算内记忆。
        return selected
