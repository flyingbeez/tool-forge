"""执行器测试 —— 这个文件是整个仓库最重要的一课：

**把"外部依赖故障"和"时间"都变成可编程的输入，才能写出确定性的测试。**
  * sleeper 注入 → 退避不需要真的等
  * clock 注入  → 熔断恢复不需要真的等 5 秒
"""

from __future__ import annotations

import time
import unittest
import warnings

from toolforge import (
    AuditLog,
    CircuitBreakerPolicy,
    Metrics,
    RateLimitPolicy,
    RetryPolicy,
    TimeoutPolicy,
    ToolExecutor,
    ToolPolicy,
    ToolRegistry,
    tool,
)


def fake_clock():
    now = [0.0]
    return now, (lambda: now[0])


def build_registry() -> tuple[ToolRegistry, dict]:
    """构造一组行为可控的工具，state 里记录调用次数。"""
    state: dict = {"flaky": 0, "always_fail": 0, "value_error": 0, "slow": 0}
    registry = ToolRegistry()

    @tool
    def add(a: int, b: int) -> int:
        """两数相加。

        Args:
            a: 加数
            b: 加数
        """
        return a + b

    @tool
    def flaky(fail_times: int = 2) -> str:
        """前 fail_times 次调用抛 ConnectionError，之后成功。

        Args:
            fail_times: 前几次失败
        """
        state["flaky"] += 1
        if state["flaky"] <= fail_times:
            raise ConnectionError("上游抖动")
        return f"第 {state['flaky']} 次成功"

    @tool
    def always_fail() -> str:
        """永远抛 ConnectionError。"""
        state["always_fail"] += 1
        raise ConnectionError("下游挂了")

    @tool
    def value_error() -> str:
        """永远抛 ValueError（不可重试）。"""
        state["value_error"] += 1
        raise ValueError("代码写错了")

    @tool
    def slow(seconds: float = 0.3) -> str:
        """睡眠指定秒数。

        Args:
            seconds: 睡眠秒数
        """
        state["slow"] += 1
        time.sleep(seconds)
        return "slow done"

    @tool
    def echo(text: str) -> str:
        """回显文本。

        Args:
            text: 文本
        """
        return text

    for func in (add, flaky, always_fail, value_error, slow, echo):
        registry.register(func)
    return registry, state


class TestBasicExecution(unittest.TestCase):
    def setUp(self):
        self.registry, self.state = build_registry()
        self.executor = ToolExecutor(self.registry, sleeper=lambda _: None)

    def tearDown(self):
        self.executor.close()

    def test_success(self):
        result = self.executor.call("add", {"a": 2, "b": 3})
        self.assertTrue(result.ok)
        self.assertEqual(result.value, 5)
        self.assertEqual(result.attempts, 1)

    def test_missing_tool(self):
        result = self.executor.call("nope", {})
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "tool_not_found")
        self.assertIn("可用工具", result.error_message)

    def test_validation_error_is_not_retried(self):
        result = self.executor.call("add", {"a": "abc", "b": 1})
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "validation_error")
        self.assertEqual(result.attempts, 0, "参数错误不该产生任何执行尝试")
        self.assertIsInstance(result.details, list)

    def test_validation_error_does_not_touch_tool(self):
        before = self.state["flaky"]
        self.executor.call("flaky", {"fail_times": "not-a-number"})
        self.assertEqual(self.state["flaky"], before, "校验失败时工具函数不应被调用")

    def test_raise_for_status(self):
        with self.assertRaises(Exception):
            self.executor.call("nope", {}).raise_for_status()

    def test_to_dict_is_serialisable(self):
        import json

        payload = self.executor.call("add", {"a": 1, "b": 1}).to_dict()
        self.assertEqual(json.loads(json.dumps(payload))["value"], 2)


class TestRetry(unittest.TestCase):
    def setUp(self):
        self.registry, self.state = build_registry()
        self.sleeps: list[float] = []
        self.executor = ToolExecutor(self.registry, sleeper=self.sleeps.append)

    def tearDown(self):
        self.executor.close()

    def _registry_with_policy(self, **kwargs) -> ToolRegistry:
        policy = ToolPolicy(
            retry=kwargs.pop("retry", RetryPolicy(max_attempts=4, base_delay=0.1,
                                                  multiplier=2.0, jitter=0.0)),
            timeout=kwargs.pop("timeout", TimeoutPolicy(seconds=None)),
            idempotent=kwargs.pop("idempotent", True),
        )
        return self.registry, policy

    def test_retries_until_success(self):
        registry, policy = self._registry_with_policy()
        registry.tools["flaky"].policy = policy
        result = self.executor.call("flaky", {"fail_times": 2})
        self.assertTrue(result.ok, result.error_message)
        self.assertEqual(result.attempts, 3)
        self.assertEqual(len(self.sleeps), 2, "失败两次应退避两次")
        self.assertAlmostEqual(self.sleeps[0], 0.1)
        self.assertAlmostEqual(self.sleeps[1], 0.2)

    def test_retries_exhausted(self):
        registry, policy = self._registry_with_policy()
        registry.tools["always_fail"].policy = policy
        result = self.executor.call("always_fail", {})
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "execution_error")
        self.assertEqual(result.attempts, 4)
        self.assertEqual(self.state["always_fail"], 4)
        self.assertEqual(len(self.sleeps), 3, "最后一次失败后不应再退避")

    def test_non_retryable_exception_stops_immediately(self):
        registry, policy = self._registry_with_policy()
        registry.tools["value_error"].policy = policy
        result = self.executor.call("value_error", {})
        self.assertFalse(result.ok)
        self.assertEqual(result.attempts, 1, "ValueError 属于代码错误，重试无意义")
        self.assertEqual(self.state["value_error"], 1)
        self.assertEqual(self.sleeps, [])

    def test_non_idempotent_tool_never_retries(self):
        registry, policy = self._registry_with_policy(idempotent=False)
        registry.tools["always_fail"].policy = policy
        result = self.executor.call("always_fail", {})
        self.assertEqual(result.attempts, 1, "非幂等工具只允许尝试一次")
        self.assertEqual(self.state["always_fail"], 1)
        self.assertEqual(self.sleeps, [])


class TestTimeout(unittest.TestCase):
    def test_timeout_returns_error_not_exception(self):
        registry, state = build_registry()
        registry.tools["slow"].policy = ToolPolicy(
            retry=RetryPolicy(max_attempts=1),
            timeout=TimeoutPolicy(seconds=0.05),
        )
        with ToolExecutor(registry) as executor:
            result = executor.call("slow", {"seconds": 0.4})
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "timeout")
        self.assertIn("超时", result.error_message)
        del state

    def test_timeout_is_retryable(self):
        registry, _ = build_registry()
        registry.tools["slow"].policy = ToolPolicy(
            retry=RetryPolicy(max_attempts=2, base_delay=0.0, jitter=0.0),
            timeout=TimeoutPolicy(seconds=0.05),
        )
        with ToolExecutor(registry, sleeper=lambda _: None) as executor:
            result = executor.call("slow", {"seconds": 0.4})
        self.assertEqual(result.attempts, 2, "超时应被判定为可重试")

    def test_no_timeout_when_disabled(self):
        registry, _ = build_registry()
        registry.tools["slow"].policy = ToolPolicy(
            retry=RetryPolicy(max_attempts=1), timeout=TimeoutPolicy(seconds=None)
        )
        with ToolExecutor(registry) as executor:
            result = executor.call("slow", {"seconds": 0.01})
        self.assertTrue(result.ok)


class TestRateLimit(unittest.TestCase):
    def test_second_call_is_rate_limited(self):
        now, clock = fake_clock()
        registry, _ = build_registry()
        registry.tools["echo"].policy = ToolPolicy(
            retry=RetryPolicy(max_attempts=1),
            timeout=TimeoutPolicy(seconds=None),
            rate_limit=RateLimitPolicy(rate_per_sec=0.001, burst=1),
        )
        executor = ToolExecutor(registry, clock=clock)
        try:
            first = executor.call("echo", {"text": "a"})
            second = executor.call("echo", {"text": "b"})
        finally:
            executor.close()
        self.assertTrue(first.ok)
        self.assertFalse(second.ok)
        self.assertEqual(second.error_code, "rate_limited")
        self.assertEqual(second.attempts, 0)
        del now


class TestCircuitBreakerInExecutor(unittest.TestCase):
    def setUp(self):
        self.now, self.clock = fake_clock()
        self.registry, self.state = build_registry()
        self.registry.tools["always_fail"].policy = ToolPolicy(
            retry=RetryPolicy(max_attempts=1),
            timeout=TimeoutPolicy(seconds=None),
            circuit=CircuitBreakerPolicy(failure_threshold=2, recovery_timeout=5.0,
                                         half_open_max_calls=1),
        )
        self.executor = ToolExecutor(self.registry, clock=self.clock, sleeper=lambda _: None)

    def tearDown(self):
        self.executor.close()

    def test_opens_then_recovers(self):
        r1 = self.executor.call("always_fail", {})
        r2 = self.executor.call("always_fail", {})
        r3 = self.executor.call("always_fail", {})
        self.assertEqual(r1.error_code, "execution_error")
        self.assertEqual(r2.error_code, "execution_error")
        self.assertEqual(r3.error_code, "circuit_open", "达到阈值后应快速失败")
        self.assertEqual(self.state["always_fail"], 2, "熔断后不应再真正调用工具")
        self.assertFalse(r3.attempts, "熔断拒绝不应产生尝试")

        # 恢复时间到 → 半开探测
        self.now[0] = 5.0
        self.registry.tools["always_fail"].func = lambda: "恢复了"
        r4 = self.executor.call("always_fail", {})
        self.assertTrue(r4.ok)
        self.assertEqual(self.executor.breakers_snapshot()["always_fail"]["state"], "closed")

    def test_circuit_records_one_failure_per_logical_call(self):
        """一次 max_attempts=3 的失败调用，只能给熔断器记 1 次失败。"""
        self.registry.tools["always_fail"].policy = ToolPolicy(
            retry=RetryPolicy(max_attempts=3, base_delay=0.0, jitter=0.0),
            timeout=TimeoutPolicy(seconds=None),
            circuit=CircuitBreakerPolicy(failure_threshold=3, recovery_timeout=5.0),
        )
        self.executor.call("always_fail", {})
        snap = self.executor.breakers_snapshot()["always_fail"]
        self.assertEqual(snap["consecutive_failures"], 1)
        self.assertEqual(snap["state"], "closed")


class TestParallelAndObservability(unittest.TestCase):
    def setUp(self):
        self.registry, self.state = build_registry()
        self.audit = AuditLog()
        self.metrics = Metrics()
        self.executor = ToolExecutor(self.registry, audit=self.audit, metrics=self.metrics,
                                     sleeper=lambda _: None)

    def tearDown(self):
        self.executor.close()

    def test_call_many_parallel_shares_correlation_id(self):
        results = self.executor.call_many(
            [("add", {"a": 1, "b": 2}), ("echo", {"text": "x"}), ("add", {"a": 3, "b": 4})],
            parallel=True,
        )
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r.ok for r in results))
        self.assertEqual(len({r.correlation_id for r in results}), 1)

    def test_call_many_sequential(self):
        results = self.executor.call_many([("echo", {"text": "a"}), ("echo", {"text": "b"})],
                                          parallel=False)
        self.assertEqual([r.value for r in results], ["a", "b"])

    def test_audit_records_every_attempt(self):
        self.registry.tools["always_fail"].policy = ToolPolicy(
            retry=RetryPolicy(max_attempts=3, base_delay=0.0, jitter=0.0),
            timeout=TimeoutPolicy(seconds=None),
        )
        self.executor.call("always_fail", {}, correlation_id="cid-1")
        records = self.audit.for_correlation("cid-1")
        self.assertEqual(len(records), 3)
        self.assertEqual([r.attempt for r in records], [1, 2, 3])
        self.assertTrue(all(not r.ok for r in records))
        self.assertEqual(records[0].error_code, "execution_error")

    def test_audit_redacts_sensitive_args(self):
        registry = ToolRegistry()
        audit = AuditLog()

        @tool
        def register_user(phone: str, name: str) -> str:
            """注册用户。

            Args:
                phone: 手机号
                name: 姓名
            """
            return name

        registry.register(register_user, sensitive_args=("phone",))
        with ToolExecutor(registry, audit=audit, sleeper=lambda _: None) as executor:
            executor.call("register_user", {"phone": "13800000000", "name": "张三"})

        record = audit.all()[0]
        self.assertEqual(record.args["phone"], "***REDACTED***")
        self.assertEqual(record.args["name"], "张三")

    def test_metrics_accumulate(self):
        self.executor.call("add", {"a": 1, "b": 1})
        self.executor.call("add", {"a": "bad", "b": 1})
        snapshot = self.metrics.snapshot()["add"]
        self.assertEqual(snapshot["calls"], 2)
        self.assertEqual(snapshot["successes"], 1)
        self.assertEqual(snapshot["validation_errors"], 1)
        self.assertGreaterEqual(snapshot["p95_ms"], 0)

    def test_metrics_render_table(self):
        self.executor.call("add", {"a": 1, "b": 1})
        table = self.metrics.render_table()
        self.assertIn("| 工具 |", table)
        self.assertIn("`add`", table)
        self.assertIn("合计", table)


class TestDeprecation(unittest.TestCase):
    def test_deprecated_tool_excluded_from_schemas_but_still_callable(self):
        registry = ToolRegistry()

        @tool
        def old_api(x: int) -> int:
            """旧接口。

            Args:
                x: 数字
            """
            return x

        registry.register(old_api, deprecated=True, deprecation_note="请改用 new_api")
        self.assertEqual(registry.schemas(), [], "废弃工具不应下发给模型")
        self.assertEqual(len(registry.deprecated_tools()), 1)

        with ToolExecutor(registry, sleeper=lambda _: None) as executor:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                result = executor.call("old_api", {"x": 1})
        self.assertTrue(result.ok)
        self.assertTrue(any(issubclass(w.category, DeprecationWarning) for w in caught))


if __name__ == "__main__":
    unittest.main()
