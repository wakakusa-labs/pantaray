"""Validation JSON Schema generation for ActionAgent tool definitions."""

from __future__ import annotations

from collections.abc import Sequence

from pantaray_agents.agents.action_agent.tools.base import (
    FieldSpec,
    InputMember,
    InputSpec,
    ReferenceGroupSpec,
    VariantSpec,
)
from pantaray_agents.agents.action_agent.tools.schema_types import (
    freeze_schema_node,
    schema_to_plain_json,
)
from pantaray_agents.schema.agent.base import JSONValue


def build_validation_input_schema(input_spec: InputSpec) -> dict[str, JSONValue]:
    """InputSpec から validator 用 JSON Schema を生成する。"""

    validate_input_spec(input_spec)
    if input_spec.variants:
        if input_spec.variant_schema_mode == "allOf":
            return _build_discriminated_object_schema(input_spec)
        return {
            "type": "object",
            "oneOf": [
                _build_object_schema(
                    (*input_spec.fields, *variant.fields),
                    description=input_spec.description,
                    additional_properties=input_spec.additional_properties,
                    const_field=variant.discriminator_field,
                    const_value=variant.discriminator_value,
                )
                for variant in input_spec.variants
            ],
        }
    return _build_object_schema(
        input_spec.fields,
        description=input_spec.description,
        additional_properties=input_spec.additional_properties,
    )


def validate_input_spec(input_spec: InputSpec) -> None:
    """ToolSpec の入力定義を fail-fast で検証する。"""

    seen_variants: set[tuple[str, str]] = set()
    for variant in input_spec.variants:
        key = (variant.discriminator_field, variant.discriminator_value)
        if key in seen_variants:
            raise ValueError(
                "Duplicate discriminator detected in ToolSpec: "
                f"{variant.discriminator_field}={variant.discriminator_value}"
            )
        seen_variants.add(key)
    _validate_members(input_spec.fields)
    for variant in input_spec.variants:
        _validate_members(variant.fields)


def _validate_members(members: Sequence[InputMember]) -> None:
    canonical_names: set[str] = set()
    alias_to_canonical: dict[str, str] = {}
    for member in members:
        if isinstance(member, ReferenceGroupSpec):
            if member.canonical_name in canonical_names:
                raise ValueError(f"Duplicate canonical field: {member.canonical_name}")
            canonical_names.add(member.canonical_name)
            for alias in member.aliases:
                owner = alias_to_canonical.get(alias.name)
                if owner is not None and owner != member.canonical_name:
                    raise ValueError(
                        f"Alias '{alias.name}' is shared by '{owner}' and "
                        f"'{member.canonical_name}'."
                    )
                alias_to_canonical[alias.name] = member.canonical_name
            continue

        if member.children:
            _validate_members(member.children)


def _build_object_schema(
    members: Sequence[InputMember],
    *,
    description: str,
    additional_properties: bool,
    const_field: str | None = None,
    const_value: str | None = None,
) -> dict[str, JSONValue]:
    properties: dict[str, JSONValue] = {}
    required: list[str] = []
    any_of: list[dict[str, JSONValue]] = []

    for member in members:
        member_props, member_required, member_any_of = _build_member_schema(member)
        properties.update(member_props)
        required.extend(member_required)
        any_of.extend(member_any_of)

    if const_field is not None and const_value is not None:
        properties[const_field] = {"const": const_value}
        required.append(const_field)

    schema: dict[str, JSONValue] = {
        "type": "object",
        "properties": properties,
        "required": _json_list(required),
        "additionalProperties": additional_properties,
    }
    if description:
        schema["description"] = description
    if any_of:
        schema["anyOf"] = _json_list(any_of)
    return schema


def _build_discriminated_object_schema(input_spec: InputSpec) -> dict[str, JSONValue]:
    """variant を top-level enum + allOf 条件として表現する。"""

    properties: dict[str, JSONValue] = {}
    required: list[str] = []
    any_of: list[dict[str, JSONValue]] = []
    for member in input_spec.fields:
        member_props, member_required, member_any_of = _build_member_schema(member)
        properties.update(member_props)
        required.extend(member_required)
        any_of.extend(member_any_of)

    discriminator_field = input_spec.variants[0].discriminator_field
    discriminator_values = [
        variant.discriminator_value for variant in input_spec.variants
    ]
    properties[discriminator_field] = {
        "type": "string",
        "enum": _json_list(discriminator_values),
        "description": "Operation to perform.",
    }
    required.append(discriminator_field)

    variant_properties = _collect_variant_properties(input_spec.variants)
    for key, value in variant_properties.items():
        properties.setdefault(key, value)

    schema: dict[str, JSONValue] = {
        "type": "object",
        "properties": properties,
        "required": _json_list(required),
        "additionalProperties": input_spec.additional_properties,
    }
    if input_spec.description:
        schema["description"] = input_spec.description
    if any_of:
        schema["anyOf"] = _json_list(any_of)

    all_of: list[dict[str, JSONValue]] = []
    for variant in input_spec.variants:
        variant_props: dict[str, JSONValue] = {}
        variant_required: list[str] = []
        for member in variant.fields:
            member_props, member_required, _ = _build_member_schema(member)
            variant_props.update(member_props)
            variant_required.extend(member_required)
        then_schema: dict[str, JSONValue] = {"properties": variant_props}
        if variant_required:
            then_schema["required"] = _json_list(variant_required)
        all_of.append(
            {
                "if": {
                    "properties": {
                        discriminator_field: {"const": variant.discriminator_value}
                    }
                },
                "then": then_schema,
            }
        )
    if all_of:
        schema["allOf"] = _json_list(all_of)
    return schema


def _collect_variant_properties(
    variants: Sequence[VariantSpec],
) -> dict[str, JSONValue]:
    """variant field の top-level property 雛形を集約する。"""

    properties: dict[str, JSONValue] = {}
    for variant in variants:
        for member in variant.fields:
            if isinstance(member, ReferenceGroupSpec):
                continue
            generic_schema = _build_generic_field_schema(member)
            if member.name in properties:
                properties[member.name] = _merge_generic_property_schemas(
                    properties[member.name],
                    generic_schema,
                )
                continue
            properties[member.name] = generic_schema
    return properties


def _build_generic_field_schema(member: FieldSpec) -> JSONValue:
    """top-level property 雛形を生成する。"""

    raw_schema = schema_to_plain_json(freeze_schema_node(member.schema))
    if not isinstance(raw_schema, dict):
        raise TypeError(f"Field '{member.name}' schema must be a JSON object.")
    generic = _with_description(member.name, raw_schema, member.description)
    if member.children:
        generic["type"] = "object"
        generic["properties"] = {
            key: value
            for child in member.children
            for key, value in _build_member_schema(child)[0].items()
        }
        generic["required"] = _json_list(())
        generic["additionalProperties"] = False
    return generic


def _merge_generic_property_schemas(
    left: JSONValue,
    right: JSONValue,
) -> JSONValue:
    """variant をまたぐ generic property schema を緩くマージする。"""

    if not isinstance(left, dict) or not isinstance(right, dict):
        return left
    if left.get("type") != "object" or right.get("type") != "object":
        return left

    merged = dict(left)
    left_props = left.get("properties")
    right_props = right.get("properties")
    if isinstance(left_props, dict) and isinstance(right_props, dict):
        merged_props = dict(left_props)
        for key, value in right_props.items():
            if key in merged_props:
                merged_props[key] = _merge_generic_leaf_schema(
                    merged_props[key],
                    value,
                )
            else:
                merged_props[key] = value
        merged["properties"] = merged_props
    merged["required"] = []
    merged["additionalProperties"] = False
    return merged


def _merge_generic_leaf_schema(left: JSONValue, right: JSONValue) -> JSONValue:
    """generic object property の重複定義を緩く統合する。"""

    if not isinstance(left, dict) or not isinstance(right, dict):
        return left
    left_type = left.get("type")
    right_type = right.get("type")
    if left_type == right_type and isinstance(left_type, str):
        merged: dict[str, JSONValue] = {"type": left_type}
        description = str(left.get("description") or right.get("description") or "")
        if description:
            merged["description"] = description
        if left_type == "object":
            return _merge_generic_property_schemas(left, right)
        return merged
    return left


def _build_member_schema(
    member: InputMember,
) -> tuple[dict[str, JSONValue], list[str], list[dict[str, JSONValue]]]:
    if isinstance(member, ReferenceGroupSpec):
        properties: dict[str, JSONValue] = {}
        for alias in member.aliases:
            alias_schema = schema_to_plain_json(freeze_schema_node(alias.schema))
            if not isinstance(alias_schema, dict):
                raise TypeError(f"Alias '{alias.name}' schema must be a JSON object.")
            properties[alias.name] = _with_description(
                alias.name, alias_schema, member.description
            )
        any_of: list[dict[str, JSONValue]] = []
        if member.required:
            any_of = [
                {"required": _json_list((alias.name,))} for alias in member.aliases
            ]
        return properties, [], any_of

    schema = _build_field_schema(member)
    required = [member.name] if member.required else []
    return {member.name: schema}, required, []


def _build_field_schema(member: FieldSpec) -> JSONValue:
    base_schema = schema_to_plain_json(freeze_schema_node(member.schema))
    if not isinstance(base_schema, dict):
        raise TypeError(f"Field '{member.name}' schema must be a JSON object.")
    if not member.children:
        return _with_description(member.name, base_schema, member.description)

    child_schema = _build_object_schema(
        member.children,
        description=str(base_schema.get("description", "")),
        additional_properties=bool(base_schema.get("additionalProperties", False)),
    )
    merged = _with_description(member.name, base_schema, member.description)
    merged["properties"] = child_schema["properties"]
    merged["required"] = child_schema["required"]
    if child_schema.get("anyOf"):
        merged["anyOf"] = child_schema["anyOf"]
    if "additionalProperties" not in merged:
        merged["additionalProperties"] = False
    return merged


def _with_description(
    name: str, schema: dict[str, JSONValue], description: str
) -> dict[str, JSONValue]:
    """Carry the field description to the model through the schema it receives.

    The same schema validates arguments; ``description`` is only an annotation.
    """

    if not description:
        return dict(schema)
    if "description" in schema:
        raise ValueError(
            f"Field '{name}' defines its description twice; keep only the field "
            "description."
        )
    return {**schema, "description": description}


def _json_list(values: Sequence[JSONValue]) -> list[JSONValue]:
    return list(values)


__all__ = [
    "build_validation_input_schema",
    "validate_input_spec",
]
