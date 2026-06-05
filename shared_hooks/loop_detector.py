"""LoopDetector —— 通过状态哈希去重检测循环。

【核心思想】
Agent 卡死时的典型特征：连续 N 次工具调用返回相同结果，或连续 N 轮 LLM 输出相同。
对状态做 MD5 哈希，如果连续 threshold（默认 3）次哈希值完全相同 → 判定循环 → deny。

【两个维度的循环都要检测】
- tool_loop：工具维度（同一个工具反复返回同样结果——比如搜索一直返回空）
- turn_loop：对话维度（Agent 反复输出同样的中间思考）

【为什么不用计数器】
如果 Agent 真的需要 5 次调用同一工具完成任务（不同参数），用计数器会误杀。
哈希去重要求"状态完全一致"，留出了正常的循环空间。

【与 cost_guard 的顺序】
loop_detector 在 cost_guard 之后——
循环场景下 cost_guard 先算账，再让 loop_detector 决定是否阻断。
"""

import hashlib

from xiaopaw.hook_framework.registry import DenyReason, GuardrailDeny


class LoopDetector:
    def __init__(self, threshold: int = 3):
        self._threshold = threshold
        # 用 list 而非 deque：需要切片 hashes[-threshold:]
        self._tool_hashes: list[str] = []
        self._turn_hashes: list[str] = []
        self._loop_detections = 0
        self._total_tool_calls = 0
        self._total_turns = 0

    def after_tool_handler(self, ctx):
        """AFTER_TOOL_CALL：检查工具维度循环。

        状态 = tool_name + tool_output。同一个工具不同输出不算循环。
        """
        self._total_tool_calls += 1
        state = f"{ctx.tool_name}:{ctx.metadata.get('tool_output', '')}"
        self._check_loop(self._tool_hashes, state, "tool_loop")

    def after_turn_handler(self, ctx):
        """AFTER_TURN：检查对话维度循环。

        状态 = LLM 输出文本。Agent 反复说一样的话就判定循环。
        """
        self._total_turns += 1
        state = ctx.metadata.get("output", "")
        self._check_loop(self._turn_hashes, state, "turn_loop")

    def _check_loop(self, hashes: list[str], state: str, detection_type: str):
        # 只取 16 位 MD5 前缀——足够区分，省内存
        # 用 MD5 不是为了密码学安全，是因为它快且分布均匀
        h = hashlib.md5(state.encode()).hexdigest()[:16]
        hashes.append(h)
        # 截断历史：只保留最近 2*threshold 条，避免长 session 内存膨胀
        if len(hashes) > self._threshold * 2:
            del hashes[: len(hashes) - self._threshold * 2]
        # 检测最近 threshold 个哈希是否完全相同
        if len(hashes) >= self._threshold:
            recent = hashes[-self._threshold :]
            if len(set(recent)) == 1:
                self._loop_detections += 1
                raise GuardrailDeny(
                    DenyReason.LOOP_DETECTED,
                    f"Loop detected ({detection_type}): identical state repeated {self._threshold} times",
                )

    def get_metrics(self) -> dict:
        unique_tool = len(set(self._tool_hashes))
        unique_turn = len(set(self._turn_hashes))
        return {
            "total_turns": self._total_turns,
            "total_tool_calls": self._total_tool_calls,
            "unique_tool_states": unique_tool,
            "unique_turn_states": unique_turn,
            "loop_detections": self._loop_detections,
        }
