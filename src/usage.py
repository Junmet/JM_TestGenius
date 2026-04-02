from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _safe_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


@dataclass
class UsageTracker:
    """
    累计 LLM 调用次数、接口返回的 token（若存在）以及请求/回复字符量（粗略成本参考）。
    """

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens_reported: int = 0
    prompt_chars: int = 0
    completion_chars: int = 0
    rows: list[dict[str, Any]] = field(default_factory=list)

    def record(
        self,
        *,
        stage: str,
        source_name: str,
        messages: list[dict[str, str]],
        content: str,
        response_metadata: dict[str, Any] | None,
        usage_metadata: dict[str, Any] | None,
    ) -> None:
        pc = sum(len(str(m.get("content") or "")) for m in messages)
        cc = len(content or "")
        self.prompt_chars += pc
        self.completion_chars += cc
        self.calls += 1

        pt = ct = tt = None
        if isinstance(response_metadata, dict):
            tu = response_metadata.get("token_usage")
            if not isinstance(tu, dict):
                tu = response_metadata.get("usage")
            if isinstance(tu, dict):
                pt = _safe_int(tu.get("prompt_tokens"))
                ct = _safe_int(tu.get("completion_tokens"))
                tt = _safe_int(tu.get("total_tokens"))
        if isinstance(usage_metadata, dict):
            if pt is None:
                pt = _safe_int(usage_metadata.get("input_tokens"))
            if ct is None:
                ct = _safe_int(usage_metadata.get("output_tokens"))
            if tt is None:
                tt = _safe_int(usage_metadata.get("total_tokens"))

        if pt is not None:
            self.prompt_tokens += pt
        if ct is not None:
            self.completion_tokens += ct
        if tt is not None:
            self.total_tokens_reported += tt
        elif pt is not None and ct is not None:
            self.total_tokens_reported += pt + ct

        self.rows.append(
            {
                "stage": stage,
                "source": source_name,
                "prompt_tokens": pt,
                "completion_tokens": ct,
                "total_tokens": tt,
                "prompt_chars": pc,
                "completion_chars": cc,
            }
        )

    @property
    def estimated_tokens(self) -> int:
        """任务总 token 预估：优先各次接口返回累计；无则 (请求+回复)字符÷4 粗估。"""
        if self.total_tokens_reported > 0:
            return self.total_tokens_reported
        return max(1, (self.prompt_chars + self.completion_chars) // 4)

    @property
    def token_estimate_source(self) -> str:
        """供控制台一行展示：数字含义说明。"""
        if self.total_tokens_reported > 0:
            return "接口返回累计"
        return "无接口 token 时按字符÷4 粗估"

    def budget_remaining(self, max_total_tokens: int | None) -> int | None:
        if max_total_tokens is None or max_total_tokens <= 0:
            return None
        return max(0, max_total_tokens - self.estimated_tokens)


class UsageBudgetExceeded(RuntimeError):
    """累计用量超过 --max-total-tokens 上限。"""


def check_usage_budget(usage: UsageTracker, max_total_tokens: int | None) -> None:
    if max_total_tokens is None or max_total_tokens <= 0:
        return
    if usage.estimated_tokens > max_total_tokens:
        raise UsageBudgetExceeded(
            f"累计用量（估算 token≈{usage.estimated_tokens}）已超过上限 {max_total_tokens}，"
            "已停止后续调用以避免打爆配额。可调高 --max-total-tokens 或减小文档/用例规模。"
        )
