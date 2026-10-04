"""错误分类 —— 整个工具层的设计中心。

核心思想：**错误的"能不能重试"必须由错误类型决定，而不是由调用方猜。**

Agent 工具调用失败的原因差别极大：

| 失败原因 | 例子 | 重试有用吗 |
|---|---|---|
| 参数不合法 | 模型把 days 写成 "abc" | ❌ 完全没用，重试一百次还是错 |
| 参数语义错误 | 城市名写错 | ❌ 重试没用，要模型改参数 |
| 网络抖动 / 429 | 上游服务暂时不可用 | ✅ 有用，指数退避能救回来 |
| 超时 | 上游变慢 | ✅ 可能有用 |
| 下游服务挂了 | 连续 5xx | ⚠️ 短期没用，需要熔断 |
| 权限不足 / 认证失败 | 401 | ❌ 重试没用，要人改配置 |

把这张表编码成类型，是这一层最重要的价值：
**对一个 ValidationError 做三次指数退避重试，是新手最常见的浪费。**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ToolError",
    "ValidationError",
    "ToolNotFound",
    "ToolTimeout",
    "ToolExecutionError",
    "RateLimited",
    "CircuitOpen",
    "FatalToolError",
]


@dataclass
class ParamError:
    """单个参数的校验错误，带 JSON 路径，方便模型精确定位。"""

    path: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - 简单展示
        return f"{self.path}: {self.message}"


class ToolError(Exception):
    """所有工具层错误的基类。

    Attributes:
        code: 稳定的机器可读错误码（用于审计日志与告警规则，不要用 message 做判断）。
        retryable: 是否值得重试。子类通过类属性声明默认值。
        details: 附加结构化信息（例如逐条参数错误）。
    """

    code: str = "tool_error"
    retryable: bool = False

    def __init__(self, message: str, *, code: str | None = None,
                 retryable: bool | None = None, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable
        self.details = details

    def to_dict(self) -> dict:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.details is not None:
            payload["details"] = self.details
        return payload


class ValidationError(ToolError):
    """参数校验失败。**永远不要重试** —— 重试不会改变参数。"""

    code = "validation_error"
    retryable = False

    def __init__(self, errors: list[ParamError] | str) -> None:
        if isinstance(errors, str):
            super().__init__(errors, details=[errors])
            self.errors = [ParamError("$", errors)]
            return
        self.errors = errors
        readable = "；".join(str(e) for e in errors) or "参数校验失败"
        super().__init__(f"参数校验失败：{readable}", details=[str(e) for e in errors])


class ToolNotFound(ToolError):
    """工具不存在。重试无用，应该把可用工具清单回灌给模型。"""

    code = "tool_not_found"
    retryable = False


class ToolTimeout(ToolError):
    """调用超时。**可以重试**，但要注意上游可能仍在执行。"""

    code = "timeout"
    retryable = True


class ToolExecutionError(ToolError):
    """工具内部抛异常。默认认为可重试，调用方可以用 retryable=False 覆盖。"""

    code = "execution_error"
    retryable = True


class RateLimited(ToolError):
    """被限流。可以重试，且应当等待。"""

    code = "rate_limited"
    retryable = True


class CircuitOpen(ToolError):
    """熔断器打开，请求被快速失败。

    注意：这里 **retryable=False** 是有意的 —— 熔断器已经判断"重试没意义，
    直接失败比让请求排队更好"，此时再重试就违背了熔断的目的。
    """

    code = "circuit_open"
    retryable = False


class FatalToolError(ToolError):
    """明确不可恢复的错误（鉴权失败、配置错误）。手动标记，覆盖默认值。"""

    code = "fatal"
    retryable = False
