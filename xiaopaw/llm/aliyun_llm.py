"""AliyunLLM: CrewAI BaseLLM adapter for Qwen via DashScope-compatible API."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import logging
import os
import re
from pathlib import Path

import requests
from crewai import BaseLLM

logger = logging.getLogger(__name__)

_MCP_LIST_PARAMS = frozenset({"file_types"})
_DEFAULT_TOOL_RESULT_MAX_CHARS = 12_000
_TRUNCATE_SUFFIX = (
    "\n\n[注意] 以上内容已被截断（原始长度超过 {max_chars} 字符）。"
    "如果需要完整内容，请考虑分段处理或使用文件操作工具。"
)

ENDPOINTS = {
    "cn": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
    "intl": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions",
    "finance": "https://dashscope-finance.aliyuncs.com/compatible-mode/v1/chat/completions",
}


def _normalize_mcp_tool_arguments(tool_calls: list[dict]) -> list[dict]:
    result = copy.deepcopy(tool_calls)
    for tc in result:
        fn = tc.get("function", {})
        raw = fn.get("arguments", "{}")
        try:
            args = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            continue
        keys_to_delete = []
        for k, v in list(args.items()):
            if isinstance(v, str):
                if v in ("None", "null"):
                    if k in _MCP_LIST_PARAMS:
                        args[k] = []
                    else:
                        keys_to_delete.append(k)
                elif v == "True":
                    args[k] = True
                elif v == "False":
                    args[k] = False
        for k in keys_to_delete:
            del args[k]
        fn["arguments"] = json.dumps(args, ensure_ascii=False)
    return result


def _truncate_tool_results(
    messages: list[dict], max_chars: int | None = None
) -> list[dict]:
    if max_chars is None:
        max_chars = int(os.environ.get("LLM_TOOL_RESULT_MAX_CHARS", _DEFAULT_TOOL_RESULT_MAX_CHARS))
    result = []
    for msg in messages:
        if msg.get("role") == "tool" and isinstance(msg.get("content"), str):
            content = msg["content"]
            if len(content) > max_chars:
                content = content[:max_chars] + _TRUNCATE_SUFFIX.format(max_chars=max_chars)
                msg = {**msg, "content": content}
        result.append(msg)
    return result


class AliyunLLM(BaseLLM):
    def __init__(
        self,
        model: str,
        image_model: str | None = None,
        api_key: str | None = None,
        region: str = "cn",
        temperature: float | None = None,
        timeout: int = 600,
        retry_count: int | None = None,
    ) -> None:
        super().__init__(model=model, temperature=temperature)
        self.api_key = api_key or os.environ.get("QWEN_API_KEY") or os.environ.get("DASHSCOPE_API_KEY", "")
        self.region = region
        self.endpoint = ENDPOINTS.get(region, ENDPOINTS["cn"])
        self.image_model = image_model or "qwen3-vl-plus"
        self.timeout = timeout
        self.retry_count = retry_count or int(os.environ.get("LLM_RETRY_COUNT", "2"))
        self.debug_payload = os.environ.get("QWEN_DEBUG_PAYLOAD", "").lower() in ("1", "true")

    def supports_function_calling(self) -> bool:
        return True

    def supports_stop_words(self) -> bool:
        return True

    def get_context_window_size(self) -> int:
        m = self.model.lower()
        if "long" in m:
            return 200_000
        if any(k in m for k in ("max", "plus", "turbo", "flash")):
            return 131_072
        return 8192

    def _validate_messages(self, messages: list[dict]) -> None:
        valid_roles = {"system", "user", "assistant", "tool"}
        for msg in messages:
            role = msg.get("role")
            if role not in valid_roles:
                raise ValueError(f"invalid message role: {role}")

    def _normalize_multimodal_tool_result(
        self, messages: list[dict]
    ) -> tuple[list[dict], bool]:
        has_multimodal = False
        result = []
        for msg in messages:
            if msg.get("role") == "assistant" and "Add image to content Local" in str(msg.get("content", "")):
                content = str(msg.get("content", ""))
                b64_match = re.search(r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+", content)
                url_match = re.search(r"Observation:\s*(https?://\S+)", content)
                if b64_match:
                    data_url = b64_match.group(0)
                    result.append({
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "请分析这张图片："},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    })
                    has_multimodal = True
                    continue
                elif url_match:
                    img_url = url_match.group(1)
                    result.append({
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "请分析这张图片："},
                            {"type": "image", "image": img_url},
                        ],
                    })
                    has_multimodal = True
                    continue
            result.append(msg)
        return result, has_multimodal

    def call(
        self,
        messages,
        tools=None,
        callbacks=None,
        available_functions=None,
        max_iterations: int = 10,
        _retry_on_empty: bool = True,
        _empty_retry_count: int = 0,
        **kwargs,
    ) -> str:
        if max_iterations <= 0:
            raise RuntimeError("max_iterations exhausted")

        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]

        messages, has_multimodal = self._normalize_multimodal_tool_result(messages)
        self._validate_messages(messages)
        messages = _truncate_tool_results(messages)

        use_model = self.image_model if has_multimodal else self.model
        payload: dict = {"model": use_model, "messages": messages}
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if tools:
            payload["tools"] = tools

        if self.debug_payload:
            logger.debug("LLM payload: %s", json.dumps(payload, ensure_ascii=False)[:2000])

        if callbacks:
            for cb in callbacks:
                if hasattr(cb, "on_llm_start"):
                    cb.on_llm_start(serialized={}, prompts=[str(messages)])

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        last_exc: Exception | None = None
        for attempt in range(self.retry_count + 1):
            try:
                resp = requests.post(
                    self.endpoint, json=payload, headers=headers, timeout=self.timeout
                )
                if resp.status_code >= 500:
                    if attempt < self.retry_count:
                        logger.warning("5xx (attempt %d): %s", attempt + 1, resp.status_code)
                        continue
                    resp.raise_for_status()
                if resp.status_code == 429:
                    if attempt < self.retry_count:
                        logger.warning("rate limited (attempt %d)", attempt + 1)
                        continue
                    resp.raise_for_status()
                if resp.status_code >= 400:
                    logger.error("LLM error %d: %s", resp.status_code, resp.text[:500])
                    resp.raise_for_status()

                data = resp.json()
                choice = data.get("choices", [{}])[0]
                message = choice.get("message", {})
                content = message.get("content", "")
                raw_tool_calls = message.get("tool_calls")

                if raw_tool_calls:
                    if available_functions is not None:
                        return self._handle_function_calls(
                            raw_tool_calls, messages, tools, available_functions, max_iterations
                        )
                    return _normalize_mcp_tool_arguments(raw_tool_calls)

                if not content and _retry_on_empty and _empty_retry_count < 2:
                    return self.call(
                        messages, tools=tools, callbacks=callbacks,
                        available_functions=available_functions,
                        max_iterations=max_iterations,
                        _retry_on_empty=False,
                        _empty_retry_count=_empty_retry_count + 1,
                    )

                if callbacks:
                    for cb in callbacks:
                        if hasattr(cb, "on_llm_end"):
                            cb.on_llm_end(response=content)

                return content

            except requests.Timeout:
                last_exc = TimeoutError(f"LLM timeout after {self.timeout}s")
                if attempt < self.retry_count:
                    logger.warning("timeout (attempt %d)", attempt + 1)
                    continue
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < self.retry_count:
                    logger.warning("request error (attempt %d): %s", attempt + 1, exc)
                    continue

        raise last_exc or RuntimeError("LLM call failed after all retries")

    def _handle_function_calls(self, tool_calls, messages, tools, available_functions, max_iterations):
        messages = list(messages)
        messages.append({"role": "assistant", "tool_calls": tool_calls})
        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {}
            if fn_name in available_functions:
                result = available_functions[fn_name](**args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": str(result),
                })
            else:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": f"Error: unknown function {fn_name}",
                })
        return self.call(
            messages, tools=tools, available_functions=available_functions,
            max_iterations=max_iterations - 1,
        )

    async def acall(self, *args, **kwargs):
        return await asyncio.to_thread(self.call, *args, **kwargs)
