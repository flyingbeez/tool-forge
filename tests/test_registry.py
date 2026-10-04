"""注册表与错误类型测试。"""

from __future__ import annotations

import unittest
import warnings

from toolforge import (
    CircuitOpen,
    RateLimited,
    RetryPolicy,
    ToolError,
    ToolExecutionError,
    ToolNotFound,
    ToolPolicy,
    ToolRegistry,
    ToolTimeout,
    ValidationError,
    tool,
)


class TestErrorTaxonomy(unittest.TestCase):
    def test_retryable_flags(self):
        self.assertFalse(ValidationError("bad").retryable)
        self.assertFalse(ToolNotFound("missing").retryable)
        self.assertTrue(ToolTimeout("slow").retryable)
        self.assertTrue(ToolExecutionError("boom").retryable)
        self.assertTrue(RateLimited("throttled").retryable)
        self.assertFalse(CircuitOpen("open").retryable)
        self.assertFalse(ToolError("generic").retryable, "基类默认不可重试")

    def test_stable_codes(self):
        self.assertEqual(ValidationError("x").code, "validation_error")
        self.assertEqual(ToolNotFound("x").code, "tool_not_found")
        self.assertEqual(ToolTimeout("x").code, "timeout")
        self.assertEqual(RateLimited("x").code, "rate_limited")
        self.assertEqual(CircuitOpen("x").code, "circuit_open")

    def test_override_retryable(self):
        error = ToolExecutionError("nope", retryable=False)
        self.assertFalse(error.retryable)

    def test_to_dict(self):
        error = ValidationError("bad")
        payload = error.to_dict()
        self.assertEqual(payload["code"], "validation_error")
        self.assertFalse(payload["retryable"])
        self.assertIn("details", payload)


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.registry = ToolRegistry()

        @tool(name="forecast", description="查询天气", version="2.1.0", tags=("weather", "read"))
        def get_forecast(city: str, days: int = 1) -> dict:
            """内部 docstring 会被装饰器参数覆盖。

            Args:
                city: 城市
                days: 天数
            """
            return {"city": city, "days": days}

        @tool(tags=("write",))
        def send_alert(message: str) -> str:
            """发送告警。

            Args:
                message: 内容
            """
            return message

        self.registry.register(get_forecast)
        self.registry.register(send_alert, deprecated=True, deprecation_note="请改用 forecast")
        self.registry.register(lambda: "legacy", name="legacy", description="老接口")
        self.registry.tools["legacy"].deprecated = True

    def test_names_sorted(self):
        self.assertEqual(self.registry.names(), ["forecast", "legacy", "send_alert"])

    def test_decorator_metadata_preserved(self):
        spec = self.registry.get("forecast")
        self.assertEqual(spec.name, "forecast")
        self.assertEqual(spec.description, "查询天气")
        self.assertEqual(spec.version, "2.1.0")
        self.assertEqual(spec.tags, ("weather", "read"))

    def test_get_unknown_raises(self):
        with self.assertRaises(ToolNotFound) as ctx:
            self.registry.get("nope")
        self.assertIn("可用工具", str(ctx.exception))

    def test_schemas_exclude_deprecated(self):
        names = [s["function"]["name"] for s in self.registry.schemas()]
        self.assertEqual(names, ["forecast"], "废弃工具不应下发给模型")

    def test_deprecated_schema_is_marked_when_requested_directly(self):
        spec = self.registry.tools["send_alert"]
        self.assertTrue(spec.schema()["function"]["description"].startswith("[已废弃]"))

    def test_by_tag(self):
        self.assertEqual([s.name for s in self.registry.by_tag("write")], ["send_alert"])

    def test_deprecated_tools(self):
        self.assertEqual({s.name for s in self.registry.deprecated_tools()},
                         {"send_alert", "legacy"})

    def test_summary_includes_signature_and_version(self):
        text = self.registry.get("forecast").summary()
        self.assertIn("forecast(city: string, days: integer =?)", text)
        self.assertIn("v2.1.0", text)

    def test_summary_marks_deprecated(self):
        self.assertIn("[deprecated]", self.registry.tools["legacy"].summary())

    def test_warn_if_deprecated(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.registry.warn_if_deprecated("send_alert")
        self.assertEqual(len(caught), 1)
        self.assertIn("请改用 forecast", str(caught[0].message))

    def test_warn_if_not_deprecated_is_silent(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.registry.warn_if_deprecated("forecast")
        self.assertEqual(caught, [])

    def test_len_and_contains(self):
        self.assertEqual(len(self.registry), 3)
        self.assertIn("forecast", self.registry)
        self.assertNotIn("nope", self.registry)

    def test_describe(self):
        text = self.registry.describe()
        self.assertIn("forecast", text)
        self.assertIn("send_alert", text)

    def test_policy_attached(self):
        policy = ToolPolicy(retry=RetryPolicy(max_attempts=7), idempotent=False)
        self.registry.register(lambda x: x, name="withpolicy", policy=policy)
        self.assertEqual(self.registry.get("withpolicy").policy.effective_attempts(), 1)


if __name__ == "__main__":
    unittest.main()
