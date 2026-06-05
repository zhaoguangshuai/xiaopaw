"""SecurityAuditLogger —— append-only JSONL 安全审计日志。

【设计要点】
1. **append-only JSONL**：每行一个 JSON 事件，绝不修改/删除已写入的行
   →文件可以被外部 SIEM 系统直接 tail 消费，无需复杂 schema
2. **被多策略共享**：sandbox_guard 和 permission_gate 通过 deps: { audit: audit_logger } 注入同一实例
   →所有安全事件集中在一个文件，便于事后关联分析
3. **SESSION_END 写摘要**：每次会话结束追加一行 session_summary，把这次会话的事件数按类型聚合

【为什么必须排在 strategies 段第一位】
HookLoader 按 yaml 列表顺序实例化。
如果 audit_logger 排在 sandbox_guard 后面 → SandboxGuard.__init__(audit=None) →
运行时 self._audit.record_event() 抛 AttributeError → 因为 fail_closed=True →
变成 GuardrailDeny → 所有请求被拒绝，系统瘫痪。
"""

import json
import os
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path


class SecurityAuditLogger:
    def __init__(self, audit_file: str | Path | None = None):
        env_path = os.environ.get("SECURITY_AUDIT_FILE")
        if env_path:
            self._audit_file = Path(env_path)
        elif audit_file:
            self._audit_file = Path(audit_file)
        else:
            self._audit_file = None
        self._events: deque[dict] = deque(maxlen=10000)

    def record_event(self, security_event: str, **kwargs):
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "security_event": security_event,
            **kwargs,
        }
        self._events.append(entry)
        self._write(entry)

    def session_end_handler(self, ctx):
        """SESSION_END 时写本会话的安全摘要。

        【为什么需要会话级摘要】
        逐条事件用于精确审计，但巡检时更关心宏观趋势：
            "今天哪些 session 有安全事件？哪类事件最多？"
        摘要行让运维只需 grep "session_summary" 就能拿到分类统计。
        """
        events_by_type: dict[str, int] = {}
        for e in self._events:
            t = e["security_event"]
            events_by_type[t] = events_by_type.get(t, 0) + 1

        summary = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "security_event": "session_summary",
            "session_id": ctx.session_id,
            "total_security_events": len(self._events),
            "events_by_type": events_by_type,
        }
        self._write(summary)

    def _write(self, entry: dict):
        if self._audit_file is None:
            return
        try:
            with open(self._audit_file, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            print(f"[SecurityAuditLogger] write error: {e}", file=sys.stderr)

    def get_metrics(self) -> dict:
        events_by_type: dict[str, int] = {}
        for e in self._events:
            t = e["security_event"]
            events_by_type[t] = events_by_type.get(t, 0) + 1
        return {
            "total_security_events": len(self._events),
            "events_by_type": events_by_type,
        }
