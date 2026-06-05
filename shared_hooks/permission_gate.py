"""PermissionGate —— 工具权限网关（Deny > Warn > Allow）。

【三级权限模型】
- deny  ：抛 GuardrailDeny，直接拦截
- warn  ：放行 + 写审计日志（"调用了，但记录在案"）
- allow ：静默放行

【Default-Deny 原则】
未在 security.yaml 中显式声明的工具走 default。
**default 应该设成 warn 或 deny**，绝不能默认 allow——
新工具上线时如果忘配权限，应该走人工审核而不是默认放行。

【与 sandbox_guard 的协作】
sandbox_guard 检查"输入有没有问题"（路径穿越、注入），
permission_gate 检查"这个调用方有没有权限调这个工具"（按 tool_name + 后续可扩展按 sender_id）。
两者都挂在 BEFORE_TOOL_CALL，sandbox_guard 在前——先把恶意输入的拦下，
省得为非法请求做权限计算。
"""

from collections import deque
from pathlib import Path

import yaml

from xiaopaw.hook_framework.registry import DenyReason, GuardrailDeny


class PermissionGate:
    def __init__(self, tools: dict[str, str] | None = None, default: str = "warn", audit=None):
        self._tool_permissions: dict[str, str] = {
            k.lower(): v.lower() for k, v in (tools or {}).items()
        }
        self._default = default.lower()
        self._audit = audit
        self.decisions: deque[dict] = deque(maxlen=10000)

    @classmethod
    def from_yaml(cls, path: Path, audit=None):
        try:
            with open(path) as f:
                config = yaml.safe_load(f)
        except (OSError, yaml.YAMLError) as e:
            raise ValueError(f"Failed to load permission config from {path}: {e}") from e
        if not isinstance(config, dict):
            raise ValueError(f"Permission config must be a YAML dict, got {type(config).__name__}")
        perms = config.get("permissions", {})
        return cls(
            tools=perms.get("tools", {}),
            default=perms.get("default", "warn"),
            audit=audit,
        )

    def before_tool_handler(self, ctx):
        """BEFORE_TOOL_CALL 入口 —— 查权限矩阵 + 记录决策。

        【policy_source 字段的意义】
        审计日志里区分 "explicit"（显式声明）和 "default"（走默认）很重要——
        新工具误配只会留下 "default" 痕迹，便于事后排查"为什么这工具被允许调用"。
        """
        tool = ctx.tool_name.lower()
        # 显式声明优先，否则走 default
        permission = self._tool_permissions.get(tool, self._default)
        policy_source = "explicit" if tool in self._tool_permissions else "default"

        # 每次决策都记一笔，便于后续 get_metrics() 统计 allow/warn/deny 比例
        decision = {
            "tool": ctx.tool_name,
            "permission": permission,
            "policy_source": policy_source,
        }
        self.decisions.append(decision)

        if permission == "deny":
            if self._audit:
                self._audit.record_event(
                    "permission_deny", tool=ctx.tool_name
                )
            raise GuardrailDeny(
                DenyReason.PERMISSION_DENIED,
                f"Permission denied for tool: {ctx.tool_name}",
            )

        # warn：放行但留痕——用户确实调用了，需要事后追溯
        if permission == "warn":
            if self._audit:
                self._audit.record_event(
                    "permission_warn", tool=ctx.tool_name
                )
        # allow：静默放行，不写日志（避免日志膨胀）

    def get_metrics(self) -> dict:
        allow_count = sum(1 for d in self.decisions if d["permission"] == "allow")
        warn_count = sum(1 for d in self.decisions if d["permission"] == "warn")
        deny_count = sum(1 for d in self.decisions if d["permission"] == "deny")
        denied_tools = [d["tool"] for d in self.decisions if d["permission"] == "deny"]
        return {
            "total_decisions": len(self.decisions),
            "allow_count": allow_count,
            "warn_count": warn_count,
            "deny_count": deny_count,
            "denied_tools": denied_tools,
        }
