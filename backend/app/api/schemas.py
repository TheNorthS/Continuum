from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


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


class ScopeRequest(BaseModel):
    base_ids: BaseIds


class ConversationCreate(BaseModel):
    title: str = Field(default="新对话", min_length=1, max_length=100)
    base_ids: BaseIds | None = None


class ChatRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    question: str = Field(min_length=1, max_length=4000)
    mode: Literal["adaptive", "agentic"] = "adaptive"
    base_ids: BaseIds | None = None


class SearchRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=4, ge=1, le=12)
    base_ids: BaseIds | None = None
