"""审计与指标测试。"""

from __future__ import annotations

import io
import json
import os
import unittest

from toolforge.audit import MASK, AuditLog, redact
from toolforge.metrics import Metrics, ToolStat


class TestRedact(unittest.TestCase):
    def test_flat_sensitive_keys(self):
        payload = {"user": "alice", "password": "p@ss", "api_key": "sk-123"}
        cleaned = redact(payload)
        self.assertEqual(cleaned["user"], "alice")
        self.assertEqual(cleaned["password"], MASK)
        self.assertEqual(cleaned["api_key"], MASK)

    def test_nested(self):
        payload = {"a": {"token": "t", "ok": 1}, "b": [{"secret": "s"}, {"ok": 2}]}
        cleaned = redact(payload)
        self.assertEqual(cleaned["a"]["token"], MASK)
        self.assertEqual(cleaned["a"]["ok"], 1)
        self.assertEqual(cleaned["b"][0]["secret"], MASK)
        self.assertEqual(cleaned["b"][1]["ok"], 2)

    def test_chinese_sensitive_keys(self):
        cleaned = redact({"手机": "13800000000", "身份证": "110101199001011234", "备注": "ok"})
        self.assertEqual(cleaned["手机"], MASK)
        self.assertEqual(cleaned["身份证"], MASK)
        self.assertEqual(cleaned["备注"], "ok")

    def test_extra_keys(self):
        cleaned = redact({"nickname": "x"}, extra_keys=("nickname",))
        self.assertEqual(cleaned["nickname"], MASK)

    def test_case_insensitive(self):
        self.assertEqual(redact({"Authorization": "Bearer x"})["Authorization"], MASK)

    def test_depth_limit(self):
        payload: dict = {}
        cursor = payload
        for _ in range(20):
            cursor["child"] = {}
            cursor = cursor["child"]
        cursor["password"] = "deep"
        # 超过深度就不再深挖，不会抛异常也不会泄露
        result = redact(payload)
        self.assertIsInstance(result, dict)

    def test_scalars_pass_through(self):
        self.assertEqual(redact("plain"), "plain")
        self.assertEqual(redact(42), 42)
        self.assertIsNone(redact(None))


class TestAuditLog(unittest.TestCase):
    def setUp(self):
        self.log = AuditLog(memory_limit=10)

    def test_record_and_query(self):
        self.log.record(tool="a", attempt=1, ok=True, args={"x": 1}, result="ok",
                        correlation_id="c1")
        self.log.record(tool="b", attempt=1, ok=False, error_code="timeout",
                        error_message="超时", correlation_id="c1")
        self.assertEqual(len(self.log.all()), 2)
        self.assertEqual(len(self.log.for_tool("a")), 1)
        self.assertEqual(len(self.log.for_correlation("c1")), 2)

    def test_result_is_truncated(self):
        self.log.record(tool="a", attempt=1, ok=True, result="x" * 1000)
        self.assertLess(len(self.log.all()[0].result_preview), 260)
        self.assertTrue(self.log.all()[0].result_preview.endswith("…"))

    def test_memory_limit_is_ring_buffer(self):
        for i in range(25):
            self.log.record(tool="a", attempt=i, ok=True)
        self.assertEqual(len(self.log.all()), 10)
        self.assertEqual(self.log.all()[-1].attempt, 24)

    def test_clear(self):
        self.log.record(tool="a", attempt=1, ok=True)
        self.log.clear()
        self.assertEqual(self.log.all(), [])

    def test_jsonl_file_output(self):
        # 不用临时目录：某些受限环境不允许往"刚创建的子目录"里写文件。
        # 直接在已有的 tests/ 目录下建一个唯一名字的日志文件，用完删掉。
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            f"tmp_audit_{os.getpid()}.jsonl")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        log = AuditLog(path=path)
        log.record(tool="a", attempt=1, ok=True, args={"password": "x", "city": "北京"},
                   correlation_id="c")
        log.record(tool="a", attempt=2, ok=False, error_code="e", error_message="m",
                   correlation_id="c")
        with io.open(path, encoding="utf-8") as handle:
            lines = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["tool"], "a")
        self.assertEqual(lines[0]["args"]["password"], MASK, "落盘前必须脱敏")
        self.assertEqual(lines[0]["args"]["city"], "北京")
        self.assertEqual(lines[1]["error_code"], "e")

    def test_result_preview_is_also_redacted(self):
        # 工具经常把敏感字段原样回传（例如扣款接口返回订单号），只脱敏入参是不够的
        self.log.record(tool="charge", attempt=1, ok=True,
                        args={"order_id": "ORD-1001"},
                        result={"order_id": "ORD-1001", "status": "paid"},
                        extra_keys=("order_id",))
        preview = self.log.all()[0].result_preview
        self.assertNotIn("ORD-1001", preview)
        self.assertIn(MASK, preview)
        self.assertIn("paid", preview)

    def test_render(self):
        self.log.record(tool="a", attempt=1, ok=True, result="v", correlation_id="1234567890")
        self.log.record(tool="b", attempt=1, ok=False, error_code="timeout",
                        error_message="慢", correlation_id="1234567890")
        text = self.log.render()
        self.assertIn("a", text)
        self.assertIn("timeout: 慢", text)

    def test_render_empty(self):
        self.assertIn("无审计记录", self.log.render())


class TestMetrics(unittest.TestCase):
    def setUp(self):
        self.metrics = Metrics()

    def test_counters(self):
        self.metrics.start_call("t")
        self.metrics.record_attempt("t", duration_ms=10)
        self.metrics.record_success("t")
        self.metrics.record_retry("t")
        self.metrics.start_call("t")
        self.metrics.record_attempt("t", duration_ms=30)
        self.metrics.record_failure("t", "timeout")

        snap = self.metrics.snapshot()["t"]
        self.assertEqual(snap["calls"], 2)
        self.assertEqual(snap["attempts"], 2)
        self.assertEqual(snap["successes"], 1)
        self.assertEqual(snap["failures"], 1)
        self.assertEqual(snap["retries"], 1)
        self.assertEqual(snap["timeouts"], 1)
        self.assertEqual(snap["success_rate"], 0.5)

    def test_percentiles(self):
        for value in range(1, 101):
            self.metrics.record_attempt("t", duration_ms=float(value))
        snap = self.metrics.snapshot()["t"]
        self.assertAlmostEqual(snap["p50_ms"], 50.5, places=2)
        self.assertAlmostEqual(snap["p95_ms"], 95.05, places=2)
        self.assertAlmostEqual(snap["p99_ms"], 99.01, places=2)
        self.assertAlmostEqual(snap["mean_ms"], 50.5, places=2)

    def test_error_code_buckets(self):
        for code, field in (("timeout", "timeouts"), ("rate_limited", "rate_limited"),
                            ("circuit_open", "circuit_rejected"),
                            ("validation_error", "validation_errors")):
            self.metrics.start_call("t")
            self.metrics.record_failure("t", code)
            self.assertEqual(self.metrics.snapshot()["t"][field], 1, code)

    def test_single_value_percentile(self):
        stat = ToolStat()
        stat.latencies_ms.append(7.0)
        self.assertEqual(stat.latency(0.5), 7.0)
        self.assertEqual(stat.latency(0.99), 7.0)

    def test_empty_percentile_is_zero(self):
        self.assertEqual(ToolStat().latency(0.95), 0.0)

    def test_totals(self):
        for tool in ("a", "b"):
            self.metrics.start_call(tool)
            self.metrics.record_attempt(tool, duration_ms=5)
            self.metrics.record_success(tool)
        self.assertEqual(self.metrics.totals()["calls"], 2)
        self.assertEqual(self.metrics.totals()["successes"], 2)

    def test_reset(self):
        self.metrics.start_call("a")
        self.metrics.reset()
        self.assertEqual(self.metrics.snapshot(), {})

    def test_render_table_empty(self):
        self.assertIn("暂无指标", self.metrics.render_table())

    def test_render_table_markdown(self):
        self.metrics.start_call("alpha")
        self.metrics.record_attempt("alpha", duration_ms=12.5)
        self.metrics.record_success("alpha")
        table = self.metrics.render_table()
        lines = table.strip().splitlines()
        self.assertTrue(lines[0].startswith("| 工具 |"))
        self.assertIn("`alpha`", table)
        self.assertEqual(len(lines), 4)  # 表头 + 分隔 + 一行 + 合计


if __name__ == "__main__":
    unittest.main()
