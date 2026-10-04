"""韧性策略测试：重试 / 令牌桶 / 熔断状态机。

这些代码如果不可测，就说明设计有问题 —— 所以全部支持注入 clock / rng。
"""

from __future__ import annotations

import random
import unittest

from toolforge.policy import (
    CircuitBreaker,
    CircuitBreakerPolicy,
    CircuitState,
    RateLimitPolicy,
    RetryPolicy,
    TimeoutPolicy,
    TokenBucket,
    ToolPolicy,
)


def fake_clock():
    now = [0.0]
    return now, (lambda: now[0])


class TestRetryPolicy(unittest.TestCase):
    def test_exponential_backoff_without_jitter(self):
        policy = RetryPolicy(base_delay=0.2, multiplier=2.0, jitter=0.0, max_delay=100)
        self.assertAlmostEqual(policy.delay_for(1), 0.2)
        self.assertAlmostEqual(policy.delay_for(2), 0.4)
        self.assertAlmostEqual(policy.delay_for(3), 0.8)
        self.assertAlmostEqual(policy.delay_for(4), 1.6)

    def test_capped_at_max_delay(self):
        policy = RetryPolicy(base_delay=1.0, multiplier=10.0, jitter=0.0, max_delay=5.0)
        self.assertEqual(policy.delay_for(5), 5.0)

    def test_jitter_within_bounds(self):
        policy = RetryPolicy(base_delay=1.0, multiplier=1.0, jitter=0.5, rng=random.Random(7))
        for _ in range(50):
            delay = policy.delay_for(1)
            self.assertGreaterEqual(delay, 0.5)
            self.assertLessEqual(delay, 1.5)

    def test_jitter_is_deterministic_with_injected_rng(self):
        a = RetryPolicy(base_delay=1.0, jitter=0.5, rng=random.Random(1))
        b = RetryPolicy(base_delay=1.0, jitter=0.5, rng=random.Random(1))
        self.assertEqual([a.delay_for(i) for i in range(1, 6)],
                         [b.delay_for(i) for i in range(1, 6)])

    def test_invalid_config(self):
        with self.assertRaises(ValueError):
            RetryPolicy(max_attempts=0)
        with self.assertRaises(ValueError):
            RetryPolicy(jitter=2)

    def test_tool_policy_effective_attempts_respects_idempotence(self):
        policy = ToolPolicy(retry=RetryPolicy(max_attempts=4), idempotent=True)
        self.assertEqual(policy.effective_attempts(), 4)
        policy.idempotent = False
        self.assertEqual(policy.effective_attempts(), 1, "非幂等工具绝不能重试")


class TestTimeoutPolicy(unittest.TestCase):
    def test_default_and_none(self):
        self.assertEqual(TimeoutPolicy().seconds, 10.0)
        self.assertIsNone(TimeoutPolicy(seconds=None).seconds)

    def test_invalid(self):
        with self.assertRaises(ValueError):
            TimeoutPolicy(seconds=0)


class TestTokenBucket(unittest.TestCase):
    def test_burst_then_exhausted(self):
        now, clock = fake_clock()
        bucket = TokenBucket(RateLimitPolicy(rate_per_sec=2.0, burst=2), clock=clock)
        self.assertTrue(bucket.try_acquire())
        self.assertTrue(bucket.try_acquire())
        self.assertFalse(bucket.try_acquire())
        del now

    def test_refill_over_time(self):
        now, clock = fake_clock()
        bucket = TokenBucket(RateLimitPolicy(rate_per_sec=2.0, burst=2), clock=clock)
        self.assertTrue(bucket.try_acquire())
        self.assertTrue(bucket.try_acquire())
        self.assertFalse(bucket.try_acquire())
        now[0] = 0.5  # 2/s * 0.5s = 1 个令牌
        self.assertTrue(bucket.try_acquire())
        self.assertFalse(bucket.try_acquire())

    def test_refill_capped_at_burst(self):
        now, clock = fake_clock()
        bucket = TokenBucket(RateLimitPolicy(rate_per_sec=100.0, burst=3), clock=clock)
        now[0] = 1000.0
        self.assertEqual(bucket.available, 3.0)

    def test_config_validation(self):
        with self.assertRaises(ValueError):
            RateLimitPolicy(rate_per_sec=0)
        with self.assertRaises(ValueError):
            RateLimitPolicy(burst=0)


class TestCircuitBreaker(unittest.TestCase):
    def setUp(self):
        self.now, self.clock = fake_clock()
        self.policy = CircuitBreakerPolicy(failure_threshold=3, recovery_timeout=5.0,
                                           half_open_max_calls=1)
        self.breaker = CircuitBreaker(self.policy, clock=self.clock)

    def test_opens_after_threshold(self):
        self.assertEqual(self.breaker.state, CircuitState.CLOSED)
        self.breaker.record_failure()
        self.breaker.record_failure()
        self.assertEqual(self.breaker.state, CircuitState.CLOSED, "未达阈值不应打开")
        self.assertTrue(self.breaker.allow())
        self.breaker.record_failure()
        self.assertEqual(self.breaker.state, CircuitState.OPEN)

    def test_open_rejects_until_recovery_timeout(self):
        for _ in range(3):
            self.breaker.record_failure()
        self.assertFalse(self.breaker.allow())
        self.now[0] = 4.9
        self.assertFalse(self.breaker.allow(), "未到恢复时间不应放行")
        self.now[0] = 5.0
        self.assertEqual(self.breaker.state, CircuitState.HALF_OPEN)

    def test_half_open_success_closes(self):
        for _ in range(3):
            self.breaker.record_failure()
        self.now[0] = 5.0
        self.assertTrue(self.breaker.allow(), "半开状态应放行探测请求")
        self.assertFalse(self.breaker.allow(), "半开状态只放行 half_open_max_calls 个")
        self.breaker.record_success()
        self.assertEqual(self.breaker.state, CircuitState.CLOSED)

    def test_half_open_failure_reopens(self):
        for _ in range(3):
            self.breaker.record_failure()
        self.now[0] = 5.0
        self.assertTrue(self.breaker.allow())
        self.breaker.record_failure()
        self.assertEqual(self.breaker.state, CircuitState.OPEN)
        self.now[0] = 9.9
        self.assertFalse(self.breaker.allow(), "重新打开后计时器要重置")

    def test_success_resets_consecutive_failures(self):
        self.breaker.record_failure()
        self.breaker.record_failure()
        self.breaker.record_success()
        self.breaker.record_failure()
        self.breaker.record_failure()
        self.assertEqual(self.breaker.state, CircuitState.CLOSED,
                         "成功一次后连续失败计数应清零")

    def test_transitions_are_recorded(self):
        for _ in range(3):
            self.breaker.record_failure()
        self.now[0] = 5.0
        self.breaker.allow()
        self.breaker.record_success()
        self.assertEqual(
            [(a, b) for a, b, _ in self.breaker.transitions],
            [("closed", "open"), ("open", "half_open"), ("half_open", "closed")],
        )

    def test_snapshot(self):
        self.breaker.record_failure()
        snap = self.breaker.snapshot()
        self.assertEqual(snap["state"], "closed")
        self.assertEqual(snap["consecutive_failures"], 1)

    def test_config_validation(self):
        with self.assertRaises(ValueError):
            CircuitBreakerPolicy(failure_threshold=0)
        with self.assertRaises(ValueError):
            CircuitBreakerPolicy(half_open_max_calls=0)


if __name__ == "__main__":
    unittest.main()
