"""Generate brokered tool input specs from runtime Pydantic args models."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TypeGuard, cast

from pydantic import BaseModel

from pantaray_agents.schema.agent.base import JSONValue

from .base import InputSpec, SchemaNode, field_spec

NULL_SCHEMA_TYPE = "null"


@dataclass(frozen=True, slots=True)
class BrokerToolFieldPresentation:
    """Model-facing description for one brokered tool field."""

    name: str
    description: str


def broker_tool_input_spec_from_model(
    *,
    model: type[BaseModel],
    fields: Sequence[BrokerToolFieldPresentation],
    description: str,
) -> InputSpec:
    """Build an InputSpec from the broker runtime args model.

    The Pydantic model owns data shape and validation constraints. Presentation
    metadata owns the field descriptions the model reads. This adapter only projects
    the runtime JSON Schema into the existing LLM-facing schema shape.
    """

    schema = cast(dict[str, JSONValue], model.model_json_schema())
    properties = _required_object(schema, "properties")
    required_names = _string_set(schema.get("required", []))
    _validate_field_presentations(model, fields)

    return InputSpec(
        fields=tuple(
            field_spec(
                name=presentation.name,
                schema=cast(
                    Mapping[str, SchemaNode],
                    _normalize_prompt_schema(properties[presentation.name], schema),
                ),
                required=presentation.name in required_names,
                description=presentation.description,
            )
            for presentation in fields
        ),
        description=description,
    )


def _validate_field_presentations(
    model: type[BaseModel],
    fields: Sequence[BrokerToolFieldPresentation],
) -> None:
    model_field_names = set(model.model_fields)
    presentations_by_name = {field.name: field for field in fields}
    presentation_names = set(presentations_by_name)

    missing = model_field_names - presentation_names
    if missing:
        raise ValueError(
            "Brokered tool field presentation is missing fields: "
            f"{', '.join(sorted(missing))}"
        )

    extra = presentation_names - model_field_names
    if extra:
        raise ValueError(
            "Brokered tool field presentation contains unknown fields: "
            f"{', '.join(sorted(extra))}"
        )

    if len(fields) != len(presentations_by_name):
        raise ValueError("Brokered tool field presentation contains duplicate fields.")


def _normalize_prompt_schema(
    value: JSONValue, root_schema: Mapping[str, JSONValue]
) -> JSONValue:
    if isinstance(value, list):
        return [_normalize_prompt_schema(item, root_schema) for item in value]

    if not isinstance(value, dict):
        return value

    if "$ref" in value:
        ref = value["$ref"]
        if not isinstance(ref, str):
            raise TypeError("Pydantic schema $ref must be a string.")
        resolved = _resolve_local_ref(ref, root_schema)
        merged = {
            key: nested
            for key, nested in value.items()
            if key not in {"$ref", "title", "default", "discriminator"}
        }
        normalized = _normalize_prompt_schema(resolved, root_schema)
        if not isinstance(normalized, dict):
            raise TypeError("Resolved Pydantic schema reference must be an object.")
        return _normalize_prompt_schema({**normalized, **merged}, root_schema)

    collapsed = _collapse_nullable_any_of(value)
    if collapsed is not value:
        return _normalize_prompt_schema(collapsed, root_schema)

    normalized_dict: dict[str, JSONValue] = {}
    for key, nested in value.items():
        if key in {"$defs", "title", "default", "discriminator"}:
            continue
        normalized_dict[key] = _normalize_prompt_schema(nested, root_schema)

    if "const" in normalized_dict:
        normalized_dict.pop("type", None)

    return normalized_dict


def _collapse_nullable_any_of(value: Mapping[str, JSONValue]) -> JSONValue:
    any_of = value.get("anyOf")
    if not isinstance(any_of, list) or len(any_of) != 2:
        return cast(JSONValue, value)

    non_null_options = [
        option
        for option in any_of
        if not (_is_json_object(option) and option.get("type") == NULL_SCHEMA_TYPE)
    ]
    if len(non_null_options) != 1:
        return cast(JSONValue, value)

    siblings = {
        key: nested
        for key, nested in value.items()
        if key not in {"anyOf", "title", "default"}
    }
    non_null = non_null_options[0]
    if not isinstance(non_null, dict):
        return cast(JSONValue, value)
    return {**non_null, **siblings}


def _resolve_local_ref(ref: str, root_schema: Mapping[str, JSONValue]) -> JSONValue:
    prefix = "#/$defs/"
    if not ref.startswith(prefix):
        raise ValueError(f"Unsupported non-local Pydantic schema reference: {ref}")
    defs = _required_object(root_schema, "$defs")
    definition_name = ref.removeprefix(prefix)
    if definition_name not in defs:
        raise ValueError(f"Pydantic schema reference not found: {ref}")
    return defs[definition_name]


def _required_object(
    schema: Mapping[str, JSONValue],
    key: str,
) -> dict[str, JSONValue]:
    value = schema.get(key)
    if not isinstance(value, dict):
        raise TypeError(f"Pydantic schema key '{key}' must be an object.")
    return value


def _string_set(value: JSONValue) -> set[str]:
    if not isinstance(value, list):
        return set()
    result: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise TypeError("Pydantic schema required entries must be strings.")
        result.add(item)
    return result


def _is_json_object(value: JSONValue) -> TypeGuard[dict[str, JSONValue]]:
    return isinstance(value, dict)
