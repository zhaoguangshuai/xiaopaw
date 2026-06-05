"""SkillLoaderTool —— 渐进式能力披露 + Sub-Crew 触发器。

【渐进式能力披露的价值】
Main Crew 的 prompt 里**只放技能名+描述**（一两行），不放任何技能实现细节。
LLM 看到的"技能清单"始终保持小尺寸，避免被庞大的工具 schema 撑爆 context；
真正的实现复杂度推迟到 Sub-Crew 内部 ——
LLM 通过 skill_loader(skill_name="baidu_search", query=...) 调用，
本工具内部读取 SKILL.md → 构造 Sub-Crew → 在沙箱里执行 → 返回结果。

【两层 Crew 的责任划分】
- Main Crew  ：理解意图、选技能、维护对话上下文
- Sub-Crew   ：在沙箱里执行单一技能（路径隔离 + 工具受限）

═══════════════════════════════════════════════════════════════════════
【ContextVar 是怎么从父 Crew 传到 Sub-Crew 的？】
═══════════════════════════════════════════════════════════════════════

涉及的 ContextVar（每个都是"当前线程私有"的全局变量）：
    主线程视角                          值（举例）
    ─────────────────────────────────────────────────
    _current_adapter   (crew_adapter)   <CrewObservabilityAdapter>
    _trace_id_var      (langfuse_trace) "s-2976e0a09d01"  ← session_id
    _root_span_id_var  (langfuse_trace) "span-session-xxx"
    _span_stack_var    (langfuse_trace) (("span-skill-baidu", "skill_loader", 2, {...}),)
    _gen_id_var        (langfuse_trace) "gen-llm-3"  ← 父线程当前的 LLM 调用
    _gen_count_var     (langfuse_trace) 3
    ...

整体流程（从主线程进入 Sub-Crew 子线程）：

  主线程                                    子线程（ThreadPoolExecutor）
  ─────────                                 ──────────────────────────
  _run() 被调用
    │
    ├─ ① _get_langfuse_parent_span_id()
    │    取栈顶 span_id（即"tool-skill_baidu_search"那个 span）
    │    存进局部变量 parent_span_id
    │
    ├─ ② ctx = contextvars.copy_context()
    │    把【所有】ContextVar 当前值打成一个快照
    │    （副本，不是引用——主线程后续改不会影响 ctx）
    │
    └─ ③ pool.submit(ctx.run, _run_with_cleanup)
         告诉线程池：在子线程里运行 _run_with_cleanup，
         但要先用 ctx 这个快照"激活"所有 ContextVar
                                                 │
                                                 ▼
                                          ④ ctx.run(_run_with_cleanup)
                                             子线程的 ContextVar 全是父快照的副本
                                             ★ adapter / trace_id 此时已经"自动可见"
                                                 │
                                                 ▼
                                          ⑤ _reset_langfuse_contextvars(parent_span_id)
                                             针对 Langfuse ContextVar 做"部分重置"：
                                             - 保留 _trace_id_var（同一棵树）
                                             - _root_span_id_var ← parent_span_id
                                               （让子 trace 挂在父 skill span 之下）
                                             - _gen_id_var/_span_stack_var ← 清零
                                               （子线程从干净状态开始累积）
                                                 │
                                                 ▼
                                          ⑥ Sub-Crew 跑起来
                                             里面的 Hook 调用 langfuse_trace 的
                                             _enqueue() 写 ingestion event 时：
                                             trace_id = 父 trace（自动）
                                             parent_observation_id = parent_span_id
                                                 │
                                                 ▼
                                          ⑦ finally: _flush_langfuse_subcrew()
                                             把子线程 buffer 里的事件推送到 Langfuse
                                             ★ 不重置 ContextVar：见 langfuse_trace.subcrew_cleanup

为什么要"既 copy_context 又部分 reset"？
- copy_context：让 adapter / trace_id 这些"应该共享"的 ContextVar 自动到位
- 部分 reset  ：但 _span_stack_var / _gen_id_var 是"父线程当前正在做什么"的瞬时状态，
                子线程要从空栈、空 gen 重新开始累积，否则会出现：
                · 父线程的 LLM gen 被子线程当成自己的 → 关闭时机错乱
                · 父线程的 span 栈被子线程 push/pop → 主线程后续看到脏栈

为什么不在 _reset 里也清掉 _current_adapter？
- adapter 必须在子线程里依然可见 —— Sub-Crew 的工具调用要 dispatch 同一套 hooks，
  共用同一个 _pending_deny / _turn_count。adapter 本身是线程安全的设计前提，
  共享是故意的。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import re
from pathlib import Path

import yaml
from crewai.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr, field_validator

from xiaopaw.agents.skill_crew import build_skill_crew

logger = logging.getLogger(__name__)


def _get_langfuse_parent_span_id() -> str:
    """★ 步骤①：在主线程里"快照"出当前应该作为 sub-crew 父节点的 span ID。

    必须在 copy_context() 之前调用 —— 因为：
    - 此时主线程的 _span_stack_var 栈顶 = 当前 skill_loader 工具的 span（在 BEFORE_TOOL_CALL
      时由 langfuse_trace.before_tool_handler 压栈）
    - 我们想要把 sub-crew 的所有 observation 挂在这个 span 之下
    - 取出 span_id 存到普通局部变量，跨线程传递不依赖 ContextVar
    """
    try:
        from shared_hooks.langfuse_trace import _root_span_id_var, _span_stack_var

        stack = _span_stack_var.get(())
        if stack:
            # 栈顶元素结构：(span_id, tool_name, turn_number, tool_input)
            return stack[-1][0]
        # 栈空兜底：用 session 根 span 当父节点（不至于挂成孤儿）
        return _root_span_id_var.get("")
    except ImportError:
        return ""


def _reset_langfuse_contextvars(parent_span_id: str = "") -> None:
    """★ 步骤⑤：子线程里对 Langfuse ContextVar 做"选择性重置"。

    在子线程开头调用（此时所有 ContextVar 已经是父线程快照的副本）。

    【为什么不能直接共享父线程状态】
    copy_context() 复制的是"快照"，但 Langfuse 的几个 ContextVar 含义是
    "当前线程正在做什么"——继承父值会出问题：
        _gen_id_var = "gen-父线程-3"  ← 子线程不该认为自己有未关闭的 gen
        _span_stack_var = (父栈)      ← 子线程的 push/pop 会污染主线程视图

    【为什么 _trace_id_var 不重置】
    它代表"这次对话属于哪棵 trace 树"——子 crew 的 observation 必须挂在同一棵树上，
    否则 Langfuse Session 视图里会拆成两条独立 trace。
    子线程不重置它，langfuse_trace.before_tool_handler 写新 span 时会
    自动用这个 trace_id —— 这是 Sub-Crew trace 自动挂父节点的关键。

    【_root_span_id_var 的精细处理】
    parent_span_id 来自步骤①，是"主线程压栈时的 skill_loader span"。
    把子线程的 root 重设为它，新 span 通过 _get_tool_parent_id() 找父节点时，
    在子线程空栈情况下会 fallback 到 _root_span_id_var —— 也就自动挂到了父 skill span 下。
    """
    try:
        from shared_hooks.langfuse_trace import (
            _closed_spans_var,
            _gen_count_var,
            _gen_id_var,
            _root_span_id_var,
            _span_stack_var,
            _tool_count_var,
        )

        # 把"子线程的 root span"改写成父 skill 的 span_id
        # 这样子线程里新建的第一个 span 会自动挂在父 skill 之下
        if parent_span_id:
            _root_span_id_var.set(parent_span_id)
        else:
            # 兜底：parent_span_id 取空时，从父快照的栈顶取
            # （理论上 ① 已经处理过这种情况，这里只是双保险）
            stack = _span_stack_var.get(())
            if stack:
                _root_span_id_var.set(stack[-1][0])

        # 重置"瞬时状态"——子线程从干净的栈/计数开始
        # 注意：这只影响子线程的副本，不影响主线程（ContextVar 写时复制语义）
        _gen_id_var.set("")          # 没有未关闭的 gen
        _gen_count_var.set(0)        # generation 编号重新从 0 数
        _tool_count_var.set(0)       # tool span 编号重新从 0 数
        _span_stack_var.set(())      # 空栈
        _closed_spans_var.set({})    # 已关闭 span 索引清空
    except ImportError:
        pass


def _flush_langfuse_subcrew() -> None:
    """★ 步骤⑦：子线程结束前清理 + flush。

    转调 langfuse_trace.subcrew_cleanup()，关闭子线程内未 close 的 span/gen
    并把 buffer 里累积的事件推送到 Langfuse。
    重要：subcrew_cleanup **不会** 重置 ContextVar——
    因为 ContextVar 是子线程副本，重置无意义；同时父线程从未让出执行权，
    它的 ContextVar 完全独立，不需要"恢复"。
    """
    try:
        from shared_hooks.langfuse_trace import subcrew_cleanup
        subcrew_cleanup()
    except ImportError:
        pass

_SESSION_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"
_SANDBOX_SKILLS_MOUNT = "/mnt/skills"
_CREWAI_VAR_PATTERN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_\-]*)\}")


class SkillLoaderInput(BaseModel):
    skill_name: str = Field(..., description="Skill 名称，必须与可用列表中的 <name> 匹配")
    task_context: str = Field(
        default="",
        description="任务上下文，详细描述需要执行的操作。对于 task 类型的 Skill，建议使用 JSON 格式。",
    )

    @field_validator("task_context", mode="before")
    @classmethod
    def coerce(cls, v):
        if v is None:
            return ""
        if isinstance(v, (dict, list)):
            return json.dumps(v, ensure_ascii=False)
        return str(v)


class SkillLoaderTool(BaseTool):
    name: str = "skill_loader"
    description: str = ""
    args_schema: type = SkillLoaderInput

    _session_id: str = PrivateAttr(default="")
    _sandbox_url: str = PrivateAttr(default="")
    _routing_key: str = PrivateAttr(default="")
    _skill_registry: dict = PrivateAttr(default_factory=dict)
    _instruction_cache: dict = PrivateAttr(default_factory=dict)
    _history_all: list = PrivateAttr(default_factory=list)

    def __init__(
        self,
        session_id: str = "",
        sandbox_url: str = "",
        routing_key: str = "",
        history_all: list | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        if session_id and not _SESSION_ID_PATTERN.match(session_id):
            raise ValueError(f"Invalid session_id: {session_id!r}")
        self._session_id = session_id
        self._sandbox_url = sandbox_url
        self._routing_key = routing_key
        self._history_all = history_all or []
        self._skill_registry = {}
        self._instruction_cache = {}
        self._build_description()

    def _build_description(self) -> None:
        manifest_path = _SKILLS_DIR / "load_skills.yaml"
        if not manifest_path.exists():
            self.description = "No skills available."
            return

        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        skills_xml: list[str] = []

        for skill_name, skill_cfg in manifest.items():
            if not skill_cfg.get("enabled", True):
                continue
            skill_path = skill_cfg.get("path", skill_name)
            if ".." in skill_path:
                logger.warning("path traversal blocked in skill: %s", skill_name)
                continue

            skill_dir = _SKILLS_DIR / skill_path
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue

            desc = self._extract_frontmatter_description(
                skill_md.read_text(encoding="utf-8")
            )
            skill_type = skill_cfg.get("type", "reference")

            self._skill_registry[skill_name] = {
                "type": skill_type,
                "path": skill_path,
                "dir": skill_dir,
            }

            skills_xml.append(
                f"  <skill>\n"
                f"    <name>{skill_name}</name>\n"
                f"    <type>{skill_type}</type>\n"
                f"    <description>{desc}</description>\n"
                f"  </skill>"
            )

        session_dir = f"/workspace/sessions/{self._session_id}" if self._session_id else "/workspace"
        header = (
            f"加载并调用 Skill。会话目录: {session_dir}\n"
            f"上传文件: {session_dir}/uploads/\n"
            f"输出文件: {session_dir}/outputs/\n\n"
            f"[重要] 下方 <name> 标签内容是 skill_name 参数值，不是工具名称。\n"
            f"正确调用方式：skill_loader(skill_name=\"baidu_search\", task_context=\"...\")\n"
            f"错误做法：直接以 baidu_search 为工具名调用（会报 Tool not found）\n\n"
        )
        self.description = (
            header
            + "<available_skills>\n"
            + "\n".join(skills_xml)
            + "\n</available_skills>"
        )

    def _extract_frontmatter_description(self, content: str) -> str:
        match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        if not match:
            return content[:200]
        try:
            fm = yaml.safe_load(match.group(1))
            return str(fm.get("description", ""))[:200]
        except yaml.YAMLError:
            return content[:200]

    def _get_skill_instructions(self, skill_name: str) -> str:
        if skill_name in self._instruction_cache:
            return self._instruction_cache[skill_name]

        info = self._skill_registry[skill_name]
        skill_md = info["dir"] / "SKILL.md"
        raw = skill_md.read_text(encoding="utf-8")
        instructions = re.sub(r"^---\n.*?\n---\n?", "", raw, count=1, flags=re.DOTALL)

        session_dir = f"/workspace/sessions/{self._session_id}" if self._session_id else "/workspace"
        if self._sandbox_url:
            skill_base = f"{_SANDBOX_SKILLS_MOUNT}/{info['path']}"
        else:
            skill_base = str(info["dir"])
        instructions = instructions.replace("{skill_base}", skill_base)
        instructions = instructions.replace("{_skill_base}", skill_base)
        instructions = instructions.replace("{session_id}", self._session_id)
        instructions = instructions.replace("{session_dir}", session_dir)

        # Escape remaining braces to prevent CrewAI template errors
        def _escape_unresolved(text: str) -> str:
            return _CREWAI_VAR_PATTERN.sub(
                lambda m: "{{" + m.group(1) + "}}", text
            )

        instructions = _escape_unresolved(instructions)

        sandbox_directive = (
            f"\n\n<sandbox_execution_directive>\n"
            f"会话目录: {session_dir}\n"
            f"技能脚本目录: {skill_base}\n"
            f"routing_key: {self._routing_key}\n"
            f"执行脚本前请先 cd {skill_base}\n"
            f"</sandbox_execution_directive>"
        )
        instructions += sandbox_directive

        self._instruction_cache[skill_name] = instructions
        return instructions

    def _handle_history_reader(self, task_context: str) -> str:
        try:
            params = json.loads(task_context) if task_context else {}
        except json.JSONDecodeError:
            params = {}

        page = max(1, int(params.get("page", 1)))
        page_size = min(50, max(1, int(params.get("page_size", 20))))
        total = len(self._history_all)
        total_pages = max(1, (total + page_size - 1) // page_size)
        start = (page - 1) * page_size
        end = start + page_size
        messages = [
            {"role": m.role, "content": m.content, "ts": m.ts}
            for m in self._history_all[start:end]
        ]
        return json.dumps(
            {
                "errcode": 0,
                "message": "ok",
                "data": {
                    "messages": messages,
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                    "total_pages": total_pages,
                },
            },
            ensure_ascii=False,
        )

    async def _execute_skill_async(
        self, skill_name: str, task_context: str
    ) -> str:
        if skill_name == "history_reader":
            return self._handle_history_reader(task_context)

        info = self._skill_registry[skill_name]
        instructions = self._get_skill_instructions(skill_name)

        if info["type"] == "reference":
            return f"<skill_instructions>\n{instructions}\n</skill_instructions>"

        crew = build_skill_crew(
            skill_name=skill_name,
            skill_instructions=instructions,
            session_id=self._session_id,
            sandbox_mcp_url=self._sandbox_url,
        )

        inputs = {"task_context": task_context, "skill_name": skill_name}
        for m in _CREWAI_VAR_PATTERN.finditer(instructions):
            var = m.group(1)
            if var not in inputs:
                inputs[var] = var

        try:
            result = await crew.akickoff(inputs=inputs)
            return str(result)
        finally:
            hook = getattr(crew, "_subcrew_tool_hook", None)
            if hook is not None:
                try:
                    from crewai.hooks import unregister_before_tool_call_hook
                    unregister_before_tool_call_hook(hook)
                except (ValueError, AttributeError):
                    pass
            for agent in crew.agents:
                for mcp in getattr(agent, "mcps", []) or []:
                    try:
                        if hasattr(mcp, "stop") and callable(mcp.stop):
                            await asyncio.wait_for(mcp.stop(), timeout=10.0)
                    except asyncio.TimeoutError:
                        logger.warning(
                            "MCP graceful stop timed out after 10s for %s, "
                            "deferring to event loop cleanup",
                            skill_name,
                        )
                    except Exception as exc:
                        logger.warning("MCP cleanup error (non-fatal): %s", exc)

    async def _arun(self, skill_name: str, task_context: str = "") -> str:
        if skill_name not in self._skill_registry and skill_name != "history_reader":
            available = ", ".join(sorted(self._skill_registry.keys()))
            return (
                f"错误：未找到 Skill '{skill_name}'。\n"
                f"可用 Skills: {available}"
            )
        return await self._execute_skill_async(skill_name, task_context)

    def _run(self, skill_name: str, task_context: str = "") -> str:
        """★ 同步入口 —— ContextVar 跨线程传递的核心枢纽 ★

        CrewAI 在主线程同步调用 BaseTool._run()，但 Sub-Crew 内部用 asyncio +
        会调用阻塞 IO（沙箱 MCP），不能直接在主线程跑。所以本方法把 Sub-Crew 整体
        塞到一个独立子线程 + 独立 event loop 里执行，并通过 copy_context() 桥接 ContextVar。

        【为什么用 ThreadPoolExecutor 而不是 asyncio.to_thread】
        到这里的调用栈已经在 CrewAI 自己的 event loop 中（akickoff），
        在已有 loop 里 run_until_complete 另一个协程会冲突。
        所以另起一个完全独立的子线程，子线程内自建 event loop，互不干扰。
        """
        if skill_name not in self._skill_registry and skill_name != "history_reader":
            available = ", ".join(sorted(self._skill_registry.keys()))
            return (
                f"错误：未找到 Skill '{skill_name}'。\n"
                f"可用 Skills: {available}"
            )

        import contextvars

        # ───── 步骤①：快照父线程的 Langfuse 父 span ID ─────
        # 必须在 copy_context() 之前取——此时主线程栈顶是当前 skill_loader 自己的 span
        parent_span_id = _get_langfuse_parent_span_id()

        # ───── 步骤②：copy_context() 把所有 ContextVar 当前值打成快照 ─────
        # 注意 copy_context() 复制的是"键值对的浅拷贝"，对值是引用：
        #   - adapter 对象：引用共享（这正是我们想要的，sub-crew 用同一个 adapter）
        #   - trace_id 字符串：引用共享（不可变，安全）
        #   - _span_stack_var 元组：引用共享（不可变，安全）
        # 子线程后续通过 .set() 修改 ContextVar，是写入子线程私有的副本表，
        # 不会影响主线程的 ContextVar 视图——这就是"copy-on-write"语义。
        ctx = contextvars.copy_context()

        def _run_with_cleanup():
            """在子线程里执行的闭包。
            被 ctx.run() 包裹后，所有 ContextVar 读写都作用在 ctx 这个副本上。
            """
            # 子线程独立的 event loop（不和主线程的 loop 冲突）
            loop = asyncio.new_event_loop()

            # ───── 步骤⑤：选择性重置 Langfuse ContextVar ─────
            # 此时已经在 ctx.run() 内部，写入只影响子线程副本
            _reset_langfuse_contextvars(parent_span_id)
            try:
                # ───── 步骤⑥：真正执行 Sub-Crew ─────
                # _execute_skill_async 内部调 build_skill_crew + crew.akickoff
                # Sub-Crew 里所有 hook 触发的 langfuse_trace 调用，
                # 读到的都是步骤⑤ reset 之后的"干净"ContextVar：
                #   - trace_id 仍是父 trace（同一棵树）
                #   - root_span_id = 父 skill span（自动挂父）
                #   - span_stack 空（从零累积）
                return loop.run_until_complete(
                    self._execute_skill_async(skill_name, task_context)
                )
            finally:
                # ───── 步骤⑦：清理 + flush ─────
                # 关闭子线程内未 close 的 span/gen，把 buffer 推送到 Langfuse
                _flush_langfuse_subcrew()

                # 取消子 loop 里残留的 task（避免 loop.close 警告）
                pending = asyncio.all_tasks(loop)
                for t in pending:
                    t.cancel()
                if pending:
                    try:
                        loop.run_until_complete(
                            asyncio.wait_for(
                                asyncio.gather(*pending, return_exceptions=True),
                                timeout=15.0,
                            )
                        )
                    except (asyncio.TimeoutError, Exception):
                        logger.warning("Event loop cleanup timed out, forcing close")
                loop.close()

        # ───── 步骤③：把闭包提交到独立线程，用 ctx.run 桥接 ContextVar ─────
        # ctx.run(fn) 的语义：在调用 fn 前激活 ctx 这个 ContextVar 表，
        # fn 内部所有 ContextVar 读写都作用在副本上，fn 返回后副本被丢弃。
        # 所以子线程里 fn 看到的 ContextVar = 主线程在步骤②那一刻的快照。
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(ctx.run, _run_with_cleanup)
            # 5 分钟超时——Sub-Crew 在沙箱里跑长任务的兜底
            return future.result(timeout=300)
