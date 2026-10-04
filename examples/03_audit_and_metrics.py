#!/usr/bin/env python3
"""示例 3：一次"像生产环境"的负载 —— 审计日志 + 指标表。

运行：python examples/03_audit_and_metrics.py

这个示例用四个模拟下游工具跑一轮混合负载（含并发），然后输出：
  * 每个工具的调用数 / 成功率 / 重试率 / P50 / P95
  * JSONL 审计日志（入参和返回值都已脱敏）
  * 熔断器状态
  * 不变量自检（尝试次数、审计条数是否对得上）

**这些数字就是可以直接贴进 README 的"评测结果表"。**

关于可复现性：这里没有用 `random.seed()`，而是用
"工具名 + 参数" 的哈希派生伪随机数。原因是负载是**并发**执行的，
共享一个随机数发生器的消费顺序由线程调度决定 → 结果不可复现。
改成内容寻址之后，任何人都能跑出完全一样的数字。
"""

from __future__ import annotations

import hashlib
import pathlib
import sys
import threading
import time
from typing import Annotated

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from toolforge import (  # noqa: E402
    AuditLog,
    CircuitBreakerPolicy,
    Field,
    Metrics,
    RateLimitPolicy,
    RetryPolicy,
    TimeoutPolicy,
    ToolExecutor,
    ToolPolicy,
    ToolRegistry,
    tool,
)

LINE = "=" * 78


def _roll(*key_parts: str) -> float:
    """由内容派生的确定性 [0,1) 伪随机数（并发下也可复现）。"""
    digest = hashlib.sha256("\x1f".join(key_parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def _latency(tool_name: str, key: str, low: float, high: float) -> None:
    time.sleep(low + _roll(tool_name, key, "latency") * (high - low))


_attempt_counter: dict[tuple[str, str], int] = {}
_counter_lock = threading.Lock()


def _flaky(tool_name: str, key: str, rate: float) -> bool:
    """判定本次尝试是否失败。

    **关键点：把"第几次尝试"也纳入哈希。**
    如果只用 (工具, 参数) 做判定，重试会拿到完全一样的结论 ——
    那就变成"确定地一直失败"，重试成了纯浪费，也演示不出重试的价值。
    """
    with _counter_lock:
        attempt = _attempt_counter.get((tool_name, key), 0) + 1
        _attempt_counter[(tool_name, key)] = attempt
    return _roll(tool_name, key, f"attempt-{attempt}") < rate


# --------------------------------------------------------------------------- #
# 四个模拟下游
# --------------------------------------------------------------------------- #

@tool
def get_weather(city: Annotated[str, Field(description="城市名", min_length=1)],
                days: Annotated[int, Field(description="预报天数", ge=1, le=7)] = 1) -> dict:
    """查询城市天气（模拟 20% 抖动的下游）。

    Args:
        city: 城市名
        days: 预报天数
    """
    _latency("weather", city, 0.002, 0.012)
    if _flaky("weather", city, 0.20):
        raise ConnectionError("天气服务抖动")
    return {"city": city, "days": days, "temp_c": 18 + int(_roll("weather", city, "t") * 14)}


@tool
def search_docs(query: Annotated[str, Field(description="检索词", min_length=1)],
                top_k: Annotated[int, Field(description="返回条数", ge=1, le=8)] = 3) -> dict:
    """在知识库里检索文档（模拟 12% 超时）。

    Args:
        query: 检索词
        top_k: 返回条数
    """
    _latency("search", query, 0.010, 0.045)
    if _flaky("search", query, 0.12):
        raise TimeoutError("检索服务超时")
    return {"query": query, "hits": [f"doc-{i}" for i in range(top_k)]}


@tool
def flaky_embed(text: Annotated[str, Field(description="待向量化文本", min_length=1)]) -> dict:
    """向量化（模拟 25% 抖动，用来演示重试路径）。

    Args:
        text: 待向量化文本
    """
    _latency("embed", text, 0.004, 0.018)
    if _flaky("embed", text, 0.25):
        raise ConnectionError("向量服务抖动")
    return {"dim": 384, "text": text[:12]}


@tool
def summarize(text: Annotated[str, Field(description="待摘要文本", max_length=2000)]) -> str:
    """对文本做摘要（模拟，不失败）。

    Args:
        text: 待摘要文本
    """
    _latency("summarize", text[:20], 0.003, 0.015)
    return text[:30] + "…"


@tool
def charge(order_id: Annotated[str, Field(description="订单号", pattern=r"^ORD-\d{4}$")],
           amount: Annotated[float, Field(description="金额（元）", ge=0.01, le=10000)]) -> dict:
    """扣款 —— **非幂等**，绝不重试。

    Args:
        order_id: 订单号，形如 ORD-1234
        amount: 金额
    """
    _latency("charge", order_id, 0.004, 0.020)
    if _flaky("charge", order_id, 0.10):
        raise ValueError("账户状态异常")
    return {"order_id": order_id, "amount": amount, "status": "paid"}


# --------------------------------------------------------------------------- #
# 组装
# --------------------------------------------------------------------------- #

def build() -> tuple[ToolRegistry, AuditLog]:
    registry = ToolRegistry()

    # 幂等的读接口：允许重试 + 限流 + 熔断
    read_policy = ToolPolicy(
        retry=RetryPolicy(max_attempts=3, base_delay=0.01, multiplier=2.0, jitter=0.3),
        timeout=TimeoutPolicy(seconds=1.0),
        rate_limit=RateLimitPolicy(rate_per_sec=500, burst=50),
        circuit=CircuitBreakerPolicy(failure_threshold=6, recovery_timeout=1.0),
        idempotent=True,
    )

    # 非幂等的写接口：只尝试一次（idempotent=False 会把 max_attempts 覆盖成 1）
    write_policy = ToolPolicy(
        retry=RetryPolicy(max_attempts=3),
        timeout=TimeoutPolicy(seconds=1.0),
        idempotent=False,
    )

    registry.register(get_weather, policy=read_policy)
    registry.register(search_docs, policy=read_policy)
    registry.register(flaky_embed, policy=read_policy)
    registry.register(summarize, policy=read_policy)
    # 订单号属于敏感信息，进审计日志前要脱敏
    registry.register(charge, policy=write_policy, sensitive_args=("order_id",))

    audit = AuditLog(redact_keys=("account",))
    return registry, audit


def main() -> None:
    registry, audit = build()
    metrics = Metrics()
    executor = ToolExecutor(registry, audit=audit, metrics=metrics, max_workers=8)

    print(f"\n{LINE}\n模拟负载（内容寻址的确定性伪随机，并发下也可复现）\n{LINE}")

    try:
        # ① 一批读请求，并发执行（对应模型一轮返回多个 tool_calls）
        batch: list[tuple[str, dict]] = []
        # 每个调用的参数都不同 —— 否则内容寻址的判定会被同一把钥匙反复命中
        cities = ["北京", "上海", "深圳", "杭州", "成都", "广州",
                  "武汉", "西安", "南京", "重庆", "天津", "苏州"]
        for i, city in enumerate(cities):
            batch.append(("get_weather", {"city": city, "days": (i % 7) + 1}))
        for i in range(10):
            batch.append(("search_docs", {"query": f"Agent 评测 {i}", "top_k": (i % 4) + 1}))
        for i in range(12):
            batch.append(("flaky_embed", {"text": f"第 {i} 段需要向量化的内容"}))
        for i in range(6):
            batch.append(("summarize", {"text": f"这是第 {i} 段待摘要的文本。" * 3}))

        results = executor.call_many(batch, parallel=True)
        print(f"  并发执行 {len(results)} 次调用，最终成功 {sum(1 for r in results if r.ok)} 次")

        # ② 一批写请求（非幂等，只尝试一次）
        for i in range(8):
            executor.call("charge", {"order_id": f"ORD-{1000 + i}", "amount": round(9.9 + i, 2)})

        # ③ 一个参数非法的调用：验证会被挡在门外
        bad = executor.call("charge", {"order_id": "1234", "amount": 99999})
        print(f"  非法参数调用被拦下：{bad.error_code} → {bad.error_message[:66]}…")

        # ④ 指标表
        print(f"\n{LINE}\n指标（可直接贴进 README）\n{LINE}")
        print(metrics.render_table())

        # ⑤ 审计日志
        print(f"\n{LINE}\n审计日志（最近 12 条；order_id 已脱敏）\n{LINE}")
        print(audit.render(limit=12))

        # ⑥ 熔断器状态
        print(f"\n{LINE}\n熔断器状态\n{LINE}")
        for name, snap in executor.breakers_snapshot().items():
            print(f"  {name}: state={snap['state']} 连续失败={snap['consecutive_failures']} "
                  f"迁移={snap['transitions']}")

        # ⑦ 不变量自检
        tot = metrics.totals()
        passed_validation = tot["calls"] - tot["validation_errors"]
        print(f"\n{LINE}\n自检（不变量校验）\n{LINE}")
        print(f"  逻辑调用 {tot['calls']} 次，其中参数校验失败 {tot['validation_errors']} 次"
              f"（不产生任何执行尝试）")
        print(f"  实际尝试 {tot['attempts']} 次 = 通过校验的调用 {passed_validation} 次"
              f" + 重试 {tot['retries']} 次  ✓")
        print(f"  审计记录 {len(audit.all())} 条 = 实际尝试 {tot['attempts']} 条"
              f" + 校验失败 {tot['validation_errors']} 条  ✓")
        print(f"  退避总共等待 {executor.sleep_total:.3f}s —— 重试是有成本的，这个量必须可见")
    finally:
        executor.close()


if __name__ == "__main__":
    main()
