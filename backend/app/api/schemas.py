from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=128)
    challenge: str = Field(min_length=20, max_length=100)


class LoginCredentials(Credentials):
    # Login verifies an existing credential; password creation policy belongs to registration/reset.
    password: str = Field(min_length=1, max_length=128)


BaseIds = Annotated[
    list[Annotated[str, Field(min_length=1, max_length=32)]], Field(min_length=1, max_length=100)
]
AttachmentIds = Annotated[list[Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]], Field(max_length=10)]


class AttachmentSelection(BaseModel):
    attachment_ids: AttachmentIds  # [] 明确清空；PATCH 不接受省略或 null。


class ScopeRequest(BaseModel):
    base_ids: BaseIds | None = None
    use_knowledge: bool | None = None

    @model_validator(mode="after")
    def validate_selection(self):
        if self.use_knowledge is False and self.base_ids is not None:
            raise ValueError("关闭知识库时不得同时指定知识库")
        if self.base_ids is None and self.use_knowledge is not False:
            raise ValueError("请选择知识库或关闭知识库")
        return self


class ConversationCreate(BaseModel):
    title: str = Field(default="新对话", min_length=1, max_length=100)
    base_ids: BaseIds | None = None
    use_knowledge: bool = True
    attachment_ids: AttachmentIds = Field(default_factory=list)


class ChatRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    question: str = Field(min_length=1, max_length=4000)
    mode: Literal["adaptive", "agentic"] = "adaptive"
    base_ids: BaseIds | None = None
    use_knowledge: bool | None = None
    attachment_ids: AttachmentIds | None = None  # 省略/null 沿用会话，[] 清空，列表替换。


class SearchRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=4, ge=1, le=12)
    base_ids: BaseIds | None = None
