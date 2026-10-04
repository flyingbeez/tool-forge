#!/usr/bin/env python3
"""示例 2：熔断器三态状态机 —— 下游整体挂掉时，快速失败比排队更有价值。

运行：python examples/02_circuit_breaker.py

为了让状态迁移一眼看清，这里注入了**可控时钟**，所以整个演示瞬间完成，
不需要真的等 5 秒恢复时间。这也是"可测试性驱动设计"的好处。
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from toolforge import (  # noqa: E402
    CircuitBreakerPolicy,
    RetryPolicy,
    TimeoutPolicy,
    ToolExecutor,
    ToolPolicy,
    ToolRegistry,
    tool,
)

LINE = "=" * 74

# 可控时钟：所有熔断时间判断都基于它
_now = [0.0]


def clock() -> float:
    return _now[0]


_alive = [False]


@tool
def downstream_api(key: str) -> str:
    """模拟一个"整体挂掉"的下游服务。

    Args:
        key: 查询键
    """
    if not _alive[0]:
        raise ConnectionError("下游服务不可用（502）")
    return f"下游返回 {key}"


def main() -> None:
    registry = ToolRegistry()
    registry.register(downstream_api)
    registry.tools["downstream_api"].policy = ToolPolicy(
        retry=RetryPolicy(max_attempts=1),
        timeout=TimeoutPolicy(seconds=None),
        circuit=CircuitBreakerPolicy(failure_threshold=3, recovery_timeout=5.0,
                                     half_open_max_calls=1),
    )

    executor = ToolExecutor(registry, clock=clock, sleeper=lambda _: None)
    try:
        print(f"\n{LINE}\n熔断器：连续失败 3 次后打开，5 秒后进入半开探测\n{LINE}")

        for i in range(1, 6):
            result = executor.call("downstream_api", {"key": f"k{i}"})
            state = executor.breakers_snapshot()["downstream_api"]["state"]
            mark = "" if i <= 3 else "   ← 已被熔断拦下，没有真正调用下游"
            print(f"  调用 {i}: {result.error_code or 'OK':<14} 熔断器状态={state}{mark}")

        print(f"\n  ── 模拟时间前进 5 秒（下游已恢复）──")
        _now[0] = 5.0
        _alive[0] = True

        result = executor.call("downstream_api", {"key": "probe"})
        state = executor.breakers_snapshot()["downstream_api"]["state"]
        print(f"  半开探测: {'成功' if result.ok else result.error_code}  熔断器状态={state}")
        print("  → 探测成功，熔断器自动闭合，恢复正常放行")

        print(f"\n{LINE}\n状态迁移轨迹\n{LINE}")
        transitions = executor.breakers_snapshot()["downstream_api"]["transitions"]
        chain = " → ".join([transitions[0][0]] + [after for _, after in transitions]) if transitions else "（无）"
        print(f"  {chain}")

        print(f"\n{LINE}\n为什么需要熔断\n{LINE}")
        print("  1. 下游整体挂掉时，重试和排队只会让请求越堆越多，还拖垮自己")
        print("  2. 熔断后**立即失败**，把错误快速返回给上层（这里就是 Agent）")
        print("  3. 等待恢复时间后只放**一个**探测请求，避免恢复瞬间被流量打死")
        print("  4. 这就是 retryable=False 的原因：熔断器已经判断重试没意义了")
    finally:
        executor.close()


if __name__ == "__main__":
    main()
