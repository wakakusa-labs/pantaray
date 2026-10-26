from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Literal

from jsonschema import Draft7Validator, ValidationError  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError  # type: ignore[import-untyped]

from pantaray_llm.contracts.json_value import JSONValue

type SchemaUsage = Literal["structured_output", "tool_parameters"]
type SchemaCompiler = Callable[..., JSONValue]

_OPENAI_UNSUPPORTED_KEYWORDS = frozenset(
    {
        "allOf",
        "not",
        "dependentRequired",
        "dependentSchemas",
        "if",
        "then",
        "else",
    }
)
# The OpenAI strict validator rejects validation keywords outside its documented
# subset (https://developers.openai.com/api/docs/guides/structured-outputs
# #supported-schemas) with HTTP 400. Dropping the keywords below only widens the
# wire schema, and decode() still enforces the canonical schema. minLength,
# maxLength, and pattern stay preserved: the live validator accepts them
# (verified against production schemas via e2e smoke, 2026-09).
_OPENAI_STRIPPED_KEYWORDS = frozenset(
    {
        "additionalItems",
        "contains",
        "maxContains",
        "maxProperties",
        "minContains",
        "minProperties",
        "patternProperties",
        "propertyNames",
        "unevaluatedItems",
        "unevaluatedProperties",
        "uniqueItems",
    }
)
# The exact format allowlist the OpenAI strict validator names in its
# "'<value>' is not a valid format" error; every other format is dropped.
_OPENAI_SUPPORTED_FORMATS = frozenset(
    {
        "date-time",
        "time",
        "date",
        "duration",
        "email",
        "hostname",
        "ipv4",
        "ipv6",
        "uuid",
    }
)
_OPENAI_ROOT_ONE_OF_KEYS = frozenset({"type", "oneOf"})

# The HTTP boundary permits 100 MiB requests, so schema-specific limits must stop
# expensive work earlier: 64 oneOf branches cap pair checks at 2,016, 10,000 nodes
# bound linear validation, and depth 64 stays well below Python's recursion limit.
MAX_PROVIDER_SCHEMA_NODES: Final = 10_000
MAX_PROVIDER_SCHEMA_ONE_OF_BRANCHES: Final = 64
MAX_PROVIDER_SCHEMA_DEPTH: Final = 64


class ProviderSchemaCompilationError(ValueError):
    def __init__(self, *, path: str, keyword: str, message: str) -> None:
        super().__init__(message)
        self.path = path
        self.keyword = keyword


class ProviderSchemaDecodeError(ValueError):
    def __init__(
        self,
        *,
        path: str,
        keyword: str,
        message: str,
        property_names: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.path = path
        self.keyword = keyword
        self.property_names = property_names


@dataclass(frozen=True, slots=True)
class CompiledProviderSchema:
    canonical_schema: dict[str, JSONValue]
    wire_schema: dict[str, JSONValue]
    post_validation_keywords: tuple[str, ...] = ()

    def decode(self, value: JSONValue) -> JSONValue:
        decoded = _decode_openai_value(value, self.canonical_schema)
        error = next(Draft7Validator(self.canonical_schema).iter_errors(decoded), None)
        if error is not None:
            raise ProviderSchemaDecodeError(
                path=schema_error_path(error),
                keyword=str(error.validator or "unknown"),
                message=f"Provider output violates canonical schema: {error.message}",
                property_names=schema_error_property_names(error),
            )
        return decoded


def schema_error_path(error: ValidationError) -> str:
    parts = [str(part) for part in error.absolute_path]
    return ".".join(parts) if parts else "$"


def schema_error_property_names(error: ValidationError) -> tuple[str, ...]:
    """Name the properties a `required` or `additionalProperties` error is about.

    jsonschema's `error.message` embeds the instance value for many keywords, so
    text that reaches the model is composed from these names, never from it.
    """
    if error.validator == "required":
        return tuple(
            name for name in error.validator_value if name not in error.instance
        )
    if error.validator == "additionalProperties":
        declared = error.schema.get("properties", {})
        patterns = error.schema.get("patternProperties", {})
        return tuple(
            name
            for name in error.instance
            if name not in declared
            and not any(re.search(pattern, name) for pattern in patterns)
        )
    return ()


def compile_provider_schema(
    *,
    schema: dict[str, JSONValue],
    usage: SchemaUsage,
) -> CompiledProviderSchema:
    _validate_schema_complexity(schema)
    _validate_canonical_schema(schema=schema, usage=usage)
    if schema.get("type") != "object":
        raise ProviderSchemaCompilationError(
            path="$",
            keyword="type",
            message="OpenAI strict schemas must have an object root.",
        )
    root_union = schema.get("oneOf")
    post_validation_keywords: tuple[str, ...] = ()
    if isinstance(root_union, list):
        _validate_openai_root_union_shape(schema)
        wire_schema = _compile_openai_node(
            _flatten_root_object_union(root_union),
            path="$",
        )
        post_validation_keywords = ("oneOf",)
    else:
        wire_schema = _compile_openai_node(schema, path="$")
    if not isinstance(wire_schema, dict):
        raise AssertionError("compiled root schema must remain an object")
    return CompiledProviderSchema(
        canonical_schema=schema,
        wire_schema=wire_schema,
        post_validation_keywords=post_validation_keywords,
    )


def _validate_schema_complexity(schema: dict[str, JSONValue]) -> None:
    nodes: list[tuple[JSONValue, str, int]] = [(schema, "$", 1)]
    node_count = 0
    while nodes:
        value, path, depth = nodes.pop()
        node_count += 1
        if node_count > MAX_PROVIDER_SCHEMA_NODES:
            raise ProviderSchemaCompilationError(
                path=path,
                keyword="schema",
                message=(
                    "Provider schema exceeds the maximum node count of "
                    f"{MAX_PROVIDER_SCHEMA_NODES}."
                ),
            )
        if depth > MAX_PROVIDER_SCHEMA_DEPTH:
            raise ProviderSchemaCompilationError(
                path=path,
                keyword="schema",
                message=(
                    "Provider schema exceeds the maximum depth of "
                    f"{MAX_PROVIDER_SCHEMA_DEPTH}."
                ),
            )
        if isinstance(value, dict):
            one_of = value.get("oneOf")
            if (
                isinstance(one_of, list)
                and len(one_of) > MAX_PROVIDER_SCHEMA_ONE_OF_BRANCHES
            ):
                raise ProviderSchemaCompilationError(
                    path=f"{path}.oneOf",
                    keyword="oneOf",
                    message=(
                        "Provider schema oneOf exceeds the maximum branch count of "
                        f"{MAX_PROVIDER_SCHEMA_ONE_OF_BRANCHES}."
                    ),
                )
            nodes.extend(
                (item, f"{path}.{key}", depth + 1)
                for key, item in reversed(tuple(value.items()))
            )
        elif isinstance(value, list):
            nodes.extend(
                (item, f"{path}[{index}]", depth + 1)
                for index, item in reversed(tuple(enumerate(value)))
            )


def _validate_openai_root_union_shape(schema: dict[str, JSONValue]) -> None:
    unsupported_keys = sorted(schema.keys() - _OPENAI_ROOT_ONE_OF_KEYS)
    if not unsupported_keys:
        return
    keyword = unsupported_keys[0]
    raise ProviderSchemaCompilationError(
        path=f"$.{keyword}",
        keyword=keyword,
        message=(
            f"OpenAI root oneOf compilation cannot preserve sibling keyword {keyword}."
        ),
    )


def _flatten_root_object_union(
    branches: list[JSONValue],
) -> dict[str, JSONValue]:
    if not _branches_are_pairwise_disjoint(branches):
        raise ProviderSchemaCompilationError(
            path="$.oneOf",
            keyword="oneOf",
            message="Root oneOf branches are not provably disjoint.",
        )
    object_branches: list[dict[str, JSONValue]] = []
    for index, branch in enumerate(branches):
        if not isinstance(branch, dict) or branch.get("type") != "object":
            raise ProviderSchemaCompilationError(
                path=f"$.oneOf[{index}]",
                keyword="type",
                message="OpenAI root oneOf branches must be object schemas.",
            )
        object_branches.append(branch)

    property_options: dict[str, list[JSONValue]] = {}
    required_sets: list[set[str]] = []
    for branch in object_branches:
        properties = branch.get("properties")
        if not isinstance(properties, dict):
            properties = {}
        for name, property_schema in properties.items():
            options = property_options.setdefault(name, [])
            if not any(existing == property_schema for existing in options):
                options.append(property_schema)
        required_raw = branch.get("required")
        required_sets.append(
            {item for item in required_raw if isinstance(item, str)}
            if isinstance(required_raw, list)
            else set()
        )

    flattened_properties: dict[str, JSONValue] = {}
    for name, options in property_options.items():
        flattened_properties[name] = (
            options[0] if len(options) == 1 else {"anyOf": options}
        )
    common_required = set.intersection(*required_sets) if required_sets else set()
    required_names = list[JSONValue](sorted(common_required))
    return {
        "type": "object",
        "properties": flattened_properties,
        "required": required_names,
        "additionalProperties": False,
    }


def _validate_canonical_schema(
    *, schema: dict[str, JSONValue], usage: SchemaUsage
) -> None:
    try:
        Draft7Validator.check_schema(schema)
    except SchemaError as exc:
        raise ProviderSchemaCompilationError(
            path="$",
            keyword="schema",
            message=f"Invalid canonical JSON Schema: {exc.message}",
        ) from exc
    if usage == "tool_parameters" and schema.get("type") != "object":
        raise ProviderSchemaCompilationError(
            path="$",
            keyword="type",
            message="Tool parameters must have an object root.",
        )


def _compile_openai_node(value: JSONValue, *, path: str) -> JSONValue:
    if isinstance(value, list):
        return [
            _compile_openai_node(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if not isinstance(value, dict):
        return value
    for keyword in _OPENAI_UNSUPPORTED_KEYWORDS:
        if keyword in value:
            raise ProviderSchemaCompilationError(
                path=path,
                keyword=keyword,
                message=f"OpenAI strict schema cannot represent {keyword} at {path}.",
            )
    stripped = {
        key: item
        for key, item in value.items()
        if key not in _OPENAI_STRIPPED_KEYWORDS
        and not (
            key == "format"
            and not (isinstance(item, str) and item in _OPENAI_SUPPORTED_FORMATS)
        )
    }
    compiled = _compile_common_node(stripped, path=path, compiler=_compile_openai_node)
    if compiled.get("type") == "object":
        properties = compiled.get("properties")
        if not isinstance(properties, dict):
            raise ProviderSchemaCompilationError(
                path=path,
                keyword="properties",
                message=(
                    f"OpenAI strict object schemas must define properties at {path}."
                ),
            )
        if value.get("additionalProperties") is not False:
            raise ProviderSchemaCompilationError(
                path=path,
                keyword="additionalProperties",
                message=(
                    "OpenAI strict object schemas must set additionalProperties "
                    f"to false at {path}."
                ),
            )
        required_raw = value.get("required")
        originally_required = (
            {item for item in required_raw if isinstance(item, str)}
            if isinstance(required_raw, list)
            else set()
        )
        compiled["properties"] = {
            name: schema
            if name in originally_required
            else _make_nullable_schema(schema)
            for name, schema in properties.items()
        }
        compiled["required"] = list(properties)
        compiled["additionalProperties"] = False
    return compiled


def _compile_common_node(
    value: dict[str, JSONValue],
    *,
    path: str,
    compiler: SchemaCompiler,
) -> dict[str, JSONValue]:
    if "oneOf" in value and "anyOf" in value:
        raise ProviderSchemaCompilationError(
            path=f"{path}.anyOf",
            keyword="anyOf",
            message=(
                f"oneOf compilation cannot preserve sibling keyword anyOf at {path}."
            ),
        )
    if (
        "const" in value
        and "enum" in value
        and not _value_matches_schema(
            value=value["const"],
            schema={"enum": value["enum"]},
        )
    ):
        raise ProviderSchemaCompilationError(
            path=f"{path}.enum",
            keyword="enum",
            message=f"const value is not present in sibling enum at {path}.",
        )
    compiled: dict[str, JSONValue] = {}
    for key, item in value.items():
        if key == "const":
            continue
        if key == "properties" and isinstance(item, dict):
            # Property names are data, not schema keywords: compile each value
            # as a schema without scanning the map itself for keywords.
            compiled[key] = {
                name: compiler(subschema, path=f"{path}.properties.{name}")
                for name, subschema in item.items()
            }
            continue
        compiled[key] = compiler(item, path=f"{path}.{key}")
    if "const" in value:
        compiled["enum"] = [compiler(value["const"], path=f"{path}.const")]
    one_of = value.get("oneOf")
    if isinstance(one_of, list):
        if not _branches_are_pairwise_disjoint(one_of):
            raise ProviderSchemaCompilationError(
                path=f"{path}.oneOf",
                keyword="oneOf",
                message=f"oneOf branches are not provably disjoint at {path}.",
            )
        compiled["anyOf"] = compiled.pop("oneOf")
    return compiled


def _branches_are_pairwise_disjoint(branches: list[JSONValue]) -> bool:
    for left_index, left in enumerate(branches):
        for right in branches[left_index + 1 :]:
            if not _branches_are_disjoint(left, right):
                return False
    return True


def _branches_are_disjoint(left: JSONValue, right: JSONValue) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    left_types = _schema_types(left)
    right_types = _schema_types(right)
    if left_types and right_types and left_types.isdisjoint(right_types):
        return True
    left_properties = left.get("properties")
    right_properties = right.get("properties")
    if not isinstance(left_properties, dict) or not isinstance(right_properties, dict):
        return False
    left_required = left.get("required")
    right_required = right.get("required")
    if not isinstance(left_required, list) or not isinstance(right_required, list):
        return False
    for name in left_properties.keys() & right_properties.keys():
        if name not in left_required or name not in right_required:
            continue
        left_values = _singleton_values(left_properties[name])
        right_values = _singleton_values(right_properties[name])
        if left_values is not None and right_values is not None:
            if not any(left == right for left in left_values for right in right_values):
                return True
    return False


def _schema_types(schema: dict[str, JSONValue]) -> set[str]:
    raw_type = schema.get("type")
    if isinstance(raw_type, str):
        schema_types = {raw_type}
    elif isinstance(raw_type, list):
        schema_types = {item for item in raw_type if isinstance(item, str)}
    else:
        return set()
    if "integer" in schema_types:
        schema_types.add("number")
    return schema_types


def _singleton_values(schema: JSONValue) -> tuple[JSONValue, ...] | None:
    if not isinstance(schema, dict):
        return None
    if "const" in schema:
        return (schema["const"],)
    enum = schema.get("enum")
    if isinstance(enum, list) and len(enum) == 1:
        return (enum[0],)
    return None


def _make_nullable_schema(value: JSONValue) -> JSONValue:
    if _schema_allows_null(value):
        return value
    return {"anyOf": [value, {"type": "null"}]}


def _schema_allows_null(schema: JSONValue) -> bool:
    if not isinstance(schema, dict):
        return False
    schema_type = schema.get("type")
    if schema_type == "null":
        return True
    if isinstance(schema_type, list) and "null" in schema_type:
        return True
    for union_key in ("anyOf", "oneOf"):
        options = schema.get(union_key)
        if isinstance(options, list) and any(
            _schema_allows_null(item) for item in options
        ):
            return True
    return False


def _decode_openai_value(value: JSONValue, schema: JSONValue) -> JSONValue:
    if not isinstance(schema, dict):
        return value
    union = schema.get("oneOf") or schema.get("anyOf")
    if isinstance(union, list):
        decoded_candidates: list[JSONValue] = []
        for branch in union:
            decoded = _decode_openai_value(value, branch)
            if _value_matches_schema(value=decoded, schema=branch):
                decoded_candidates.append(decoded)
        if not decoded_candidates:
            return value
        first = decoded_candidates[0]
        if any(candidate != first for candidate in decoded_candidates[1:]):
            raise ProviderSchemaDecodeError(
                path="$",
                keyword="oneOf",
                message="Provider value matches ambiguous union branches",
            )
        return first
    if isinstance(value, list):
        item_schema = schema.get("items")
        if item_schema is None:
            return value
        return [_decode_openai_value(item, item_schema) for item in value]
    if not isinstance(value, dict):
        return value
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return value
    required_raw = schema.get("required")
    required = (
        {item for item in required_raw if isinstance(item, str)}
        if isinstance(required_raw, list)
        else set()
    )
    decoded_object: dict[str, JSONValue] = {}
    for name, item in value.items():
        property_schema = properties.get(name)
        if (
            item is None
            and name not in required
            and not _schema_allows_null(property_schema)
        ):
            continue
        decoded_object[name] = _decode_openai_value(item, property_schema)
    return decoded_object


def _value_matches_schema(*, value: JSONValue, schema: JSONValue) -> bool:
    if not isinstance(schema, dict):
        return False
    return bool(Draft7Validator(schema).is_valid(value))


__all__ = [
    "CompiledProviderSchema",
    "MAX_PROVIDER_SCHEMA_DEPTH",
    "MAX_PROVIDER_SCHEMA_NODES",
    "MAX_PROVIDER_SCHEMA_ONE_OF_BRANCHES",
    "ProviderSchemaCompilationError",
    "ProviderSchemaDecodeError",
    "SchemaUsage",
    "compile_provider_schema",
    "schema_error_path",
    "schema_error_property_names",
]
