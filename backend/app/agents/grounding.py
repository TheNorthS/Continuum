"""Bounded evidence feedback, kept inside the current answer attempt."""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GroundingIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    claim: str = Field(min_length=1, max_length=400)
    reason: str = Field(min_length=1, max_length=600)


class GroundingVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supported: bool = Field(strict=True)
    issues: list[GroundingIssue] = Field(max_length=4)

    @model_validator(mode="after")
    def consistent(self):
        if (self.supported and self.issues) or (not self.supported and not self.issues):
            raise ValueError("supported=true requires no issues; supported=false requires evidence issues")
        return self
