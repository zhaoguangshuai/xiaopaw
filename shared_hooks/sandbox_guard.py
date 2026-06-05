"""SandboxGuard —— 确定性输入消毒（不依赖 LLM）。

【核心思想：Prompt is advice, Hook is law】
soul.md 里写"NEVER 执行 shell 命令"对 LLM 来说是"建议"——LLM 在任务压力下会违规。
SandboxGuard 用 4 组硬编码正则在 BEFORE_TOOL_CALL 兜底拦截，命中即抛 GuardrailDeny。

【挂载事件】BEFORE_TOOL_CALL（fail_closed=True）

【检测项】
1. 路径穿越：../  ..\
2. 危险命令：rm -rf, sudo, chmod 777, curl|sh, eval(), exec() ...
3. Shell 注入：; | && $( ` （沙箱原生工具豁免）
4. Prompt 注入：[SYSTEM]、忽略以上指令、ignore previous instructions ...

【输入预处理】
NFKC Unicode 归一化 + 最多 3 轮 URL 解码 + null byte 拦截。
这是为了防止攻击者用 %2E%2E%2F 这种编码绕过正则。
"""

import re
import sys
import unicodedata
from collections import deque
from urllib.parse import unquote

from xiaopaw.hook_framework.registry import DenyReason, GuardrailDeny

# ── 4 组检测正则 ──────────────────────────────────────────────
# 灵感来源：Claude Code 的 cyberRiskInstruction.ts —— 在工业实战里被反复打磨的清单

# 路径穿越：匹配 ../ 或 ..\
_PATH_TRAVERSAL = re.compile(r"\.\.[/\\]")

# 危险命令：删除/提权/管道执行/动态执行/磁盘操作
_DANGEROUS_COMMANDS = re.compile(
    r"\b(rm\s+-rf|sudo\b|chmod\s+777|curl\s.*\|\s*sh|eval\s*\(|exec\s*\(|"
    r"dd\s+if=|mkfs\b|shred\b|doas\b|pkexec\b|su\s+)",
    re.IGNORECASE,
)

# Shell 注入：分号、管道、AND 链、命令替换、子命令
_SHELL_INJECTION = re.compile(r"[;|]|&&|`|\$\(")

# 环境变量引用：$VAR / ${VAR}（仅告警不拦截，因为合法用例多）
_ENV_VAR = re.compile(r"\$\{?\w+\}?")

# Prompt 注入：role 标签、控制 token、忽略指令的中英文表达
_PROMPT_INJECTION = re.compile(
    r"\[(SYSTEM|INST|/INST)\]|"
    r"<\|?(system|im_start|im_end)\|?>|"
    r"忽略(之前|以上|上面|所有)(的)?(所有)?指令|"
    r"ignore\s+(previous|all|above)\s+instructions",
    re.IGNORECASE,
)

# 沙箱原生工具的豁免标记：sandbox_xxx / mcp_xxx 这类工具运行在隔离容器里，
# 它们的输入里出现 ; | 之类是合法的 shell 命令（如 git 命令组合），不应误拦
_SANDBOX_TOOL_MARKER = re.compile(r"sandbox_|mcp_")


def _normalize(raw: str) -> str:
    """输入预处理：NFKC + 多轮 URL 解码 + null byte 检测。

    【为什么要做这一步】
    攻击者会用编码绕过正则。比如 ../ 可以编码为：
        %2E%2E%2F        （URL 编码一次）
        %252E%252E%252F  （URL 编码两次，绕过单次解码）
        ．．／            （Unicode 全角字符）
    所以先归一化、迭代解码，再扔给正则匹配。

    【为什么最多 3 轮】
    实战中 3 轮已能覆盖绝大多数嵌套编码，再多就性能浪费。
    """
    # NFKC 把全角/兼容字符归一化为标准形式（'．' → '.', 'Ｆｕｌｌ' → 'Full'）
    normalized = unicodedata.normalize("NFKC", raw)
    # 多轮 URL 解码：直到稳定或达到 3 轮上限
    prev = normalized
    for _ in range(3):
        decoded = unquote(prev)
        if decoded == prev:
            break
        prev = decoded
    # null byte（\x00）会让某些 C 库提前截断字符串，导致后续校验被绕过
    if "\x00" in prev:
        raise GuardrailDeny(DenyReason.SANDBOX_VIOLATION, "Null byte in input")
    return prev


class SandboxGuard:
    """输入消毒策略 —— 命中即抛 GuardrailDeny。

    【deps 共享 audit_logger】
    构造参数 audit 由 HookLoader 通过 deps 注入，
    与 PermissionGate 共享同一个 SecurityAuditLogger 实例，
    所有违规事件会写到同一个 security_audit.jsonl 文件，便于事后分析。
    """

    _MAX_VIOLATIONS = 1000  # 内存里只保留最近 1000 条违规记录（避免长 session 内存膨胀）

    def __init__(self, audit=None):
        self._audit = audit
        # deque(maxlen=N)：达到上限自动淘汰最早的元素
        self._violations: deque[dict] = deque(maxlen=self._MAX_VIOLATIONS)

    def before_tool_handler(self, ctx):
        """BEFORE_TOOL_CALL 入口 —— 4 组检测短路求值。

        【检测顺序】路径穿越 → 危险命令 → Shell 注入 → 环境变量（仅告警）→ Prompt 注入
        命中前面的就直接抛，不会到后面——dispatch_gate 见 deny 立即中止整条链路。
        """
        # 把所有参数值拼成一个字符串做整体扫描——攻击 payload 可能藏在任意字段里
        raw = " ".join(str(v) for v in ctx.tool_input.values()) if ctx.tool_input else ""
        if not raw:
            return

        text = _normalize(raw)

        if _PATH_TRAVERSAL.search(text):
            self._record("path_traversal", ctx.tool_name, text)
            raise GuardrailDeny(DenyReason.SANDBOX_VIOLATION, "Path traversal detected")

        if _DANGEROUS_COMMANDS.search(text):
            self._record("dangerous_command", ctx.tool_name, text)
            raise GuardrailDeny(DenyReason.SANDBOX_VIOLATION, "Dangerous command detected")

        # 沙箱原生工具豁免：sandbox_xxx / mcp_xxx 在隔离容器里跑 shell 是合法的
        if not _SANDBOX_TOOL_MARKER.search(ctx.tool_name) and _SHELL_INJECTION.search(text):
            self._record("shell_injection", ctx.tool_name, text)
            raise GuardrailDeny(DenyReason.SANDBOX_VIOLATION, "Shell injection detected")

        # 环境变量引用只告警不拦截（合法用例：用户让 Agent 读取配置）
        if _ENV_VAR.search(text):
            print(
                f"[SandboxGuard] WARNING: environment variable reference in input: {text[:100]}",
                file=sys.stderr,
            )

        # Prompt 注入用 PROMPT_INJECTION 这个原因码（与沙箱违规区分，便于审计归类）
        if _PROMPT_INJECTION.search(text):
            self._record("prompt_injection", ctx.tool_name, text)
            raise GuardrailDeny(DenyReason.PROMPT_INJECTION, "Prompt injection detected")

    def _record(self, violation_type: str, tool_name: str, text: str):
        self._violations.append({
            "type": violation_type,
            "tool": tool_name,
            "input_preview": text[:200],
        })
        if self._audit:
            self._audit.record_event(
                f"sandbox_{violation_type}",
                tool=tool_name,
                input_preview=text[:200],
            )

    def get_metrics(self) -> dict:
        violations_by_type: dict[str, int] = {}
        for v in self._violations:
            violations_by_type[v["type"]] = violations_by_type.get(v["type"], 0) + 1
        return {
            "total_violations": len(self._violations),
            "violations_by_type": violations_by_type,
        }
