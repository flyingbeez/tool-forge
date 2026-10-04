"""韧性策略：重试 / 超时 / 限流 / 熔断。

这四个是"调用外部依赖"的标准配置，缺一个上线都会出问题：

* **重试**：网络抖动是常态。但必须区分"能重试的错误"和"重试没用的错误"。
* **超时**：没有超时的调用会把整个 Agent 拖死。超时是**保护调用方**，不是保护被调用方。
* **限流**：保护下游，也保护你自己（429 会被上游拉黑）。
* **熔断**：下游整体挂了的时候，快速失败比让请求排队更有价值 —— 这是"止损"，不是"放弃"。

设计原则：所有策略都可通过注入 ``clock`` / ``rng`` 实现**确定性测试**，
否则这类代码根本没法写单测。
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from .errors import ToolError

__all__ = [
    "RetryPolicy",
    "TimeoutPolicy",
    "RateLimitPolicy",
    "CircuitBreakerPolicy",
    "ToolPolicy",
    "TokenBucket",
    "CircuitBreaker",
    "CircuitState",
]


# --------------------------------------------------------------------------- #
# 重试
# --------------------------------------------------------------------------- #

@dataclass
class RetryPolicy:
    """指数退避重试策略。

    Attributes:
        max_attempts: 总尝试次数（含第一次）。1 表示不重试。
        base_delay: 第一次重试前等待秒数。
        max_delay: 单次等待上限。
        multiplier: 退避倍数。
        jitter: 抖动比例 0~1。**必须加抖动**，否则多个请求会同时重试形成"重试风暴"。
        rng: 注入的随机数源，用于测试确定性。
    """

    max_attempts: int = 3
    base_delay: float = 0.2
    max_delay: float = 5.0
    multiplier: float = 2.0
    jitter: float = 0.25
    rng: random.Random | None = None

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts 至少为 1")
        if not 0 <= self.jitter <= 1:
            raise ValueError("jitter 必须在 0~1 之间")

    def delay_for(self, attempt: int) -> float:
        """第 attempt 次失败后应等待的秒数（attempt 从 1 开始）。"""
        raw = min(self.max_delay, self.base_delay * (self.multiplier ** (attempt - 1)))
        if self.jitter:
            source = self.rng or random
            raw *= 1 + source.uniform(-self.jitter, self.jitter)
        return max(0.0, raw)


@dataclass
class TimeoutPolicy:
    """超时策略。``seconds=None`` 表示不限制。"""

    seconds: float | None = 10.0

    def __post_init__(self) -> None:
        if self.seconds is not None and self.seconds <= 0:
            raise ValueError("timeout 必须为正数")


# --------------------------------------------------------------------------- #
# 限流：令牌桶
# --------------------------------------------------------------------------- #

@dataclass
class RateLimitPolicy:
    """令牌桶限流。

    Attributes:
        rate_per_sec: 每秒补充的令牌数（即长期平均 QPS 上限）。
        burst: 桶容量（允许的瞬时突发量）。
    """

    rate_per_sec: float = 5.0
    burst: int = 5

    def __post_init__(self) -> None:
        if self.rate_per_sec <= 0:
            raise ValueError("rate_per_sec 必须为正数")
        if self.burst < 1:
            raise ValueError("burst 至少为 1")


class TokenBucket:
    """线程安全的令牌桶。"""

    def __init__(self, policy: RateLimitPolicy, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.policy = policy
        self._clock = clock
        self._tokens = float(policy.burst)
        self._updated = clock()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        self._updated = now
        self._tokens = min(float(self.policy.burst), self._tokens + elapsed * self.policy.rate_per_sec)

    def try_acquire(self, tokens: int = 1) -> bool:
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def acquire(self, *, tokens: int = 1, timeout: float | None = None,
                sleep: Callable[[float], None] = time.sleep,
                poll: float = 0.01) -> bool:
        """阻塞式获取令牌；超时返回 False。"""
        deadline = None if timeout is None else self._clock() + timeout
        while True:
            if self.try_acquire(tokens):
                return True
            if deadline is not None and self._clock() >= deadline:
                return False
            sleep(poll)

    @property
    def available(self) -> float:
        with self._lock:
            self._refill()
            return self._tokens


# --------------------------------------------------------------------------- #
# 熔断：三态状态机
# --------------------------------------------------------------------------- #

class CircuitState(str, Enum):
    CLOSED = "closed"        # 正常放行
    OPEN = "open"            # 快速失败
    HALF_OPEN = "half_open"  # 放少量探测请求


@dataclass
class CircuitBreakerPolicy:
    """熔断策略。

    Attributes:
        failure_threshold: 连续失败多少次后打开熔断器。
        recovery_timeout: 打开后等待多少秒才允许探测（半开）。
        half_open_max_calls: 半开状态下最多放行几个探测请求。
    """

    failure_threshold: int = 3
    recovery_timeout: float = 5.0
    half_open_max_calls: int = 1

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold 至少为 1")
        if self.half_open_max_calls < 1:
            raise ValueError("half_open_max_calls 至少为 1")


class CircuitBreaker:
    """熔断器三态状态机（线程安全）。

    状态迁移::

        CLOSED ──连续失败 >= 阈值──▶ OPEN
        OPEN ──等待 >= recovery_timeout──▶ HALF_OPEN
        HALF_OPEN ──探测成功──▶ CLOSED
        HALF_OPEN ──探测失败──▶ OPEN
    """

    def __init__(self, policy: CircuitBreakerPolicy, *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.policy = policy
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._half_open_calls = 0
        self._half_open_successes = 0
        self._lock = threading.Lock()
        self.transitions: list[tuple[str, str, float]] = []  # 便于测试与观测

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._maybe_recover()
            return self._state

    def _maybe_recover(self) -> None:
        if self._state is CircuitState.OPEN and (self._clock() - self._opened_at) >= self.policy.recovery_timeout:
            self._transition(CircuitState.HALF_OPEN)

    def _transition(self, new_state: CircuitState) -> None:
        if new_state is self._state:
            return
        self.transitions.append((self._state.value, new_state.value, self._clock()))
        self._state = new_state
        if new_state is CircuitState.HALF_OPEN:
            self._half_open_calls = 0
            self._half_open_successes = 0
        elif new_state is CircuitState.CLOSED:
            self._consecutive_failures = 0
            self._half_open_calls = 0
            self._half_open_successes = 0
        elif new_state is CircuitState.OPEN:
            self._opened_at = self._clock()
            self._half_open_calls = 0
            self._half_open_successes = 0

    def allow(self) -> bool:
        """是否放行本次调用。"""
        with self._lock:
            self._maybe_recover()
            if self._state is CircuitState.CLOSED:
                return True
            if self._state is CircuitState.HALF_OPEN:
                if self._half_open_calls < self.policy.half_open_max_calls:
                    self._half_open_calls += 1
                    return True
                return False
            return False  # OPEN

    def record_success(self) -> None:
        with self._lock:
            if self._state is CircuitState.HALF_OPEN:
                self._half_open_successes += 1
                if self._half_open_successes >= self.policy.half_open_max_calls:
                    self._transition(CircuitState.CLOSED)
            elif self._state is CircuitState.CLOSED:
                self._consecutive_failures = 0

    def record_failure(self) -> None:
        with self._lock:
            if self._state is CircuitState.HALF_OPEN:
                self._transition(CircuitState.OPEN)
                return
            if self._state is CircuitState.CLOSED:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self.policy.failure_threshold:
                    self._transition(CircuitState.OPEN)

    def snapshot(self) -> dict:
        with self._lock:
            self._maybe_recover()
            return {
                "state": self._state.value,
                "consecutive_failures": self._consecutive_failures,
                "half_open_calls": self._half_open_calls,
                "transitions": [(b, a) for b, a, _ in self.transitions],
            }


# --------------------------------------------------------------------------- #
# 策略集合
# --------------------------------------------------------------------------- #

@dataclass
class ToolPolicy:
    """一个工具的完整韧性配置。"""

    retry: RetryPolicy = field(default_factory=RetryPolicy)
    timeout: TimeoutPolicy = field(default_factory=TimeoutPolicy)
    rate_limit: RateLimitPolicy | None = None
    circuit: CircuitBreakerPolicy | None = None
    idempotent: bool = True
    """标记工具是否幂等。

    非幂等工具（下单、发消息、扣款）默认**只尝试一次** ——
    重试一个已经产生副作用的调用，比失败更糟。
    """

    def effective_attempts(self) -> int:
        return self.retry.max_attempts if self.idempotent else 1


def validate_retryable(error: ToolError) -> bool:
    """统一的"要不要重试"判定入口，方便以后换成按错误码配置表。"""
    return bool(getattr(error, "retryable", False))
