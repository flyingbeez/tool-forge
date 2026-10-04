"""参数 Schema 生成与校验。

比常见的"手写 JSON Schema"多做了两件事：

1. **约束不只写在 schema 里，还要真的执行**。
   很多人给模型声明了 `enum` 和 `minimum`，但服务端从不校验 —— 模型不遵守时就直接写进数据库。
   这里把每条约束都实现成校验规则。

2. **错误信息带 JSON 路径**。
   模型最需要的不是"参数错了"，而是"`$.days` 应该是 1-7 的整数，你给了 99"。
   后者模型能自己改对，前者它只会再错一次。

用法::

    from typing import Annotated
    from toolforge import Field, tool

    @tool
    def forecast(city: Annotated[str, Field(description="城市名", min_length=1)],
                 days: Annotated[int, Field(description="预报天数", ge=1, le=7)] = 1) -> dict:
        ...
"""

from __future__ import annotations

import inspect
import json
import re
import types as _types
import typing
from dataclasses import dataclass
from typing import Any, Callable, Literal, get_args, get_origin, get_type_hints

from .errors import ParamError, ValidationError

__all__ = ["Field", "ParamError", "ValidationError", "build_schema", "validate_args", "json_path"]


# --------------------------------------------------------------------------- #
# Field：把约束和描述一起挂在类型注解上
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Field:
    """参数约束声明，配合 ``Annotated[X, Field(...)]`` 使用。"""

    description: str = ""
    ge: float | None = None          # 数值 >=
    le: float | None = None          # 数值 <=
    min_length: int | None = None    # 字符串/数组最小长度
    max_length: int | None = None    # 字符串/数组最大长度
    pattern: str | None = None       # 字符串正则
    enum: tuple[Any, ...] | None = None
    examples: tuple[Any, ...] | None = None


# --------------------------------------------------------------------------- #
# 类型注解 -> JSON Schema
# --------------------------------------------------------------------------- #

_PRIMITIVES: dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}

_MISSING = object()


def _apply_field(schema: dict, field: Field) -> dict:
    if field.description:
        schema["description"] = field.description
    if field.ge is not None:
        schema["minimum"] = field.ge
    if field.le is not None:
        schema["maximum"] = field.le
    if field.min_length is not None:
        schema["minLength"] = field.min_length
    if field.max_length is not None:
        schema["maxLength"] = field.max_length
    if field.pattern is not None:
        schema["pattern"] = field.pattern
    if field.enum is not None:
        schema["enum"] = list(field.enum)
    if field.examples is not None:
        schema["examples"] = list(field.examples)
    return schema


def annotation_to_schema(annotation: Any) -> dict:
    """把 Python 类型注解翻译成 JSON Schema（含 Annotated 里的约束）。"""
    if annotation is inspect.Parameter.empty or annotation is Any:
        return {}

    field: Field | None = None
    metadata = getattr(annotation, "__metadata__", None)
    if metadata:
        for meta in metadata:
            if isinstance(meta, Field):
                field = meta
        # 剥掉 Annotated 外壳，拿到真实类型
        annotation = get_args(annotation)[0]

    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin is typing.Union or origin is _types.UnionType:
        non_none = [a for a in args if a is not type(None)]
        schema = annotation_to_schema(non_none[0]) if len(non_none) == 1 else {
            "anyOf": [annotation_to_schema(a) for a in non_none]
        }
    elif origin is Literal:
        if args and all(isinstance(a, str) for a in args):
            schema = {"type": "string", "enum": list(args)}
        elif args and all(isinstance(a, int) and not isinstance(a, bool) for a in args):
            schema = {"type": "integer", "enum": list(args)}
        else:
            schema = {"enum": list(args)}
    elif origin in (list, typing.List):
        schema = {"type": "array"}
        if args:
            schema["items"] = annotation_to_schema(args[0])
    elif origin in (dict, typing.Dict):
        schema = {"type": "object"}
    elif annotation in _PRIMITIVES:
        schema = {"type": _PRIMITIVES[annotation]}
    else:
        schema = {}

    if field is not None:
        _apply_field(schema, field)
    return schema


def _parse_docstring(doc: str | None) -> tuple[str, dict[str, str]]:
    """解析 Google 风格 docstring，取总体描述与逐参数说明。"""
    if not doc:
        return "", {}
    doc = inspect.cleandoc(doc)
    description: list[str] = []
    params: dict[str, str] = {}
    section: str | None = None
    current: str | None = None
    stops = {"returns", "return", "yields", "raises", "examples", "example", "note", "notes"}

    for raw in doc.splitlines():
        line = raw.strip()
        lowered = line.rstrip(":").lower()
        if lowered in {"args", "arguments", "parameters", "params"}:
            section, current = "args", None
            continue
        if lowered in stops:
            section, current = None, None
            continue
        if section == "args":
            if not line:
                continue
            if ":" in line:
                head, _, tail = line.partition(":")
                pname = head.split("(")[0].strip().lstrip("*")
                if pname.isidentifier():
                    params[pname] = tail.strip()
                    current = pname
                    continue
            if current:
                params[current] = (params[current] + " " + line).strip()
            continue
        if line:
            description.append(line)
    return " ".join(description).strip(), params


def build_schema(func: Callable[..., Any], *, name: str | None = None,
                 description: str | None = None) -> dict:
    """由函数生成 function schema。"""
    signature = inspect.signature(func)
    try:
        hints = get_type_hints(func, include_extras=True)
    except Exception:  # noqa: BLE001 - 注解引用了无法解析的名字时降级
        hints = getattr(func, "__annotations__", {}) or {}

    doc_desc, param_docs = _parse_docstring(func.__doc__)
    properties: dict[str, dict] = {}
    required: list[str] = []

    for pname, param in signature.parameters.items():
        if pname in ("self", "cls"):
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue

        annotation = hints.get(pname, param.annotation)
        prop = annotation_to_schema(annotation)
        if "description" not in prop and pname in param_docs:
            prop["description"] = param_docs[pname]

        properties[pname] = prop
        if param.default is inspect.Parameter.empty:
            required.append(pname)
        else:
            prop["default"] = param.default

    return {
        "name": name or func.__name__,
        "description": description or doc_desc or f"调用 {func.__name__}",
        "parameters": {"type": "object", "properties": properties, "required": required},
    }


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #

def json_path(*parts: Any) -> str:
    """拼一个易读的 JSON 路径，例如 ``$.user.tags[0]``。"""
    out = "$"
    for part in parts:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}"
    return out


def _type_ok(value: Any, wanted: str) -> bool:
    if wanted == "integer":
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        return isinstance(value, float) and value.is_integer()
    if wanted == "number":
        return not isinstance(value, bool) and isinstance(value, (int, float))
    if wanted == "string":
        return isinstance(value, str)
    if wanted == "boolean":
        return isinstance(value, bool)
    if wanted == "array":
        return isinstance(value, list)
    if wanted == "object":
        return isinstance(value, dict)
    return True


def _coerce_value(value: Any, wanted: str | None) -> Any:
    """保守纠偏：只在无歧义时转换。返回原值表示无法纠偏。"""
    if wanted == "integer" and isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return value
    if wanted == "number" and isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return value
    if wanted == "boolean" and isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "yes", "1"):
            return True
        if low in ("false", "no", "0"):
            return False
    if wanted == "integer" and isinstance(value, float) and value.is_integer():
        return int(value)
    if wanted == "array" and isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value
        if isinstance(parsed, list):
            return parsed
    if wanted == "string" and isinstance(value, (int, float, bool)):
        return str(value)
    return value


def _validate_value(value: Any, schema: dict, path: list[Any], errors: list[ParamError],
                    *, coerce: bool) -> Any:
    """递归校验单个值；返回可能被纠偏后的值。"""
    if not schema:
        return value

    wanted = schema.get("type")

    # anyOf：任一分支通过即可
    if "anyOf" in schema:
        branch_errors: list[ParamError] = []
        for branch in schema["anyOf"]:
            trial: list[ParamError] = []
            candidate = _validate_value(value, branch, path, trial, coerce=coerce)
            if not trial:
                return candidate
            branch_errors.extend(trial)
        errors.append(ParamError(json_path(*path), f"不满足任何一种允许的类型（{' / '.join(b.get('type', '?') for b in schema['anyOf'])}）"))
        return value

    original = value
    if wanted and not _type_ok(value, wanted):
        if coerce:
            value = _coerce_value(value, wanted)
    if wanted and not _type_ok(value, wanted):
        errors.append(ParamError(
            json_path(*path),
            f"期望 {wanted}，收到 {type(original).__name__}（值：{original!r}）",
        ))
        return value

    if "enum" in schema and value not in schema["enum"]:
        errors.append(ParamError(json_path(*path), f"取值必须是 {schema['enum']} 之一，收到 {value!r}"))

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(ParamError(json_path(*path), f"不能小于 {schema['minimum']}，收到 {value}"))
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(ParamError(json_path(*path), f"不能大于 {schema['maximum']}，收到 {value}"))

    if isinstance(value, (str, list)):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(ParamError(json_path(*path), f"长度不能小于 {schema['minLength']}，收到 {len(value)}"))
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(ParamError(json_path(*path), f"长度不能大于 {schema['maxLength']}，收到 {len(value)}"))

    if isinstance(value, str) and schema.get("pattern"):
        if not re.search(schema["pattern"], value):
            errors.append(ParamError(json_path(*path), f"不匹配模式 {schema['pattern']!r}，收到 {value!r}"))

    if isinstance(value, list) and schema.get("items"):
        value = [_validate_value(item, schema["items"], path + [i], errors, coerce=coerce)
                 for i, item in enumerate(value)]

    if isinstance(value, dict) and schema.get("properties"):
        cleaned_nested: dict[str, Any] = {}
        nested_required = schema.get("required", [])
        for req in nested_required:
            if req not in value or value[req] is None:
                errors.append(ParamError(json_path(*(path + [req])), "缺少必填字段"))
        for key, item in value.items():
            if key in schema["properties"]:
                cleaned_nested[key] = _validate_value(
                    item, schema["properties"][key], path + [key], errors, coerce=coerce
                )
            elif schema.get("additionalProperties") is False:
                errors.append(ParamError(json_path(*(path + [key])), "不允许的额外字段"))
            else:
                cleaned_nested[key] = item
        value = cleaned_nested

    return value


def validate_args(schema: dict, args: Any, *, coerce: bool = True,
                  strict: bool = False) -> dict:
    """校验并清洗参数；失败抛 ValidationError（附带逐条路径错误）。

    Args:
        schema: ``build_schema()`` 产出的 ``parameters`` 部分。
        args: 原始参数。允许是 dict，或 JSON 字符串。
        coerce: 是否允许 "5" -> 5 这类无歧义纠偏。
        strict: 为 True 时，多余字段也算错误（默认忽略，因为模型多塞字段很常见）。
    """
    if args is None:
        args = {}
    if isinstance(args, str):
        text = args.strip()
        if not text:
            args = {}
        else:
            try:
                args = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValidationError(f"参数不是合法 JSON：{exc.msg}（原文：{text[:120]!r}）") from exc
    if not isinstance(args, dict):
        raise ValidationError(f"参数必须是 JSON 对象，收到 {type(args).__name__}")

    properties: dict = schema.get("properties", {})
    required: list = schema.get("required", [])
    errors: list[ParamError] = []

    for req in required:
        if req not in args or args[req] is None:
            errors.append(ParamError(json_path(req), "缺少必填参数"))

    cleaned: dict[str, Any] = {}
    for key, value in args.items():
        if key not in properties:
            if strict:
                errors.append(ParamError(json_path(key), "未知参数"))
            continue
        cleaned[key] = _validate_value(value, properties[key], [key], errors, coerce=coerce)

    if errors:
        raise ValidationError(errors)
    return cleaned
