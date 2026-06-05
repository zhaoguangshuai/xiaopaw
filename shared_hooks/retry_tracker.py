"""RetryTracker —— 纯观测策略，只追踪不阻断。

【设计理念：观测先于策略】
RetryTracker 故意只打 WARNING 不抛 deny——
它的价值是给运维提供"哪些工具最不稳定"的统计，
而不是替业务做决策。生产环境中工具偶发失败重试是正常现象，
盲目 deny 会破坏正常用户体验。

【挂载事件】AFTER_TOOL_CALL（dispatch_gate，但不会抛 deny）

【核心指标】retry_success_rate
连续失败后又成功的次数 / 总重试次数 = 工具稳定性的近似指标。
如果某工具 success_rate < 30%，说明它本身有问题，需要排查。
"""

import sys


class RetryTracker:
    def __init__(self, max_retries: int = 5):
        # max_retries 是 WARNING 阈值，不是阻断阈值
        self._max_retries = max_retries
        # tool_name → 当前连续失败次数（成功后清零）
        self._failures: dict[str, int] = {}
        self._total_retries = 0
        self._successful_retries = 0

    def after_tool_handler(self, ctx):
        """每次工具调用后更新失败计数。

        【状态机】
        - success=False：累加失败计数；如果之前已经失败过，记一次"重试事件"
        - success=True ：如果之前有失败，记一次"成功重试"；清零计数
        """
        if not ctx.tool_name:
            return

        if not ctx.success:
            prev = self._failures.get(ctx.tool_name, 0)
            self._failures[ctx.tool_name] = prev + 1
            # prev > 0 说明这是一次"重试"（不是首次调用）
            if prev > 0:
                self._total_retries += 1
            # 达到阈值只打 WARNING，不抛 deny ——观测策略的本分
            if self._failures[ctx.tool_name] >= self._max_retries:
                print(
                    f"[RetryTracker] WARNING: {ctx.tool_name} failed {self._failures[ctx.tool_name]} times consecutively",
                    file=sys.stderr,
                )
        else:
            # 之前失败过现在成功了 → 这是一次成功的重试
            if self._failures.get(ctx.tool_name, 0) > 0:
                self._successful_retries += 1
            # 清零：成功后下一次失败重新从 0 开始计数
            self._failures[ctx.tool_name] = 0

    def get_metrics(self) -> dict:
        active = {k: v for k, v in self._failures.items() if v > 0}
        rate = self._successful_retries / max(self._total_retries, 1) if self._total_retries > 0 else 0.0
        return {
            "active_failures": active,
            "total_retries": self._total_retries,
            "successful_retries": self._successful_retries,
            "retry_success_rate": rate,
        }
