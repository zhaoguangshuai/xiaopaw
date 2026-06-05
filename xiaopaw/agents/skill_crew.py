"""Sub-Crew —— Skill 在沙箱中的执行单元。

【为什么是"零编排"】
传统 Agent 框架要求显式声明 Workflow（A → B → C）。
零编排：每个 Skill 自带 SKILL.md 描述自己（agent role + task）；
Main Crew 调 SkillLoader → SkillLoader 读 SKILL.md → 临时构造 Sub-Crew → 执行 → 返回结果。
没有任何中央编排者，能力是"声明"出来的而不是"编排"出来的。

【与 Hook 框架的协同】
Sub-Crew 在子线程跑，但 ContextVar（adapter / trace_id / span 栈）由 copy_context() 自动继承。
所以 Sub-Crew 的 LLM/工具调用会自动挂在父线程"tool-skill_xxx" span 之下，无需显式传 parent_id。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml
from crewai import Agent, Crew, Process, Task
from crewai.hooks import ToolCallHookContext, before_tool_call, unregister_before_tool_call_hook
# 必须用 MCPServerHTTP（Streamable HTTP），不是 MCPServerSSE。
# AIO-Sandbox 的 /mcp 端点是 Streamable HTTP（POST + 可选 SSE 升级）。
# 用 MCPServerSSE 会发 GET /mcp 期望持续事件流，sandbox 几秒后关连接，
# CrewAI MCP 适配器卡在 _resolve_native 等 tools/list 响应 → 测试 5min 超时。
# 症状参考 README → "常见坑 FAQ" 第 2 条。
from crewai.mcp import MCPServerHTTP

from xiaopaw.llm.aliyun_llm import AliyunLLM

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).parent / "config"
_DEFAULT_SANDBOX_MCP_URL = "http://localhost:8030/mcp"

_STRING_CONTENT_FIELDS = {"content", "file_text", "new_str"}


def _normalize_subcrew_tool_input(tool_input: dict) -> None:
    """Convert dict values to JSON strings for MCP file-write tools.

    The sub-crew LLM sometimes passes a dict where a string is expected
    (e.g. file_operations content, str_replace_editor file_text). Pydantic
    rejects non-string values → repeated retries burn the time budget.
    """
    for field in _STRING_CONTENT_FIELDS:
        val = tool_input.get(field)
        if isinstance(val, (dict, list)):
            tool_input[field] = json.dumps(val, ensure_ascii=False)


def _format_cfg(cfg: dict, **kwargs) -> dict:
    result = {}
    for k, v in cfg.items():
        if isinstance(v, str):
            result[k] = v.format(**kwargs)
        else:
            result[k] = v
    return result


def _make_subcrew_step_callback() -> Callable[[Any], Awaitable[None]]:
    """Step callback for sub-crew: fires AFTER_TOOL_CALL + AFTER_TURN events.

    Unlike the main crew's step_callback, this omits sender.send_thinking
    since the sub-crew has no direct channel to the user.
    """
    from crewai.agents.parser import AgentAction, AgentFinish
    from xiaopaw.hook_framework.crew_adapter import get_current_adapter

    async def _callback(step_output: Any) -> None:
        adapter = get_current_adapter()
        if not adapter:
            return

        step_text = ""
        if isinstance(step_output, AgentAction):
            step_text = str(step_output.text or step_output.thought or "")
        elif isinstance(step_output, AgentFinish):
            step_text = str(getattr(step_output, "output", "") or "")
        adapter.dispatch_after_turn(output=step_text[:2000])

        if adapter._pending_deny:
            pending = adapter._pending_deny
            adapter._pending_deny = None
            raise pending

    return _callback


def build_skill_crew(
    skill_name: str,
    skill_instructions: str,
    session_id: str = "",
    sandbox_mcp_url: str = _DEFAULT_SANDBOX_MCP_URL,
    sub_agent_model: str = "qwen3-max",
    max_iter: int = 20,
    allowed_tools: list[str] | None = None,
) -> Crew:
    if not sandbox_mcp_url or not sandbox_mcp_url.startswith(("http://", "https://")):
        raise ValueError(
            f"build_skill_crew: sandbox_mcp_url must be an http(s) URL, got "
            f"{sandbox_mcp_url!r}. Empty or malformed URLs cause httpx.UnsupportedProtocol "
            f"deep inside Sub-Crew, which manifests as a 5-minute TestAPI timeout. "
            f"Pass a valid URL (e.g. http://localhost:8030/mcp) or skip skill execution."
        )
    sandbox_mcp = MCPServerHTTP(url=sandbox_mcp_url)
    skill_llm = AliyunLLM(model=sub_agent_model, region="cn", temperature=0.3)

    session_dir = f"/workspace/sessions/{session_id}" if session_id else "/workspace"

    agents_cfg = yaml.safe_load((_CONFIG_DIR / "agents.yaml").read_text(encoding="utf-8"))
    tasks_cfg = yaml.safe_load((_CONFIG_DIR / "tasks.yaml").read_text(encoding="utf-8"))

    agent_cfg = _format_cfg(
        agents_cfg["skill_agent"],
        skill_name=skill_name,
        skill_name_upper=skill_name.upper(),
        session_dir=session_dir,
        skill_instructions=skill_instructions,
    )
    agent_cfg["max_iter"] = max_iter

    skill_agent = Agent(
        **agent_cfg,
        llm=skill_llm,
        mcps=[sandbox_mcp],
        verbose=True,
    )

    task_cfg = _format_cfg(tasks_cfg["skill_task"], session_dir=session_dir)
    skill_task = Task(**task_cfg, agent=skill_agent)

    @before_tool_call
    def _subcrew_tool_hook(context: ToolCallHookContext) -> bool | None:
        _normalize_subcrew_tool_input(context.tool_input)
        return None

    crew = Crew(
        agents=[skill_agent],
        tasks=[skill_task],
        process=Process.sequential,
        verbose=True,
        step_callback=_make_subcrew_step_callback(),
    )
    crew._subcrew_tool_hook = _subcrew_tool_hook  # keep ref for unregister
    return crew
