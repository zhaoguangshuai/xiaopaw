"""Agent output models."""

from pydantic import BaseModel, Field


class MainTaskOutput(BaseModel):
    reply: str = Field(..., description="发送给飞书用户的回复内容")
    used_skills: list[str] = Field(default_factory=list, description="本次调用的 Skill 名称列表")
