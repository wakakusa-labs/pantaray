"""web_search / web_extract / web_crawl のカタログ用の入力欄と入力ガード。規則は共有ツール側が持つ。"""

from __future__ import annotations

from collections.abc import Mapping

from pantaray_agents.agents.action_agent.tools.base import (
    FieldSpec,
    ToolPolicyValidationError,
    field_spec,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.web.validation import (
    ArgumentProblem,
    extract_argument_problem,
    search_argument_problem,
    url_argument_problem,
)


def shared_fields(
    fields: Mapping[str, JSONValue], required: tuple[str, ...]
) -> tuple[FieldSpec, ...]:
    """The shared request fields as catalog fields, each description beside its schema."""

    specs: list[FieldSpec] = []
    for name, schema in fields.items():
        assert isinstance(schema, dict)
        description = schema.get("description")
        specs.append(
            field_spec(
                name=name,
                schema={
                    key: value for key, value in schema.items() if key != "description"
                },
                required=name in required,
                description=description if isinstance(description, str) else "",
            )
        )
    return tuple(specs)


def _raise(problem: ArgumentProblem | None) -> None:
    if problem is not None:
        raise ToolPolicyValidationError(
            problem.summary,
            details={"path": list(problem.path), "message": problem.message},
        )


def validate_web_search_args(args: Mapping[str, JSONValue]) -> None:
    _raise(search_argument_problem(args))


def validate_web_extract_args(args: Mapping[str, JSONValue]) -> None:
    _raise(extract_argument_problem(args))


def validate_web_crawl_args(args: Mapping[str, JSONValue]) -> None:
    _raise(url_argument_problem(args.get("url"), path=("url",)))
