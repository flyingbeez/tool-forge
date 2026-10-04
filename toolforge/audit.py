"""审计日志：每一次工具调用都要留下可追溯、可脱敏的记录。

为什么 Agent 系统比普通后端更需要对工具调用做审计？

1. **工具调用是有副作用的动作**。模型自己决定调什么、传什么参数 ——
   等于一个不可预测的调用方在触发你的业务接口。出了事必须能回放。
2. **调试需要的不是"报错了"，而是"模型传了什么"**。日志里必须有入参。
3. **入参里经常带敏感信息**（token、手机号、身份证）。所以必须有脱敏，且**默认脱敏**。

设计取舍：
* 结果只记 **预览**（截断），不记全文 —— 审计日志的目标是"可追溯"，不是备份数据。
* 格式用 **JSONL**：一行一条，便于 `grep`，也便于后续 ingest 到 ELK / ClickHouse。
* 内存里保留环形缓冲，方便测试与本地排查；写盘是可选的。
"""

from __future__ import annotations

import io
import json
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

__all__ = ["AuditRecord", "AuditLog", "redact", "SENSITIVE_KEY_PATTERNS"]

# 键名里出现这些片段就脱敏
SENSITIVE_KEY_PATTERNS: tuple[str, ...] = (
    "password", "passwd", "secret", "token", "api_key", "apikey", "authorization",
    "auth", "credential", "private_key", "access_key", "session", "cookie",
    "手机", "phone", "id_card", "身份证", "bank", "card_no",
)

MASK = "***REDACTED***"


def _is_sensitive(key: str) -> bool:
    low = key.lower()
    return any(pattern in low for pattern in SENSITIVE_KEY_PATTERNS)


def redact(value: Any, *, extra_keys: Iterable[str] = (), depth: int = 0) -> Any:
    """递归脱敏：命中敏感键名的值替换为掩码。

    Args:
        value: 任意 JSON 兼容结构。
        extra_keys: 调用方额外指定的敏感键名（精确匹配）。
        depth: 内部递归深度，超过 8 层停止深挖（防止自引用结构炸栈）。
    """
    if depth > 8:
        return "<max-depth>"

    extra = {k.lower() for k in extra_keys}

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and (key.lower() in extra or _is_sensitive(key)):
                out[key] = MASK
            else:
                out[key] = redact(item, extra_keys=extra, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(item, extra_keys=extra, depth=depth + 1) for item in value]
    return value


@dataclass
class AuditRecord:
    """一次工具调用尝试的审计记录。"""

    ts: float
    correlation_id: str
    tool: str
    attempt: int
    ok: bool
    error_code: str = ""
    error_message: str = ""
    duration_ms: float = 0.0
    args: Any = None
    result_preview: str = ""
    circuit_state: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, default=str)


@dataclass
class AuditLog:
    """审计日志写入器。

    Args:
        path: JSONL 文件路径；None 表示只保留在内存。
        redact_keys: 额外的敏感键名。
        max_preview: 结果预览的最大字符数。
        memory_limit: 内存中保留多少条（环形缓冲）。
    """

    path: str | None = None
    redact_keys: tuple[str, ...] = ()
    max_preview: int = 200
    memory_limit: int = 1000
    records: deque[AuditRecord] = field(default_factory=lambda: deque(maxlen=1000))
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        self.records = deque(maxlen=self.memory_limit)
        if self.path:
            parent = os.path.dirname(os.path.abspath(self.path))
            if parent:
                os.makedirs(parent, exist_ok=True)

    # ------------------------------------------------------------------ #

    def write(self, record: AuditRecord) -> AuditRecord:
        with self._lock:
            self.records.append(record)
            if self.path:
                with io.open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(record.to_json() + "\n")
        return record

    def record(self, *, tool: str, attempt: int, ok: bool, args: Any = None,
               result: Any = None, error_code: str = "", error_message: str = "",
               duration_ms: float = 0.0, correlation_id: str = "",
               circuit_state: str = "", extra_keys: Iterable[str] = ()) -> AuditRecord:
        merged_keys = tuple(self.redact_keys) + tuple(extra_keys)

        preview = ""
        if result is not None:
            # 返回值同样要脱敏：工具经常把敏感字段原样回传
            # （例如 charge() 的返回值里带着订单号），只脱敏入参是不够的。
            safe_result = redact(result, extra_keys=merged_keys)
            text = (safe_result if isinstance(safe_result, str)
                    else json.dumps(safe_result, ensure_ascii=False, default=str))
            preview = text if len(text) <= self.max_preview else text[: self.max_preview] + "…"

        return self.write(AuditRecord(
            ts=time.time(),
            correlation_id=correlation_id,
            tool=tool,
            attempt=attempt,
            ok=ok,
            error_code=error_code,
            error_message=error_message,
            duration_ms=round(duration_ms, 3),
            args=redact(args, extra_keys=merged_keys),
            result_preview=preview,
            circuit_state=circuit_state,
        ))

    # ------------------------------------------------------------------ #

    def all(self) -> list[AuditRecord]:
        with self._lock:
            return list(self.records)

    def for_tool(self, tool: str) -> list[AuditRecord]:
        return [r for r in self.all() if r.tool == tool]

    def for_correlation(self, correlation_id: str) -> list[AuditRecord]:
        return [r for r in self.all() if r.correlation_id == correlation_id]

    def clear(self) -> None:
        with self._lock:
            self.records.clear()

    def render(self, limit: int = 20) -> str:
        lines = []
        for r in self.all()[-limit:]:
            flag = "OK " if r.ok else "ERR"
            detail = r.result_preview if r.ok else f"{r.error_code}: {r.error_message}"
            lines.append(f"[{r.correlation_id[:8]}] #{r.attempt} {r.tool:<14} {flag} "
                         f"{r.duration_ms:7.1f}ms  {detail}")
        return "\n".join(lines) if lines else "（无审计记录）"
