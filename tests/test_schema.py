"""Schema 与校验测试。"""

from __future__ import annotations

import unittest
from typing import Annotated, Literal, Optional

from toolforge.schema import Field, build_schema, validate_args
from toolforge import ValidationError, build_schema as build_schema_pkg  # noqa: F401


def schema_of(func) -> dict:
    return build_schema(func)["parameters"]


class TestSchemaGeneration(unittest.TestCase):
    def test_types_and_required(self):
        def f(a: str, b: int, c: float = 1.5, d: bool = False) -> str:
            """演示。

            Args:
                a: 参数 a
                b: 参数 b
            """
            return ""

        schema = schema_of(f)
        props = schema["properties"]
        self.assertEqual(props["a"], {"type": "string", "description": "参数 a"})
        self.assertEqual(props["b"]["type"], "integer")
        self.assertEqual(props["c"]["default"], 1.5)
        self.assertEqual(schema["required"], ["a", "b"])

    def test_field_constraints_land_in_schema(self):
        def f(days: Annotated[int, Field(description="天数", ge=1, le=7)]) -> int:
            """doc"""
            return days

        prop = schema_of(f)["properties"]["days"]
        self.assertEqual(prop["type"], "integer")
        self.assertEqual(prop["minimum"], 1)
        self.assertEqual(prop["maximum"], 7)
        self.assertEqual(prop["description"], "天数")

    def test_field_description_beats_docstring(self):
        def f(x: Annotated[int, Field(description="来自 Field")]) -> int:
            """doc

            Args:
                x: 来自 docstring
            """
            return x

        self.assertEqual(schema_of(f)["properties"]["x"]["description"], "来自 Field")

    def test_optional_and_pep604(self):
        def f(a: Optional[int] = None, b: int | None = None) -> str:
            """doc"""
            return ""

        props = schema_of(f)["properties"]
        self.assertEqual(props["a"]["type"], "integer")
        self.assertEqual(props["b"]["type"], "integer")

    def test_list_items_and_literal(self):
        def f(tags: list[str], mode: Literal["a", "b"] = "a") -> str:
            """doc"""
            return ""

        props = schema_of(f)["properties"]
        self.assertEqual(props["tags"]["items"]["type"], "string")
        self.assertEqual(props["mode"]["enum"], ["a", "b"])

    def test_varargs_ignored(self):
        def f(a: int, *args, **kwargs) -> str:
            """doc"""
            return ""

        self.assertEqual(list(schema_of(f)["properties"]), ["a"])


class TestValidation(unittest.TestCase):
    def setUp(self):
        def f(expression: Annotated[str, Field(description="表达式", min_length=1)],
              precision: Annotated[int, Field(ge=0, le=10)] = 2,
              mode: Literal["fast", "safe"] = "safe") -> str:
            """doc"""
            return expression

        self.schema = schema_of(f)

    def test_ok(self):
        cleaned = validate_args(self.schema, {"expression": "1+1"})
        self.assertEqual(cleaned, {"expression": "1+1"})

    def test_missing_required_reports_path(self):
        with self.assertRaises(ValidationError) as ctx:
            validate_args(self.schema, {})
        self.assertIn("$.expression", str(ctx.exception))
        self.assertIn("缺少必填参数", str(ctx.exception))

    def test_range_violation(self):
        with self.assertRaises(ValidationError) as ctx:
            validate_args(self.schema, {"expression": "1+1", "precision": 99})
        self.assertIn("不能大于 10", str(ctx.exception))

    def test_enum_violation(self):
        with self.assertRaises(ValidationError) as ctx:
            validate_args(self.schema, {"expression": "1+1", "mode": "turbo"})
        self.assertIn("取值必须是", str(ctx.exception))

    def test_min_length(self):
        with self.assertRaises(ValidationError):
            validate_args(self.schema, {"expression": ""})

    def test_coercion(self):
        cleaned = validate_args(self.schema, {"expression": "x", "precision": "3"})
        self.assertEqual(cleaned["precision"], 3)

    def test_coercion_can_be_disabled(self):
        with self.assertRaises(ValidationError):
            validate_args(self.schema, {"expression": "x", "precision": "3"}, coerce=False)

    def test_bad_type_message_includes_actual(self):
        with self.assertRaises(ValidationError) as ctx:
            validate_args(self.schema, {"expression": "x", "precision": "abc"})
        self.assertIn("收到 str", str(ctx.exception))

    def test_bool_is_not_integer(self):
        with self.assertRaises(ValidationError):
            validate_args(self.schema, {"expression": "x", "precision": True})

    def test_float_integral_accepted(self):
        cleaned = validate_args(self.schema, {"expression": "x", "precision": 3.0})
        self.assertEqual(cleaned["precision"], 3)

    def test_json_string_input(self):
        cleaned = validate_args(self.schema, '{"expression": "1+1", "precision": 1}')
        self.assertEqual(cleaned["precision"], 1)

    def test_invalid_json_string(self):
        with self.assertRaises(ValidationError) as ctx:
            validate_args(self.schema, "{expression: 1")
        self.assertIn("不是合法 JSON", str(ctx.exception))

    def test_non_dict(self):
        with self.assertRaises(ValidationError):
            validate_args(self.schema, [1, 2])

    def test_extra_keys_ignored_by_default(self):
        cleaned = validate_args(self.schema, {"expression": "x", "zzz": 1})
        self.assertNotIn("zzz", cleaned)

    def test_extra_keys_strict(self):
        with self.assertRaises(ValidationError):
            validate_args(self.schema, {"expression": "x", "zzz": 1}, strict=True)

    def test_pattern(self):
        def f(code: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]) -> str:
            """doc"""
            return code

        s = schema_of(f)
        self.assertEqual(validate_args(s, {"code": "ABC"})["code"], "ABC")
        with self.assertRaises(ValidationError):
            validate_args(s, {"code": "abc"})

    def test_array_items_validated(self):
        def f(nums: list[int]) -> str:
            """doc"""
            return ""

        s = schema_of(f)
        cleaned = validate_args(s, {"nums": [1, "2", 3]})
        self.assertEqual(cleaned["nums"], [1, 2, 3])

    def test_nested_object_path(self):
        nested = {
            "type": "object",
            "properties": {"profile": {"type": "object", "properties": {"age": {"type": "integer"}},
                                       "required": ["age"]}},
            "required": ["profile"],
        }
        with self.assertRaises(ValidationError) as ctx:
            validate_args(nested, {"profile": {}})
        self.assertIn("$.profile.age", str(ctx.exception))

    def test_array_index_in_path(self):
        nested = {
            "type": "object",
            "properties": {"nums": {"type": "array", "items": {"type": "integer"}}},
            "required": ["nums"],
        }
        with self.assertRaises(ValidationError) as ctx:
            validate_args(nested, {"nums": [1, "x", 3]})
        self.assertIn("$.nums[1]", str(ctx.exception))

    def test_multiple_errors_collected(self):
        with self.assertRaises(ValidationError) as ctx:
            validate_args(self.schema, {"precision": 99, "mode": "turbo"})
        # 缺 expression + precision 超范围 + mode 非法 = 至少 3 条
        self.assertGreaterEqual(len(ctx.exception.errors), 3)

    def test_validation_error_is_not_retryable(self):
        with self.assertRaises(ValidationError) as ctx:
            validate_args(self.schema, {})
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(ctx.exception.code, "validation_error")
        self.assertIsInstance(ctx.exception.details, list)


if __name__ == "__main__":
    unittest.main()
