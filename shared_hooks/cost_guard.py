"""CostGuard —— 实时 token 成本追踪 + 预算硬停。

【双事件挂载】
1. AFTER_TURN：每轮结束累计 token（"算账"）→ 超预算抛 deny
2. BEFORE_TOOL_CALL：调用工具前再查一次预算（防止下一轮 LLM 还没触发就先调工具消费）

【为什么 cost_guard 必须先于 loop_detector】
循环场景是高消耗场景（Agent 重复调用工具最烧钱），偏偏是最需要准确计费的情况。
如果 loop_detector 先触发 deny，cost_guard 永远算不到这一轮的账，
预算严重偏低 —— 1 美元预算可能实际花了 3 美元。
"""

import os
import sys

from xiaopaw.hook_framework.registry import DenyReason, GuardrailDeny

# 阿里云 DashScope 定价（USD per 1M tokens，2026 年汇率粗估）
MODEL_PRICES = {
    "qwen-plus": {"input": 0.80, "output": 2.00},
    "qwen-turbo": {"input": 0.30, "output": 0.60},
    "qwen-max": {"input": 2.40, "output": 9.60},
}

# 未知模型时用一个保守估价（input/output 均高于 plus）—— 宁可高估不要漏算
_DEFAULT_PRICE = {"input": 1.0, "output": 3.0}


class CostGuard:
    def __init__(self, budget_usd: float = 1.0, model: str = "qwen-plus", token_counter=None):
        env_budget = os.environ.get("COST_GUARD_BUDGET")
        if env_budget is not None:
            try:
                budget_usd = float(env_budget)
            except ValueError:
                print(f"[CostGuard] invalid COST_GUARD_BUDGET value: {env_budget!r}, using default", file=sys.stderr)
        if budget_usd < 0:
            raise ValueError("budget_usd must be non-negative")
        self._budget = budget_usd
        self._model = model
        self._token_counter = token_counter
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._estimated_cost = 0.0
        self._deny_count = 0

    def after_turn_handler(self, ctx):
        """AFTER_TURN：累计本轮 token 消费，超预算抛 deny。

        【顺序敏感】cost_guard 必须排在 loop_detector 之前——
        否则循环场景下 loop_detector 先 deny 会让本轮的 token 永远没机会算账。
        """
        self._total_input_tokens += ctx.input_tokens
        self._total_output_tokens += ctx.output_tokens
        self._estimated_cost = self._calculate_cost()
        if self._estimated_cost >= self._budget:
            self._deny_count += 1
            raise GuardrailDeny(
                DenyReason.BUDGET_EXCEEDED,
                f"Budget exceeded: ${self._estimated_cost:.4f} >= ${self._budget:.4f}",
            )

    def before_tool_handler(self, ctx):
        """BEFORE_TOOL_CALL：调用工具前再查一次预算。

        【为什么需要这一道】
        AFTER_TURN 在 step_callback 里触发，但工具调用发生在 LLM step 内部。
        如果只在 AFTER_TURN 检查，可能会出现：
            上一轮已超预算 → 没来得及触发 deny → 本轮 LLM 又调了一个昂贵工具
        所以工具调用前再确认一次累计成本，及时止损。
        """
        if self._estimated_cost >= self._budget:
            self._deny_count += 1
            raise GuardrailDeny(
                DenyReason.BUDGET_EXCEEDED,
                f"Budget exceeded: ${self._estimated_cost:.4f} >= ${self._budget:.4f}",
            )

    def _calculate_cost(self) -> float:
        model = self._model
        prices = MODEL_PRICES.get(model, _DEFAULT_PRICE)
        return (
            self._total_input_tokens * prices["input"] / 1_000_000
            + self._total_output_tokens * prices["output"] / 1_000_000
        )

    def get_metrics(self) -> dict:
        return {
            "total_input_tokens": self._total_input_tokens,
            "total_output_tokens": self._total_output_tokens,
            "estimated_cost_usd": self._estimated_cost,
            "budget_usd": self._budget,
            "deny_count": self._deny_count,
        }
