"""ツール定義の基底モデルとユーティリティ。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from jsonschema import Draft7Validator  # type: ignore[import-untyped]

from pantaray_agents.schema.agent.base import JSONValue

from . import schema_types as _schema_types

type SchemaScalar = _schema_types.SchemaScalar
type SchemaNode = _schema_types.SchemaNode
type SchemaMapping = _schema_types.SchemaMapping
type SchemaSequence = _schema_types.SchemaSequence
type ToolValidationPath = _schema_types.ToolValidationPath

FrozenSchemaMapping = _schema_types.FrozenSchemaMapping
FrozenSchemaSequence = _schema_types.FrozenSchemaSequence
ToolValidationDetails = _schema_types.ToolValidationDetails
freeze_schema_node = _schema_types.freeze_schema_node
schema_to_plain_json = _schema_types.schema_to_plain_json

type ToolArgsValidator = Callable[[Mapping[str, JSONValue]], None]
type ToolCapability = str
type ToolIntentClass = Literal[
    "read_only",
    "surgical_edit",
    "bulk_edit",
    "process_exec_local",
    "dependency_install",
    "dependency_update",
    "network_access",
    "automation_control",
    "screen_capture",
]


@dataclass(frozen=True, slots=True)
class ToolGuideSpec:
    """LLM向けの説明素材。"""

    what: str
    when: str
    pitfalls: str


@dataclass(frozen=True, slots=True)
class PromptContract:
    """LLM に提示するツール契約。フィールドの説明は入力スキーマが運ぶ。"""

    description: str


def build_description_from_guide(guide: ToolGuideSpec) -> str:
    """Guide から複数行の description 文を生成する。"""

    def section_text(text: str) -> str:
        return "\n".join(line.rstrip() for line in text.strip().splitlines()).strip()

    sections = [
        ("Purpose", section_text(guide.what)),
        ("When", section_text(guide.when)),
        ("Avoid", section_text(guide.pitfalls)),
    ]
    return "\n\n".join(f"{title}:\n{body}" for title, body in sections if body)


@dataclass(frozen=True, slots=True)
class ToolExecutionPolicy:
    """Tool execution contract persisted into the local tooling catalog."""

    intent_class: ToolIntentClass
    required_capabilities: tuple[ToolCapability, ...]
    default_timeout_ms: int | None


def tool_execution_policy(
    *,
    intent_class: ToolIntentClass,
    required_capabilities: tuple[ToolCapability, ...] = (),
    default_timeout_ms: int | None,
) -> ToolExecutionPolicy:
    return ToolExecutionPolicy(
        intent_class=intent_class,
        required_capabilities=required_capabilities,
        default_timeout_ms=default_timeout_ms,
    )


@dataclass(frozen=True, slots=True)
class FieldAliasSpec:
    """1つの canonical field に対する runtime alias。"""

    name: str
    schema: SchemaMapping


@dataclass(frozen=True, slots=True)
class ReferenceGroupSpec:
    """runtime では複数 alias を許可し、prompt では canonical だけ見せる参照定義。"""

    canonical_name: str
    aliases: tuple[FieldAliasSpec, ...]
    description: str
    required: bool = False


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """ツール入力の1フィールド定義。"""

    name: str
    schema: SchemaMapping
    required: bool = False
    description: str = ""
    children: tuple[InputMember, ...] = ()


InputMember = FieldSpec | ReferenceGroupSpec


@dataclass(frozen=True, slots=True)
class VariantSpec:
    """discriminator ごとの入力分岐。"""

    discriminator_field: str
    discriminator_value: str
    fields: tuple[InputMember, ...] = ()


@dataclass(frozen=True, slots=True)
class InputSpec:
    """ツール入力の SSOT。"""

    fields: tuple[InputMember, ...] = ()
    variants: tuple[VariantSpec, ...] = ()
    additional_properties: bool = False
    description: str = ""
    variant_schema_mode: str = "oneOf"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """ActionAgent ツールの SSOT。"""

    tool_id: str
    name: str
    description: str
    guide: ToolGuideSpec
    execution_policy: ToolExecutionPolicy
    input_spec: InputSpec
    output_schema: Mapping[str, JSONValue]
    runtime_config: Mapping[str, JSONValue] | None = None
    pre_validate_args: ToolArgsValidator | None = None


# Not frozen: re-raising through a context manager assigns ``__traceback__``.
@dataclass(eq=False)
class ToolPolicyValidationError(RuntimeError):
    """JSON Schema では表現しにくいツール入力ポリシー違反。"""

    message: str
    details: ToolValidationDetails | None = None

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """アクションエージェントが利用するツールのメタデータ。

    Attributes:
        tool_id: ツールID（LLMから参照される一意識別子）
        name: ツールの人間可読名
        description: ツールの詳細説明
        prompt_contract: LLM向けの表示契約
        input_schema: JSON Schema 互換の入力定義（不変 Mapping）
        output_schema: JSON Schema 互換の出力定義
        runtime_config: ツール実装が参照する実行時設定（内部プロンプト/定数など、LLMには出さない前提）
        input_schema_fingerprint: input_schema の canonical JSON から算出した SHA-256（validator cache key 用）
    """

    tool_id: str
    name: str
    description: str
    prompt_contract: PromptContract
    guide: ToolGuideSpec
    execution_policy: ToolExecutionPolicy
    input_schema: SchemaMapping
    output_schema: Mapping[str, JSONValue]
    runtime_config: Mapping[str, JSONValue] | None = None
    pre_validate_args: ToolArgsValidator | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    input_schema_fingerprint: str = field(init=False, repr=False, compare=False)
    _input_schema_canonical_json_for_validation: str = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        """入力スキーマを不変化し、fingerprint を初期化時に1回だけ計算する。"""
        if not isinstance(self.input_schema, Mapping):
            raise TypeError("input_schema must be a JSON object (Mapping).")

        frozen_schema = freeze_schema_node(self.input_schema)
        if not isinstance(frozen_schema, Mapping):
            raise TypeError("input_schema must be a JSON object (Mapping).")

        plain_schema = schema_to_plain_json(frozen_schema)
        if not isinstance(plain_schema, dict):
            raise TypeError("input_schema must be a JSON object (dict-compatible).")

        if not isinstance(self.output_schema, Mapping):
            raise TypeError("output_schema must be a JSON object (Mapping).")
        frozen_output_schema = freeze_schema_node(self.output_schema)
        if not isinstance(frozen_output_schema, Mapping):
            raise TypeError("output_schema must be a JSON object (Mapping).")
        plain_output_schema = schema_to_plain_json(frozen_output_schema)
        if not isinstance(plain_output_schema, dict):
            raise TypeError("output_schema must be a JSON object (dict-compatible).")
        Draft7Validator.check_schema(plain_output_schema)

        schema_canonical_json = json.dumps(
            plain_schema,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )

        object.__setattr__(self, "input_schema", frozen_schema)
        object.__setattr__(self, "output_schema", frozen_output_schema)
        object.__setattr__(
            self,
            "_input_schema_canonical_json_for_validation",
            schema_canonical_json,
        )
        object.__setattr__(
            self,
            "input_schema_fingerprint",
            hashlib.sha256(schema_canonical_json.encode("utf-8")).hexdigest(),
        )

    def build_validation_input_schema(self) -> dict[str, JSONValue]:
        """validator生成用の plain JSON schema を都度復元して返す。"""
        parsed = json.loads(self._input_schema_canonical_json_for_validation)
        if not isinstance(parsed, dict):
            raise TypeError("tool input schema for validation must be a JSON object.")
        return parsed

    @classmethod
    def from_spec(cls, spec: ToolSpec) -> ToolDefinition:
        """ToolSpec から公開用 ToolDefinition を生成する。"""

        return cls(
            tool_id=spec.tool_id,
            name=spec.name,
            description=spec.description,
            prompt_contract=PromptContract(
                description=build_description_from_guide(spec.guide)
            ),
            guide=spec.guide,
            execution_policy=spec.execution_policy,
            input_schema=build_validation_input_schema(spec.input_spec),
            output_schema=spec.output_schema,
            runtime_config=spec.runtime_config,
            pre_validate_args=spec.pre_validate_args,
        )


def build_validation_input_schema(input_spec: InputSpec) -> dict[str, JSONValue]:
    """InputSpec から validator 用 JSON Schema を生成する。"""

    from . import validation_schema as _validation_schema

    return _validation_schema.build_validation_input_schema(input_spec)


def field_spec(
    *,
    name: str,
    schema: Mapping[str, SchemaNode],
    required: bool = False,
    description: str = "",
    children: Sequence[InputMember] = (),
) -> FieldSpec:
    """FieldSpec の簡易ヘルパー。"""

    return FieldSpec(
        name=name,
        schema=FrozenSchemaMapping(dict(schema)),
        required=required,
        description=description,
        children=tuple(children),
    )


def field_alias_spec(*, name: str, schema: Mapping[str, SchemaNode]) -> FieldAliasSpec:
    """FieldAliasSpec の簡易ヘルパー。"""

    return FieldAliasSpec(name=name, schema=FrozenSchemaMapping(dict(schema)))


def reference_group_spec(
    *,
    canonical_name: str,
    aliases: Sequence[FieldAliasSpec],
    description: str,
    required: bool = False,
) -> ReferenceGroupSpec:
    """ReferenceGroupSpec の簡易ヘルパー。"""

    return ReferenceGroupSpec(
        canonical_name=canonical_name,
        aliases=tuple(aliases),
        description=description,
        required=required,
    )
