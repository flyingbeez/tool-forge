# tool-forge

> 生产级 Function Calling 工具层：**重试 / 超时 / 限流 / 熔断 / 审计 / 指标** —— 零第三方依赖。

[![tests](https://github.com/flyingbeez/tool-forge/actions/workflows/test.yml/badge.svg)](https://github.com/flyingbeez/tool-forge/actions/workflows/test.yml)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![deps](https://img.shields.io/badge/dependencies-0-brightgreen)
![tests](https://img.shields.io/badge/tests-112%20passed-success)

---

## 它解决什么问题

在 Agent 系统里，**工具调用是有副作用的动作**。模型自己决定调什么、传什么参数，
等于一个不可预测的调用方在触发你的业务接口。于是这些问题一定会出现：

- 模型把 `days` 传成 `"abc"`，你的代码抛异常，整个 Agent 挂掉；
- 上游接口抖了一下，调用失败，但重试一次就好了 —— 可你没重试；
- 你加了重试，结果**参数错误也重试了三次**，白等 3 秒还烧了三次配额；
- 下游整体挂了，请求全都在超时重试，把你自己的线程池堆满；
- 线上出问题要复盘，但你不知道模型到底传了什么参数；
- 面试官问"你怎么知道工具层好不好用"，你答不出一个数字。

这个库把这些一次性解决。**核心设计只有一句话：**

> ### `retryable` 由**错误类型**决定，不由调用方猜。

| 失败原因 | 例子 | 重试有用吗 | 本库的处理 |
|---|---|---|---|
| 参数不合法 | `days = "abc"` | ❌ 完全没用 | `ValidationError(retryable=False)`，**尝试次数 0** |
| 语义错误 | 城市名写错 | ❌ 重试没用 | 同上，把错误路径回灌给模型改 |
| 网络抖动 / 429 | 上游暂时不可用 | ✅ 有用 | `ToolExecutionError` / `RateLimited` → 指数退避 |
| 超时 | 上游变慢 | ✅ 可能有用 | `ToolTimeout(retryable=True)` |
| 下游整体挂了 | 连续 5xx | ⚠️ 短期没用 | **熔断**，`CircuitOpen(retryable=False)` 快速失败 |
| 代码写错了 | `ValueError` / `TypeError` | ❌ 没用 | 判定为不可重试，避免掩盖 bug |
| 非幂等写操作 | 下单、扣款 | ❌ **危险** | `idempotent=False` → 强制只尝试 1 次 |

## 架构

```
                      call(tool, args)
                            │
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
    ┌────────┐        ┌──────────┐       ┌───────────┐
    │ 查表    │        │ 参数校验  │       │  限流      │
    │registry│        │ schema.py│       │TokenBucket│
    └────┬───┘        └────┬─────┘       └─────┬─────┘
         │ 不存在           │ 失败              │ 无令牌
         ▼                 ▼                   ▼
   ToolNotFound      ValidationError      RateLimited
   (不重试)           (不重试, 尝试0次)     (可重试)
                            │
                            ▼
                     ┌─────────────┐
                     │   熔断器     │ ← CircuitBreaker: closed/open/half_open
                     └──────┬──────┘
                            │ 放行
                            ▼
              ┌──────────────────────────────┐
              │  执行（带超时）                │
              │  ThreadPool + future.result   │
              └──────────────┬───────────────┘
                             │
                  失败 → _classify(exc) → retryable?
                             │
              ┌──────────────┴──────────────┐
              │ 是                          │ 否
              ▼                             ▼
      退避等待 → 重试                终止，返回结构化错误
      (指数退避 + 抖动)                     │
              │                             │
              └──────────┬──────────────────┘
                         ▼
              ┌──────────────────────┐
              │ 审计日志 + 指标        │  ← 每次尝试都记
              │ AuditLog / Metrics   │
              └──────────────────────┘
```

## 快速开始

```bash
git clone https://github.com/flyingbeez/tool-forge.git
cd tool-forge

python run_tests.py                            # 112 个测试，约 0.2 秒，零依赖
python examples/01_retry_and_timeout.py        # 重试 / 不重试 / 超时
python examples/02_circuit_breaker.py          # 熔断三态状态机
python examples/03_audit_and_metrics.py        # 审计日志 + 指标表
```

**不需要任何 API Key，不需要联网，不需要安装依赖。**

## 三行接入你自己的工具

```python
from typing import Annotated
from toolforge import Field, ToolRegistry, ToolExecutor, tool

@tool
def forecast(city: Annotated[str, Field(description="城市名", min_length=1)],
             days: Annotated[int, Field(description="预报天数", ge=1, le=7)] = 1) -> dict:
    """查询指定城市未来几天的天气。"""
    return {"city": city, "days": days, "temp_c": 26}

registry = ToolRegistry()
registry.register(forecast)

with ToolExecutor(registry) as executor:
    result = executor.call("forecast", {"city": "北京", "days": 3})
    print(result.value if result.ok else result.error_message)
```

`Annotated[str, Field(...)]` 里的约束**既会写进给模型看的 JSON Schema，也会在服务端真的执行** ——
很多人只声明不校验，模型不遵守时就直接写进数据库了。

## 评测结果（真实输出，不是编的）

用 `examples/03_audit_and_metrics.py` 跑一轮混合负载：49 次逻辑调用（含并发），
四个模拟下游分别有 20% / 12% / 25% / 10% 的失败率。

判定用的是**内容寻址的确定性伪随机数**（工具名 + 参数 + 第几次尝试的哈希），
而不是 `random.seed()` —— 因为负载是并发执行的，共享随机数发生器的消费顺序由线程调度决定，
结果不可复现。改成内容寻址后，任何人运行都会得到同样的调用数、失败数和重试数。

| 工具 | 调用 | 成功 | 失败 | 重试 | 超时 | 限流 | 熔断 | 成功率 | P50(ms) | P95(ms) |
|---|---|---|---|---|---|---|---|---|---|---|
| `charge` | 9 | 8 | 1 | 0 | 0 | 0 | 0 | 88.9% | 15.5 | 16.0 |
| `flaky_embed` | 12 | 12 | 0 | 2 | 0 | 0 | 0 | 100.0% | 16.0 | 21.2 |
| `get_weather` | 12 | 12 | 0 | 0 | 0 | 0 | 0 | 100.0% | 0.0 | 16.0 |
| `search_docs` | 10 | 10 | 0 | 1 | 0 | 0 | 0 | 100.0% | 31.0 | 47.0 |
| `summarize` | 6 | 6 | 0 | 0 | 0 | 0 | 0 | 100.0% | 15.0 | 15.0 |
| **合计** | **49** | **48** | **1** | **3** | 0 | 0 | 0 | **98.0%** | 16.0 | 47.0 |

**这张表里最有信息量的一行是 `charge`。**

它是**非幂等**的写接口（扣款），所以 `idempotent=False` 强制它只尝试一次。
结果是：其他三个工具的失败全部被重试救回来了（成功率 100%），
只有 `charge` 保留了真实的 88.9% —— 因为**重试一个已经产生副作用的调用，比失败更糟**。

示例脚本还会做不变量自检，这两条在每次运行都必须成立：

```
实际尝试 51 次 = 通过校验的调用 48 次 + 重试 3 次  ✓
审计记录 52 条 = 实际尝试 51 条 + 校验失败 1 条  ✓
```

## 技术选型：我考虑过但放弃的方案

**1. 用 `signal.alarm` 实现超时 → 放弃，改用线程池**

`signal.alarm` 只在 Unix 主线程可用，Windows 上直接不可用。
改用 `ThreadPoolExecutor` + `future.result(timeout=...)`。
代价必须说清楚：**Python 杀不掉线程**，超时后被调函数仍可能在后台跑完。
所以超时保护的是**调用方**（不挂死），不是被调方（不浪费资源）。这是已知局限，不是 bug。

**2. 每次失败尝试都上报熔断器 → 放弃，改成按"逻辑调用"上报一次**

最初每次失败尝试都调 `breaker.record_failure()`。
结果一次 `max_attempts=3` 的失败调用就给熔断器记了 3 次失败，
阈值 3 的熔断器被**单次调用**打开 —— 一次抖动就熔断了整个下游。
改成只在逻辑调用最终失败时记 1 次。

**3. 用 `OSError` 作为"可重试异常" → 放弃，改成显式白名单**

`OSError` 太宽：`FileNotFoundError`、`PermissionError` 这类确定性错误也会被当成可重试，
于是会对着一个永远打不开的文件重试三次。现在用显式白名单
（`TimeoutError` / `ConnectionError` / `ConnectionResetError` / `ConnectionAbortedError` / `BrokenPipeError`），
未知异常默认**不重试** —— 宁可少重试，也不要在有副作用的工具上重复执行。

**4. 直接上 LangChain / 官方 SDK → 本项目不用，但生产建议用**

这个库的目的是把工具层的**机制**讲清楚并做成可测试的。真实业务里我仍然会用成熟框架，
因为它们的生态与可观测性更完整。但遇到"重试风暴""熔断抖动""成本失控"这类问题，
只有理解机制才能定位。

## 已知局限（主动写出来）

1. **超时杀不掉线程**：超时只保护调用方，被调函数可能继续跑完并占用线程。
   想真正中断，需要把工具改成异步（`asyncio` + `wait_for`）或可取消的进程池。
2. **线程池可能被慢工具占满**：`max_workers` 是硬上限，若大量工具同时卡住，
   后续请求会排队。生产环境应该配合信号量 + 队列长度告警。
3. **熔断器是进程内的**：多实例部署时每个进程各有各的熔断状态，
   会出现"一个实例已熔断、另一个还在打下游"的情况。跨实例需要 Redis 等共享存储。
4. **限流是单机的**：令牌桶在进程内存里，多副本时实际 QPS 是 `rate × 副本数`。
5. **审计日志是本地文件**：高并发下应该改成异步写队列或直接发到日志采集器，
   否则磁盘 I/O 会变成瓶颈。
6. **没有覆盖"幂等键"机制**：目前只做"非幂等就不重试"。
   更好的做法是让调用方传 `idempotency_key`，由下游去重，这样非幂等操作也能安全重试。

## 目录结构

```
toolforge/
├── errors.py      错误分类 —— 整个库的设计中心，retryable 的定义处
├── schema.py      Field 约束 / JSON Schema 生成 / 带 JSON 路径的校验错误
├── policy.py      重试 / 超时 / 令牌桶 / 熔断三态状态机
├── audit.py       JSONL 审计日志 + 递归脱敏（入参和返回值都脱敏）
├── metrics.py     调用数 / 成功率 / 重试率 / P50 / P95 / P99
├── registry.py    工具注册表：版本、标签、废弃、敏感参数
└── executor.py    执行流水线（上面那张图的代码实现）
examples/          3 个可运行示例，无需 API Key
tests/             112 个测试
docs/              原理讲解 / 代码导读 / 面试问答
```

## 延伸阅读

- [docs/01-生产级工具层要解决什么.md](docs/01-生产级工具层要解决什么.md) —— 为什么需要这些机制
- [docs/02-代码导读.md](docs/02-代码导读.md) —— 逐模块导读与关键取舍
- [docs/03-面试问答.md](docs/03-面试问答.md) —— 围绕本项目的 18 个面试问题与答法
- 姊妹项目：[mini-react-agent](https://github.com/flyingbeez/mini-react-agent) —— 从零实现 Agent 循环

## License

MIT
