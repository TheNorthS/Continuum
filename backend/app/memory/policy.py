# SHA-256 用于生成稳定指纹；这里的摘要不承担数据加密职责。
import hashlib

# 把结构化数据序列化成可比较、可扫描的 JSON 文本。
import json

# 用正则补充检测中文敏感字段与私钥标记。
import re

# 统一全角、兼容字符和其他 Unicode 表示形式。
import unicodedata

# 复用全局脱敏规则，避免记忆另建一套不一致的敏感词策略。
from app.common.redaction import redact

# 使用严格的工作摘要模型验证 LLM 输出结构。
from app.memory.schemas import WorkingSummary


# 对可 JSON 序列化的数据计算稳定指纹，供版本、证据与任务幂等校验使用。
def digest(value):
    # 对规范化后的字节内容计算 SHA-256。
    return hashlib.sha256(
        # 键名排序并去除多余分隔空格，保留中文，避免字典插入顺序影响指纹。
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        # 返回十六进制摘要，方便存储与比较。
    ).hexdigest()


# 把文本转成用于去重和匹配的规范形式，不用于替换用户原文。
def normalized(value):
    # NFKC 统一兼容字符，casefold 统一大小写，再折叠连续空白。
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


# 判断候选内容是否包含需要拒绝写入记忆的敏感信息。
def sensitive(value):
    # 字符串直接扫描；其他结构先展开为包含全部字段的 JSON。
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    # 脱敏前后有差异即命中；否则继续检查额外的中文凭据模式。
    return redact(text) != text or bool(
        # 查找敏感字段赋值或 PEM 私钥头，而非只判断是否提及这些词。
        re.search(
            # 匹配冒号或等号后的非空值，以及 PRIVATE KEY 标记；忽略字母大小写。
            r"(?:密码|密钥|身份证|银行卡|护照|私钥)\s*[:：=]\s*\S+|-----BEGIN .*PRIVATE KEY",
            # 在完整序列化文本中查找敏感模式。
            text,
            # 忽略大小写，兼容不同形式的私钥标记。
            re.I,
        )
    )


# 计算记忆效用分数；接纳阈值和降级策略由服务层应用。
def utility(candidate):
    # 用户明确陈述获得较高证据质量权重；其他类型在这里仅得到较低权重。
    quality = 1.0 if candidate["evidence_type"] == "user_explicit" else 0.85
    # 按固定权重加总五项指标，结果用于候选筛选与后续排序。
    return (
        # 复用价值占 30%，优先保留未来还能派上用场的信息。
        0.30 * candidate["reuse"]
        # 出现频度占 25%，衡量信息是否反复出现。
        + 0.25 * candidate["frequency"]
        # 稳定性占 20%，减少短暂状态进入长期记忆。
        + 0.20 * candidate["stability"]
        # 新颖性占 15%，衡量新增信息量。
        + 0.15 * candidate["novelty"]
        # 证据质量占 10%，与上述效用指标共同决定结果。
        + 0.10 * quality
    )


# 检查摘要结构、证据 ID 和任务状态一致性；语义核验由独立模型调用完成。
def validate_summary(value, event_ids):
    # 拒绝未知字段及不符合长度、类型约束的输出，并转回普通字典。
    summary = WorkingSummary.model_validate(value).model_dump()
    # 先收集三个单值事实：原始目标、当前状态和下一步行动。
    facts = [summary["original_goal"], summary["current_status"], summary["next_action"]]
    # 再遍历五个列表型事实字段。
    for field in ("constraints", "completed", "pending_tasks", "decisions", "errors"):
        # 将列表事实并入同一集合，统一检查引用。
        facts.extend(summary[field])
    # 所有证据 ID 必须来自本次允许使用的原始事件集合。
    if any(not set(f["evidence_ids"]).issubset(event_ids) for f in facts):
        # 发现模型编造或越界引用时拒绝摘要。
        raise ValueError("summary cites unknown event")
    # 将已完成任务规范化后，与待办任务文本集合求交集。
    if {normalized(f["text"]) for f in summary["completed"]} & {
        # 对待办项采用同样的规范化规则，避免大小写和空白掩盖重复状态。
        normalized(f["text"])
        # 对每个待办事实执行相同的文本规范化。
        for f in summary["pending_tasks"]
    }:
        # 同一任务不能同时被标成已完成和待办。
        raise ValueError("inconsistent task state")
    # 返回通过结构与引用检查的摘要，供安全审查和语义核验继续处理。
    return summary


# 按完整回合划分安全压缩片段，避免截断工具调用及其结果。
def closed_segments(events):
    """Cut only after complete turns; tool calls/results must balance within a segment."""
    # 分别保存已闭合片段、当前片段、尚未收到结果的工具调用 ID。
    segments, current, opened = [], [], set()
    # 按调用方给出的事件顺序扫描历史。
    for event in events:
        # 每个事件先加入当前片段，直到满足闭合条件。
        current.append(event)
        # 工具调用事件需要登记配对标识。
        if event["kind"] == "tool_call":
            # 缺少调用 ID 或重复打开同一 ID 都破坏工具配对关系。
            if not event["tool_call_id"] or event["tool_call_id"] in opened:
                # 拒绝无标识或重复的未闭合工具调用。
                raise ValueError("duplicate/open tool call")
            # 记录等待结果的调用 ID。
            opened.add(event["tool_call_id"])
        # 工具结果必须找到此前仍未完成的调用。
        if event["kind"] == "tool_result":
            # 没有对应调用的结果属于孤立工具消息。
            if event["tool_call_id"] not in opened:
                # 拒绝孤立结果，防止压缩时制造不完整的上下文。
                raise ValueError("orphan tool result")
            # 移除已配对调用，表示该工具动作闭合。
            opened.remove(event["tool_call_id"])
        # 助手消息结束且没有悬空工具调用时，才允许作为切分边界。
        if event["kind"] == "assistant_message" and not opened:
            # 保存完整片段，供压缩器选择连续的旧前缀。
            segments.append(current)
            # 重新开始收集下一回合。
            current = []
    # 同时返回完整片段和未闭合尾部；尾部仍保留原文，不作为压缩切点。
    return segments, current
