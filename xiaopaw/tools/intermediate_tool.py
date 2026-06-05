"""Intermediate thinking product tool — semantic checkpoint for agent reasoning."""

from __future__ import annotations

import json
from typing import Any

from crewai.tools import BaseTool
from pydantic import BaseModel, field_validator


class IntermediateToolSchema(BaseModel):
    intermediate_product: str

    @field_validator("intermediate_product", mode="before")
    @classmethod
    def coerce(cls, v: Any) -> str:
        if isinstance(v, str):
            return v
        if isinstance(v, list):
            return "\n".join(str(x) for x in v)
        if isinstance(v, dict):
            return json.dumps(v, ensure_ascii=False)
        return str(v)


class IntermediateTool(BaseTool):
    name: str = "Save_Intermediate_Product_Tool"
    description: str = (
        "保存中间思考产物。当你需要记录阶段性思考结论、待办事项列表、"
        "分析结果等中间产物时使用此工具。"
    )
    args_schema: type = IntermediateToolSchema

    def _run(self, intermediate_product: str, **_) -> str:
        return "中间结果已保存，可以进行下一步思考。"
