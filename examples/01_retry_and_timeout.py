#!/usr/bin/env python3
"""示例 1：重试与超时 —— 分清"值得重试"和"白费力气"。

运行：python examples/01_retry_and_timeout.py

这个示例演示三件事：
  1. 网络抖动（ConnectionError）会被指数退避重试救回来；
  2. 参数错误（ValidationError）一次都不重试 —— 重试不改变参数；
  3. 慢工具超时后返回结构化错误，而不是把整个 Agent 挂死。
"""

from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from toolforge import (  # noqa: E402
    RetryPolicy,
    TimeoutPolicy,
    ToolExecutor,
    ToolPolicy,
    ToolRegistry,
    tool,
)

LINE = "=" * 74


def banner(text: str) -> None:
    print(f"\n{LINE}\n{text}\n{LINE}")


# --------------------------------------------------------------------------- #
# 模拟三个真实世界的下游
# --------------------------------------------------------------------------- #

_attempts = {"flaky": 0}


@tool
def flaky_api(question: str) -> str:
    """模拟一个"经常抽风但重试就好"的上游接口。

    Args:
        question: 查询内容
    """
    _attempts["flaky"] += 1
    if _attempts["flaky"] <= 2:
        raise ConnectionError("上游服务暂时不可用（模拟网络抖动）")
    return f"上游返回：{question} 的答案是 42"


@tool
def slow_api(seconds: float = 0.5) -> str:
    """模拟一个很慢的接口。

    Args:
        seconds: 处理耗时（秒）
    """
    time.sleep(seconds)
    return "慢接口终于返回了"


@tool
def add(a: int, b: int) -> int:
    """两数相加。

    Args:
        a: 加数
        b: 加数
    """
    return a + b


def main() -> None:
    registry = ToolRegistry()
    for func in (flaky_api, slow_api, add):
        registry.register(func)

    # 幂等工具才允许重试；这里三个都是只读的，可以对 flaky_api 开重试
    registry.tools["flaky_api"].policy = ToolPolicy(
        retry=RetryPolicy(max_attempts=4, base_delay=0.05, multiplier=2.0, jitter=0.2),
        timeout=TimeoutPolicy(seconds=2.0),
    )
    registry.tools["slow_api"].policy = ToolPolicy(
        retry=RetryPolicy(max_attempts=1),
        timeout=TimeoutPolicy(seconds=0.2),
    )

    # 演示用：把真实的 sleep 换成"打印 + 真睡一小会儿"，让退避过程看得见
    def visible_sleep(seconds: float) -> None:
        print(f"        ↳ 退避等待 {seconds:.3f}s 后重试…")
        time.sleep(seconds)

    executor = ToolExecutor(registry, sleeper=visible_sleep)
    try:
        banner("场景 A｜网络抖动：指数退避把调用救回来了")
        result = executor.call("flaky_api", {"question": "生命的意义"})
        print(f"  ok={result.ok}  尝试次数={result.attempts}  耗时={result.duration_ms:.1f}ms")
        print(f"  结果：{result.value}")

        banner("场景 B｜参数错误：一次都不重试（重试不改变参数）")
        result = executor.call("add", {"a": "一", "b": 2})
        print(f"  ok={result.ok}  尝试次数={result.attempts}  错误码={result.error_code}")
        print(f"  错误信息：{result.error_message}")

        banner("场景 C｜超时：返回结构化错误，而不是把调用方挂死")
        result = executor.call("slow_api", {"seconds": 0.5})
        print(f"  ok={result.ok}  尝试次数={result.attempts}  错误码={result.error_code}")
        print(f"  错误信息：{result.error_message}")
        print(f"  注意：真实耗时约 0.2s（超时时间），不是 0.5s —— 调用方被保护了")

        banner("小结")
        print("  1. retryable 由**错误类型**决定，不由调用方猜 —— 这是这一层的设计中心")
        print("  2. 退避必须加抖动（jitter），否则大量请求会同时重试形成重试风暴")
        print("  3. 超时保护的是调用方；Python 杀不掉线程，被调函数可能仍在后台跑完")
        print("  4. 非幂等工具（下单/扣款）永远不该重试，见 ToolPolicy.idempotent")
    finally:
        executor.close()


if __name__ == "__main__":
    main()
