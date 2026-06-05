"""Baidu Qianfan web search tool."""

from __future__ import annotations

import json
import logging
import os
from typing import Literal

import requests
from crewai.tools import BaseTool
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://qianfan.baidubce.com/v2/ai_search/web_search"


class BaiduSearchInput(BaseModel):
    query: str
    top_k: int = Field(default=20, ge=0, le=50)
    recency_filter: Literal["week", "month", "semiyear", "year"] | None = None
    sites: list[str] | None = None

    @field_validator("query")
    @classmethod
    def query_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("搜索关键词不能为空")
        return v.strip()

    @field_validator("sites")
    @classmethod
    def sites_max(cls, v: list[str] | None) -> list[str] | None:
        if v and len(v) > 20:
            raise ValueError("最多指定 20 个站点")
        return v


class BaiduSearchTool(BaseTool):
    name: str = "search_web"
    description: str = (
        "搜索互联网获取最新信息。支持指定时间范围和站点过滤。"
        "参数：query（搜索词，必填）、top_k（结果数量，默认20）、"
        "recency_filter（时间过滤）、sites（限定站点列表）。"
    )
    args_schema: type = BaiduSearchInput

    def _run(
        self,
        query: str,
        top_k: int = 20,
        recency_filter: str | None = None,
        sites: list[str] | None = None,
        **_,
    ) -> str:
        api_key = os.environ.get("BAIDU_API_KEY", "")
        if not api_key:
            return "错误：BAIDU_API_KEY 环境变量未设置"

        payload: dict = {
            "messages": [{"role": "user", "content": query}],
            "search_source": "baidu_search_v2",
            "resource_type_filter": [{"type": "web", "top_k": top_k}],
        }
        if recency_filter:
            payload["search_recency_filter"] = recency_filter
        if sites:
            payload["search_filter"] = {"match": {"site": sites}}

        headers = {
            "Content-Type": "application/json",
            "X-Appbuilder-Authorization": f"Bearer {api_key}",
        }

        try:
            resp = requests.post(_SEARCH_URL, json=payload, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except requests.Timeout:
            return "搜索超时，请稍后重试"
        except requests.HTTPError as exc:
            return f"搜索请求失败（HTTP {exc.response.status_code}）"
        except requests.RequestException as exc:
            return f"网络错误：{exc}"
        except json.JSONDecodeError:
            return "搜索结果解析失败"

        if data.get("code"):
            return f"搜索 API 错误：{data.get('message', 'unknown')}"

        refs = data.get("references", [])
        if not refs:
            return f"未找到关于「{query}」的搜索结果"

        lines: list[str] = []
        for ref in refs:
            rid = ref.get("id", "")
            title = ref.get("title", "")
            url = ref.get("url", "")
            content = ref.get("content", "")[:300]
            lines.append(f"结果{rid}: [ {title} ] ( {url} )\n  内容摘要: {content}")

        return "\n\n".join(lines)
