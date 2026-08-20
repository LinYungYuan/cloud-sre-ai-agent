from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SkillFrontmatter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    agent: Literal["metrics", "trace", "log", "rca"]
    description: str = Field(min_length=1)
    required_capabilities: tuple[str, ...]
    risk: Literal["READ_ONLY"]


class SkillSpec(SkillFrontmatter):
    body: str = Field(min_length=1)
