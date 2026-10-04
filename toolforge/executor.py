"""执行器：把「查表 → 校验 → 限流 → 熔断 → 带超时执行 → 按需重试 → 记审计 → 记指标」串成一条流水线。

调用链::

    call(tool, args)
      │
      ├─ 1. 查表           工具不存在 → ToolNotFound（不重试，回灌可用清单）
      ├─ 2. 参数校验       失败 → ValidationError（不重试，带 JSON 路径错误）
      ├─ 3. 限流           无令牌 → RateLimited（可重试，交给退避等待）
      ├─ 4. 熔断           开路 → CircuitOpen（不重试，快速失败）
      ├─ 5. 执行（带超时）
      │      ├─ 成功 → 记成功 → 返回
      │      └─ 失败 → 分类 retryable?
      │                ├─ 否 → 终止
      │                └─ 是 → 退避等待 → 回到 5（直到 max_attempts）
      └─ 6. 每次尝试都写审计 + 指标
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from .audit import AuditLog
from .errors import (
    CircuitOpen,
    ToolError,
    ToolExecutionError,
    ToolNotFound,
    ToolTimeout,
    RateLimited,
    ValidationError,
)
from .metrics import Metrics
from .policy import CircuitBreaker, TokenBucket
from .registry import ToolRegistry, ToolSpec
from .schema import validate_args

__all__ = ["ToolResult", "ToolExecutor"]

# 明确可重试的底层异常。注意**不包含裸 OSError** —— 它会把 FileNotFoundError
# 这类确定性错误也当成可重试，属于典型的过度重试。
_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    TimeoutError,
    ConnectionError,
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
)


@dataclass
class ToolResult:
    """一次逻辑调用的结果（含重试后的最终状态）。"""

    tool: str
    ok: bool
    value: Any = None
    error_code: str = ""
    error_message: str = ""
    attempts: int = 0
    duration_ms: float = 0.0
    correlation_id: str = ""
    details: Any = None

    def raise_for_status(self) -> Any:
        if self.ok:
            return self.value
        raise ToolError(self.error_message, code=self.error_code, details=self.details)

    def to_dict(self) -> dict:
        payload = {
            "tool": self.tool,
            "ok": self.ok,
            "attempts": self.attempts,
            "duration_ms": round(self.duration_ms, 2),
            "correlation_id": self.correlation_id,
        }
        if self.ok:
            payload["value"] = self.value
        else:
            payload["error_code"] = self.error_code
            payload["error_message"] = self.error_message
        return payload


class ToolExecutor:
    """工具执行器。

    Args:
        registry: 工具注册表。
        audit: 审计日志（可选）。
        metrics: 指标收集器（可选，默认自建一个）。
        max_workers: 超时用的线程池大小。必须 >= 并发调用数，否则请求会排队。
        sleeper: 注入的 sleep 函数 —— 测试里替换掉，退避就不用真的等。
        clock: 注入的单调时钟。
        warn_deprecated: 调用已废弃工具时是否发 DeprecationWarning。
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        audit: AuditLog | None = None,
        metrics: Metrics | None = None,
        max_workers: int = 16,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        warn_deprecated: bool = True,
    ) -> None:
        self.registry = registry
        self.audit = audit
        self.metrics = metrics or Metrics()
        self.max_workers = max(1, max_workers)
        self._sleeper = sleeper
        self._clock = clock
        self._warn_deprecated = warn_deprecated

        self._buckets: dict[str, TokenBucket] = {}
        self._breakers: dict[str, CircuitBreaker] = {}
        self._state_lock = threading.Lock()
        self._pool: ThreadPoolExecutor | None = None
        self._pool_lock = threading.Lock()
        self.sleep_total = 0.0  # 累计退避等待秒数，便于演示"重试的成本"

    # ------------------------------------------------------------------ #
    # 内部状态
    # ------------------------------------------------------------------ #

    def _bucket(self, spec: ToolSpec) -> TokenBucket | None:
        if spec.policy.rate_limit is None:
            return None
        with self._state_lock:
            bucket = self._buckets.get(spec.name)
            if bucket is None:
                bucket = TokenBucket(spec.policy.rate_limit, clock=self._clock)
                self._buckets[spec.name] = bucket
            return bucket

    def _breaker(self, spec: ToolSpec) -> CircuitBreaker | None:
        if spec.policy.circuit is None:
            return None
        with self._state_lock:
            breaker = self._breakers.get(spec.name)
            if breaker is None:
                breaker = CircuitBreaker(spec.policy.circuit, clock=self._clock)
                self._breakers[spec.name] = breaker
            return breaker

    def _timeout_pool(self) -> ThreadPoolExecutor:
        with self._pool_lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(
                    max_workers=self.max_workers, thread_name_prefix="toolforge"
                )
            return self._pool

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #

    def call(self, tool: str, args: Any = None, *, correlation_id: str | None = None) -> ToolResult:
        """执行一次工具调用（内部可能包含多次重试尝试）。"""
        cid = correlation_id or uuid.uuid4().hex
        started = self._clock()
        result = ToolResult(tool=tool, ok=False, correlation_id=cid)

        # --- 1. 查表 ---------------------------------------------------- #
        try:
            spec = self.registry.get(tool)
        except ToolNotFound as exc:
            result.error_code = exc.code
            result.error_message = exc.message
            self._write_audit(tool, 0, False, args, exc, 0.0, cid, "")
            return result

        if self._warn_deprecated:
            self.registry.warn_if_deprecated(tool)

        self.metrics.start_call(tool)

        # --- 2. 参数校验 ------------------------------------------------ #
        try:
            cleaned = validate_args(spec.parameters, args)
        except ValidationError as exc:
            self._write_audit(tool, 0, False, args, exc, (self._clock() - started) * 1000, cid, "")
            self.metrics.record_failure(tool, exc.code)
            result.error_code = exc.code
            result.error_message = exc.message
            result.details = exc.details
            result.duration_ms = (self._clock() - started) * 1000
            return result

        # --- 3. 限流 ---------------------------------------------------- #
        bucket = self._bucket(spec)
        if bucket is not None and not bucket.try_acquire():
            exc = RateLimited(f"工具 {tool} 触发限流（{spec.policy.rate_limit.rate_per_sec}/s）")
            self._write_audit(tool, 0, False, cleaned, exc, 0.0, cid, "")
            self.metrics.record_failure(tool, exc.code)
            result.error_code = exc.code
            result.error_message = exc.message
            result.attempts = 0
            result.duration_ms = (self._clock() - started) * 1000
            return result

        # --- 4. 熔断 ---------------------------------------------------- #
        breaker = self._breaker(spec)
        if breaker is not None and not breaker.allow():
            exc = CircuitOpen(
                f"工具 {tool} 处于熔断状态（{breaker.state.value}），已快速失败"
            )
            self._write_audit(tool, 0, False, cleaned, exc, 0.0, cid, breaker.state.value)
            self.metrics.record_failure(tool, exc.code)
            result.error_code = exc.code
            result.error_message = exc.message
            result.duration_ms = (self._clock() - started) * 1000
            return result

        # --- 5. 执行 + 重试 --------------------------------------------- #
        max_attempts = spec.policy.effective_attempts()
        last_error: ToolError | None = None
        attempts_made = 0

        for attempt in range(1, max_attempts + 1):
            attempts_made = attempt
            attempt_started = self._clock()
            try:
                value = self._invoke(spec, cleaned)
            except BaseException as exc:  # noqa: BLE001 - 任何异常都要分类
                duration = (self._clock() - attempt_started) * 1000
                error = self._classify(exc, spec)
                self.metrics.record_attempt(tool, duration_ms=duration)
                self._write_audit(tool, attempt, False, cleaned, error, duration, cid,
                                  breaker.state.value if breaker else "")
                last_error = error

                if not error.retryable or attempt >= max_attempts:
                    break
                self.metrics.record_retry(tool)
                delay = spec.policy.retry.delay_for(attempt)
                self.sleep_total += delay
                self._sleeper(delay)
                continue

            duration = (self._clock() - attempt_started) * 1000
            self.metrics.record_attempt(tool, duration_ms=duration)
            self.metrics.record_success(tool)
            self._write_audit(tool, attempt, True, cleaned, None, duration, cid,
                              breaker.state.value if breaker else "", result=value)
            result.ok = True
            result.value = value
            result.attempts = attempt
            result.duration_ms = (self._clock() - started) * 1000
            if breaker is not None:
                breaker.record_success()
            return result

        # --- 6. 收尾：熔断记账 + 组装失败结果 --------------------------- #
        # 只按"逻辑调用"的最终结果记账，而不是每次尝试都记 ——
        # 否则一次 max_attempts=3 的调用失败就会把熔断器的失败计数推高 3 倍。
        if breaker is not None:
            breaker.record_failure()

        final = last_error or ToolExecutionError(f"工具 {tool} 执行失败", retryable=False)
        self.metrics.record_failure(tool, final.code)
        result.error_code = final.code
        result.error_message = final.message
        result.details = final.details
        result.attempts = attempts_made
        result.duration_ms = (self._clock() - started) * 1000
        return result

    def call_many(self, calls: Sequence[tuple[str, Any]], *, parallel: bool = True,
                  correlation_id: str | None = None) -> list[ToolResult]:
        """批量调用。``parallel=True`` 时并发执行（对应模型一轮返回多个 tool_calls）。"""
        cid = correlation_id or uuid.uuid4().hex
        if not parallel or len(calls) <= 1:
            return [self.call(name, args, correlation_id=cid) for name, args in calls]

        workers = min(len(calls), 8)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="toolforge-batch") as pool:
            futures = [pool.submit(self.call, name, args, correlation_id=cid) for name, args in calls]
            return [future.result() for future in futures]

    # ------------------------------------------------------------------ #
    # 可观测辅助
    # ------------------------------------------------------------------ #

    def breakers_snapshot(self) -> dict[str, dict]:
        with self._state_lock:
            return {name: breaker.snapshot() for name, breaker in self._breakers.items()}

    def reset_circuits(self) -> None:
        with self._state_lock:
            self._breakers.clear()
            self._buckets.clear()
        self.sleep_total = 0.0

    def close(self) -> None:
        with self._pool_lock:
            if self._pool is not None:
                self._pool.shutdown(wait=False)
                self._pool = None

    def __enter__(self) -> "ToolExecutor":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # 内部实现
    # ------------------------------------------------------------------ #

    def _invoke(self, spec: ToolSpec, cleaned: dict) -> Any:
        """执行工具函数，必要时施加超时。"""
        timeout = spec.policy.timeout.seconds
        if timeout is None:
            return spec.func(**cleaned)

        pool = self._timeout_pool()
        future = pool.submit(spec.func, **cleaned)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeout as exc:
            # 注意：Python 无法真正杀死线程，cancel() 只在任务未开始时有意义。
            # 所以超时保护的是**调用方**，被调用的函数仍可能在后台跑完。
            future.cancel()
            raise ToolTimeout(f"工具 {spec.name} 调用超时（>{timeout}s）") from exc

    @staticmethod
    def _classify(exc: BaseException, spec: ToolSpec) -> ToolError:
        """把任意异常翻译成带 retryable 语义的 ToolError。"""
        if isinstance(exc, ToolError):
            return exc

        if isinstance(exc, _RETRYABLE_EXCEPTIONS):
            return ToolExecutionError(
                f"{spec.name} 执行失败（可重试）：{type(exc).__name__}: {exc}", retryable=True
            )

        if isinstance(exc, (ValueError, TypeError, KeyError, IndexError, AttributeError,
                            NotImplementedError, AssertionError)):
            # 这些几乎都是"代码写错了"或"入参不对"，重试一百次结果一样
            return ToolExecutionError(
                f"{spec.name} 执行失败（不可重试）：{type(exc).__name__}: {exc}", retryable=False
            )

        # 未知异常：默认**不重试**。宁可少重试，也不要在有副作用的工具上重复执行。
        if spec.policy.idempotent:
            return ToolExecutionError(
                f"{spec.name} 执行失败（未知异常，幂等工具按可重试处理）："
                f"{type(exc).__name__}: {exc}", retryable=True
            )
        return ToolExecutionError(
            f"{spec.name} 执行失败（未知异常，非幂等工具不重试）：{type(exc).__name__}: {exc}",
            retryable=False,
        )

    def _write_audit(self, tool: str, attempt: int, ok: bool, args: Any,
                     error: ToolError | None, duration_ms: float, cid: str,
                     circuit_state: str, result: Any = None) -> None:
        if self.audit is None:
            return
        spec = self.registry.tools.get(tool)
        self.audit.record(
            tool=tool,
            attempt=attempt,
            ok=ok,
            args=args,
            result=result,
            error_code=error.code if error else "",
            error_message=error.message if error else "",
            duration_ms=duration_ms,
            correlation_id=cid,
            circuit_state=circuit_state,
            extra_keys=spec.sensitive_args if spec else (),
        )
