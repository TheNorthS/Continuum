# 用 Literal 限定模型协议中的枚举值，拒绝未约定的类别和关系。
from typing import Literal

# 使用 Pydantic 校验结构、字段约束及严格类型。
from pydantic import BaseModel, ConfigDict, Field


# 摘要中的最小事实单元：一段陈述及其原始事件证据。
class EvidenceFact(BaseModel):
    # 禁止额外字段，并去掉字符串首尾空白。
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    # 事实文本不能为空，最多 4,000 个字符。
    text: str = Field(min_length=1, max_length=4000)
    # 每个事实必须引用 1 至 12 条证据；ID 的真实性由策略层复核。
    evidence_ids: list[str] = Field(min_length=1, max_length=12)


# 固定工作摘要结构，明确区分目标、进展、约束、待办和下一步。
class WorkingSummary(BaseModel):
    # 拒绝模型自由添加字段，统一文本空白处理。
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    # 保存最初的用户目标，压缩器会用原文再次覆盖这一项。
    original_goal: EvidenceFact
    # 描述目前所处阶段及已知状态。
    current_status: EvidenceFact
    # 最多保留 12 条约束，每条都必须带证据。
    constraints: list[EvidenceFact] = Field(max_length=12)
    # 记录已完成任务，后续检查其不能与待办项重复。
    completed: list[EvidenceFact] = Field(max_length=12)
    # 记录尚未完成的任务。
    pending_tasks: list[EvidenceFact] = Field(max_length=12)
    # 保留已经做出的关键决定及依据。
    decisions: list[EvidenceFact] = Field(max_length=12)
    # 保留错误或失败经验，减少后续重复尝试。
    errors: list[EvidenceFact] = Field(max_length=12)
    # 用一个带证据的事实描述下一步行动。
    next_action: EvidenceFact


# 记忆提炼模型的候选协议；通过结构校验后仍需证据和安全检查。
class Candidate(BaseModel):
    # 禁止额外字段，避免模型自行写入租户或数据库标识。
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    # 候选只允许事实、偏好、决定、经历四类。
    category: Literal["fact", "preference", "decision", "episode"]
    # 事实主体限制为 1 至 120 字符，参与稳定键生成。
    subject: str = Field(min_length=1, max_length=120)
    # 事实属性限制为 1 至 120 字符，与主体共同确定记忆身份。
    predicate: str = Field(min_length=1, max_length=120)
    # 属性值限制为 1 至 1,200 字符。
    value: str = Field(min_length=1, max_length=1200)
    # 必须给出原始事件引用，不能只返回无来源结论。
    evidence_ids: list[str] = Field(min_length=1, max_length=12)
    # 保存候选对应的逐字引文，服务层会验证其出现在原文中。
    quote: str = Field(min_length=1, max_length=1200)
    # 声明证据类型；当前自动提炼链路进一步只接纳 user_explicit。
    evidence_type: Literal["user_explicit", "tool_verified", "inferred"]
    # 置信度必须为有限的 0 到 1 数值，不接受字符串等类型强制转换。
    confidence: float = Field(strict=True, ge=0, le=1, allow_inf_nan=False)
    # 复用价值参与效用评分，限定为有限的 0 到 1 数值。
    reuse: float = Field(strict=True, ge=0, le=1, allow_inf_nan=False)
    # 出现频度参与效用评分，采用相同的数值范围约束。
    frequency: float = Field(strict=True, ge=0, le=1, allow_inf_nan=False)
    # 稳定性衡量信息是否适合长期保留。
    stability: float = Field(strict=True, ge=0, le=1, allow_inf_nan=False)
    # 新颖性衡量候选相比已有知识提供了多少新信息。
    novelty: float = Field(strict=True, ge=0, le=1, allow_inf_nan=False)


# 一次提炼调用的顶层返回结构。
class Extraction(BaseModel):
    # 禁止 memories 之外的字段，保持解析协议明确。
    model_config = ConfigDict(extra="forbid")
    # 每次调用最多返回 12 条候选，可以返回空列表。
    memories: list[Candidate] = Field(max_length=12)


# 描述候选与已有记忆的关系，供第二阶段整合使用。
class Relation(BaseModel):
    # 拒绝未约定字段，避免模型影响其他写入属性。
    model_config = ConfigDict(extra="forbid")
    # 仅允许重复、细化、矛盾或独立四种关系。
    relation: Literal["duplicate", "refinement", "contradiction", "independent"]
    # 关联的邻居 ID 必须显式给出或为 None；服务层还会核对其候选范围。
    neighbor_id: str | None


# 独立语义证据核验的最小返回结构。
class Verification(BaseModel):
    # 只允许 supported 字段，避免把自由文本解释误用为控制信息。
    model_config = ConfigDict(extra="forbid")
    # 必须返回真正的布尔值；不能用字符串 true 或数字替代。
    supported: bool = Field(strict=True)
