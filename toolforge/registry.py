"""工具注册表：把函数登记成"有版本、有标签、有策略、可废弃"的工具。"""

from __future__ import annotations

import inspect
import warnings
from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import ToolNotFound
from .policy import ToolPolicy
from .schema import build_schema

__all__ = ["ToolSpec", "ToolRegistry", "tool"]


@dataclass
class ToolSpec:
    """一个工具的完整声明。"""

    name: str
    description: str
    parameters: dict
    func: Callable[..., Any]
    version: str = "1.0.0"
    deprecated: bool = False
    deprecation_note: str = ""
    tags: tuple[str, ...] = ()
    policy: ToolPolicy = field(default_factory=ToolPolicy)
    sensitive_args: tuple[str, ...] = ()
    """参数名列表：审计日志里这些参数会被脱敏（例如手机号、身份证）。"""

    def schema(self) -> dict:
        """OpenAI / DeepSeek 兼容的工具声明。"""
        payload = {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }
        if self.deprecated:
            payload["description"] = f"[已废弃] {payload['description']}"
        return {"type": "function", "function": payload}

    def summary(self) -> str:
        props = self.parameters.get("properties", {})
        required = set(self.parameters.get("required", []))
        rendered = []
        for key, spec in props.items():
            piece = f"{key}: {spec.get('type', 'any')}"
            if key not in required:
                piece += " =?"
            rendered.append(piece)
        suffix = " [deprecated]" if self.deprecated else ""
        return f"{self.name}({', '.join(rendered)}) v{self.version}{suffix}"


@dataclass
class ToolRegistry:
    """工具注册表。"""

    tools: dict[str, ToolSpec] = field(default_factory=dict)

    # -- 注册 ------------------------------------------------------------ #

    def register(self, func: Callable[..., Any] | None = None, *, spec: ToolSpec | None = None,
                 name: str | None = None, description: str | None = None,
                 version: str | None = None, tags: tuple[str, ...] | None = None,
                 deprecated: bool | None = None, deprecation_note: str | None = None,
                 policy: ToolPolicy | None = None,
                 sensitive_args: tuple[str, ...] | None = None) -> Any:
        """注册工具。

        两种用法::

            registry.register(my_func, description="...")
            registry.add(ToolSpec(...))

        显式传入的参数优先；没传的从 ``@tool`` 装饰器留下的 ``__tool_meta__`` 里取，
        最后才落到默认值。这样"装饰器声明 + 注册表登记"两处不会打架。
        """
        if spec is not None:
            self.tools[spec.name] = spec
            return spec
        if func is None:
            raise TypeError("register() 需要 func 或 spec")

        meta = getattr(func, "__tool_meta__", None) or {}

        # 装饰器已经把 schema 算好了，直接复用
        cached = getattr(func, "__tool_spec__", None)
        if cached is not None and name is None and description is None:
            schema = cached
        else:
            schema = build_schema(func, name=name, description=description)

        item = ToolSpec(
            name=schema["name"],
            description=schema["description"],
            parameters=schema["parameters"],
            func=func,
            version=version if version is not None else meta.get("version", "1.0.0"),
            deprecated=deprecated if deprecated is not None else bool(meta.get("deprecated", False)),
            deprecation_note=(deprecation_note if deprecation_note is not None
                              else meta.get("deprecation_note", "")),
            tags=tuple(tags if tags is not None else meta.get("tags", ())),
            policy=policy if policy is not None else (meta.get("policy") or ToolPolicy()),
            sensitive_args=tuple(sensitive_args if sensitive_args is not None
                                 else meta.get("sensitive_args", ())),
        )
        self.tools[item.name] = item
        return item

    add = register

    # -- 查询 ------------------------------------------------------------ #

    def get(self, name: str) -> ToolSpec:
        spec = self.tools.get(name)
        if spec is None:
            raise ToolNotFound(
                f"不存在名为 {name!r} 的工具。可用工具：{sorted(self.tools)}"
            )
        return spec

    def __contains__(self, name: object) -> bool:
        return name in self.tools

    def __len__(self) -> int:
        return len(self.tools)

    def names(self) -> list[str]:
        return sorted(self.tools)

    def schemas(self) -> list[dict]:
        """给模型看的工具声明列表；**已废弃的工具默认不下发**，避免模型误用。"""
        return [spec.schema() for spec in self.tools.values() if not spec.deprecated]

    def by_tag(self, tag: str) -> list[ToolSpec]:
        return [spec for spec in self.tools.values() if tag in spec.tags]

    def deprecated_tools(self) -> list[ToolSpec]:
        return [spec for spec in self.tools.values() if spec.deprecated]

    def describe(self) -> str:
        return "\n".join(f"- {spec.summary()}: {spec.description}" for spec in self.tools.values())

    def warn_if_deprecated(self, name: str) -> None:
        spec = self.tools.get(name)
        if spec is not None and spec.deprecated:
            note = f"（{spec.deprecation_note}）" if spec.deprecation_note else ""
            warnings.warn(f"工具 {name!r} 已废弃{note}", DeprecationWarning, stacklevel=3)


def tool(func: Callable[..., Any] | None = None, *, name: str | None = None,
         description: str | None = None, version: str = "1.0.0",
         tags: tuple[str, ...] = (), deprecated: bool = False,
         deprecation_note: str = "", policy: ToolPolicy | None = None,
         sensitive_args: tuple[str, ...] = ()) -> Any:
    """装饰器：把函数标记为工具，并预先把 schema 算好。

    用 ``functools.wraps`` 保留原函数签名，否则 ``inspect.signature`` 会失真。
    """

    def wrap(target: Callable[..., Any]) -> Callable[..., Any]:
        target.__tool_spec__ = build_schema(target, name=name, description=description)  # type: ignore[attr-defined]
        target.__tool_meta__ = {  # type: ignore[attr-defined]
            "version": version,
            "tags": tags,
            "deprecated": deprecated,
            "deprecation_note": deprecation_note,
            "policy": policy,
            "sensitive_args": sensitive_args,
        }
        return target

    if func is not None:
        return wrap(func)
    return wrap


def signature_of(spec: ToolSpec) -> inspect.Signature:  # pragma: no cover - 调试辅助
    return inspect.signature(spec.func)
