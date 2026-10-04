"""指标采集：让"我的工具层好不好"变成可打印的数字。

为什么工具层也需要指标？

* 面试必问："你怎么知道 Agent 的效果好不好？" —— 只看最终答案是不够的，
  还要知道**工具调用失败率**（说明 Schema/描述有问题）、**重试率**（说明下游不稳）、
  **P95 延迟**（说明用户会不会等）、**熔断次数**（说明下游挂了多久）。
* 工程上，这些正是你要接进 Prometheus / Langfuse 的原始数据。

这里只依赖标准库，实现一个够用的计数器 + 分位数统计，输出可直接贴进 README 的表格。
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field

__all__ = ["ToolStat", "Metrics"]


def _percentile(sorted_values: list[float], fraction: float) -> float:
    """线性插值分位数（与 numpy.percentile 默认行为一致）。"""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = fraction * (len(sorted_values) - 1)
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    weight = position - low
    return sorted_values[low] * (1 - weight) + sorted_values[high] * weight


@dataclass
class ToolStat:
    """单个工具的累计统计。"""

    calls: int = 0            # 逻辑调用次数（不含重试）
    attempts: int = 0         # 实际尝试次数（含重试）
    successes: int = 0
    failures: int = 0
    retries: int = 0
    timeouts: int = 0
    rate_limited: int = 0
    circuit_rejected: int = 0
    validation_errors: int = 0
    latencies_ms: deque[float] = field(default_factory=lambda: deque(maxlen=2000))

    @property
    def retry_rate(self) -> float:
        return self.retries / self.attempts if self.attempts else 0.0

    @property
    def success_rate(self) -> float:
        return self.successes / self.calls if self.calls else 0.0

    def latency(self, fraction: float) -> float:
        return _percentile(sorted(self.latencies_ms), fraction)

    def snapshot(self) -> dict:
        return {
            "calls": self.calls,
            "attempts": self.attempts,
            "successes": self.successes,
            "failures": self.failures,
            "retries": self.retries,
            "timeouts": self.timeouts,
            "rate_limited": self.rate_limited,
            "circuit_rejected": self.circuit_rejected,
            "validation_errors": self.validation_errors,
            "success_rate": round(self.success_rate, 4),
            "retry_rate": round(self.retry_rate, 4),
            "p50_ms": round(self.latency(0.50), 2),
            "p95_ms": round(self.latency(0.95), 2),
            "p99_ms": round(self.latency(0.99), 2),
            "mean_ms": round(sum(self.latencies_ms) / len(self.latencies_ms), 2) if self.latencies_ms else 0.0,
        }


class Metrics:
    """线程安全的指标收集器。"""

    def __init__(self) -> None:
        self._stats: dict[str, ToolStat] = {}
        self._lock = threading.Lock()

    def _stat(self, tool: str) -> ToolStat:
        stat = self._stats.get(tool)
        if stat is None:
            stat = ToolStat()
            self._stats[tool] = stat
        return stat

    # ------------------------------------------------------------------ #

    def start_call(self, tool: str) -> None:
        with self._lock:
            stat = self._stat(tool)
            stat.calls += 1

    def record_attempt(self, tool: str, *, duration_ms: float) -> None:
        with self._lock:
            stat = self._stat(tool)
            stat.attempts += 1
            stat.latencies_ms.append(duration_ms)

    def record_success(self, tool: str) -> None:
        with self._lock:
            self._stat(tool).successes += 1

    def record_failure(self, tool: str, error_code: str = "") -> None:
        with self._lock:
            stat = self._stat(tool)
            stat.failures += 1
            if error_code == "timeout":
                stat.timeouts += 1
            elif error_code == "rate_limited":
                stat.rate_limited += 1
            elif error_code == "circuit_open":
                stat.circuit_rejected += 1
            elif error_code == "validation_error":
                stat.validation_errors += 1

    def record_retry(self, tool: str) -> None:
        with self._lock:
            self._stat(tool).retries += 1

    # ------------------------------------------------------------------ #

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {name: stat.snapshot() for name, stat in self._stats.items()}

    def totals(self) -> dict:
        with self._lock:
            total = ToolStat()
            for stat in self._stats.values():
                total.calls += stat.calls
                total.attempts += stat.attempts
                total.successes += stat.successes
                total.failures += stat.failures
                total.retries += stat.retries
                total.timeouts += stat.timeouts
                total.rate_limited += stat.rate_limited
                total.circuit_rejected += stat.circuit_rejected
                total.validation_errors += stat.validation_errors
                total.latencies_ms.extend(stat.latencies_ms)
            return total.snapshot()

    def reset(self) -> None:
        with self._lock:
            self._stats.clear()

    # ------------------------------------------------------------------ #

    def render_table(self) -> str:
        """输出可直接贴进 README 的 Markdown 表格。"""
        rows = self.snapshot()
        if not rows:
            return "（暂无指标）"

        header = ("| 工具 | 调用 | 成功 | 失败 | 重试 | 超时 | 限流 | 熔断 | 成功率 | P50(ms) | P95(ms) |",
                  "|---|---|---|---|---|---|---|---|---|---|---|")
        lines = list(header)
        for name in sorted(rows):
            s = rows[name]
            lines.append(
                f"| `{name}` | {s['calls']} | {s['successes']} | {s['failures']} | {s['retries']} | "
                f"{s['timeouts']} | {s['rate_limited']} | {s['circuit_rejected']} | "
                f"{s['success_rate'] * 100:.1f}% | {s['p50_ms']:.1f} | {s['p95_ms']:.1f} |"
            )
        t = self.totals()
        lines.append(
            f"| **合计** | {t['calls']} | {t['successes']} | {t['failures']} | {t['retries']} | "
            f"{t['timeouts']} | {t['rate_limited']} | {t['circuit_rejected']} | "
            f"{t['success_rate'] * 100:.1f}% | {t['p50_ms']:.1f} | {t['p95_ms']:.1f} |"
        )
        return "\n".join(lines)
