"""Action tool runtime 用の引数正規化 helper。"""

from __future__ import annotations

from collections.abc import Mapping

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.utils.strict_numbers import is_strict_int

ToolArgs = Mapping[str, JSONValue]


def json_object_copy(value: Mapping[object, object]) -> dict[str, JSONValue]:
    copied: dict[str, JSONValue] = {}
    for key, item in value.items():
        copied[str(key)] = to_json_value(item)
    return copied


def optional_object_arg(args: ToolArgs, key: str) -> dict[str, JSONValue] | None:
    value = args.get(key)
    if not isinstance(value, dict):
        return None
    return dict(value)


def optional_string_arg(args: ToolArgs, key: str) -> str | None:
    value = args.get(key)
    return value if isinstance(value, str) else None


def require_string_arg(args: ToolArgs, key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str):
        raise RuntimeError(f"{key} must be a string after validation")
    return value


def require_int_arg(args: ToolArgs, key: str, default: int) -> int:
    value = args.get(key, default)
    if not is_strict_int(value):
        raise RuntimeError(f"{key} must be an integer after validation")
    return value


def optional_string_list_arg(args: ToolArgs, key: str) -> list[str] | None:
    value = args.get(key)
    if not isinstance(value, list):
        return None
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise RuntimeError(f"{key} must contain only strings after validation")
        items.append(item)
    return items


def require_string_list_arg(args: ToolArgs, key: str) -> list[str]:
    value = optional_string_list_arg(args, key)
    if value is None:
        raise RuntimeError(f"{key} must be a string list after validation")
    return value


def optional_int_value(value: object) -> int | None:
    return value if is_strict_int(value) else None


def to_json_value(value: object) -> JSONValue:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, tuple | list):
        return [to_json_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): to_json_value(v) for k, v in value.items()}
    return str(value)
