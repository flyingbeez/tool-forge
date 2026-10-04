"""toolforge —— 生产级 Function Calling 工具层。

和 mini-react-agent 的分工：
    mini-react-agent  解决"Agent 循环怎么写"
    tool-forge        解决"工具调用怎么才能不把线上搞挂"

模块地图：
    errors.py    错误分类（核心：retryable 由错误类型决定，不由调用方猜）
    schema.py    Field 约束 + JSON Schema 生成 + 带路径的校验错误
    policy.py    重试 / 超时 / 令牌桶限流 / 三态熔断
    audit.py     JSONL 审计日志 + 递归脱敏
    metrics.py   调用数/成功率/重试率/P50/P95/P99
    registry.py  工具注册表：版本、标签、废弃、敏感参数
    executor.py  执行流水线：查表→校验→限流→熔断→执行→重试→审计→指标
"""

from .audit import AuditLog, AuditRecord, redact
from .errors import (
    CircuitOpen,
    FatalToolError,
    ParamError,
    RateLimited,
    ToolError,
    ToolExecutionError,
    ToolNotFound,
    ToolTimeout,
    ValidationError,
)
from .executor import ToolExecutor, ToolResult
from .metrics import Metrics, ToolStat
from .policy import (
    CircuitBreaker,
    CircuitBreakerPolicy,
    CircuitState,
    RateLimitPolicy,
    RetryPolicy,
    TimeoutPolicy,
    TokenBucket,
    ToolPolicy,
)
from .registry import ToolRegistry, ToolSpec, tool
from .schema import Field, build_schema, validate_args

__version__ = "0.1.0"

__all__ = [
    # 装饰器与主要入口
    "tool",
    "Field",
    "ToolRegistry",
    "ToolSpec",
    "ToolExecutor",
    "ToolResult",
    # 策略
    "ToolPolicy",
    "RetryPolicy",
    "TimeoutPolicy",
    "RateLimitPolicy",
    "CircuitBreakerPolicy",
    "CircuitBreaker",
    "CircuitState",
    "TokenBucket",
    # 观测
    "AuditLog",
    "AuditRecord",
    "Metrics",
    "ToolStat",
    "redact",
    # 错误
    "ToolError",
    "ValidationError",
    "ParamError",
    "ToolNotFound",
    "ToolTimeout",
    "ToolExecutionError",
    "RateLimited",
    "CircuitOpen",
    "FatalToolError",
    # 函数
    "build_schema",
    "validate_args",
    "__version__",
]
