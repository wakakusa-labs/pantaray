"""Action memory_search に関する設定・解決ロジック。"""

from __future__ import annotations

from typing import TypedDict

MEMORY_SEARCH_DEFAULT_LIMIT = 20
MEMORY_SEARCH_MAX_LIMIT = 100
MEMORY_SEARCH_MAX_TIME_HINT_RADIUS_HOURS = 24 * 365
MEMORY_SEARCH_PHRASE_FALLBACK_MIN_CANDIDATES = 5
MEMORY_SEARCH_SNIPPET_MAX_CHARS = 6000
MEMORY_SEARCH_SNIPPET_CONTEXT_CHARS = 500

INSIGHT_COLUMNS: set[str] = {
    "action_id",
    "created_at",
    "error",
    "facts",
    "insight_id",
    "insight_update_id",
    "long_term_insight_sha256",
    "long_term_insight_storage_path",
    "prompt_text",
    "prompt_name",
    "prompt_version",
    "response_text",
    "short_term_insight_data",
    "status",
    "suggestion_id",
    "thinking",
    "updated_at",
    "user_id",
}

FACT_COLUMNS: set[str] = {
    "created_at",
    "error",
    "fact_id",
    "facts_profile_brief",
    "prompt_text",
    "prompt_name",
    "prompt_version",
    "response_text",
    "source_insight_ids",
    "status",
    "structured_fact_sha256",
    "structured_fact_storage_path",
    "updated_at",
    "user_id",
}

STORAGE_LONG_TERM_INSIGHT_COLUMNS: set[str] = {
    # storage-backed "content" は疑似列（DB列ではない）
    "content",
    "insight_id",
    "created_at",
    "updated_at",
    "long_term_insight_storage_path",
}

STORAGE_FACTS_COLUMNS: set[str] = {
    # storage-backed "content" は疑似列（DB列ではない）
    "content",
    "fact_id",
    "created_at",
    "updated_at",
    "structured_fact_storage_path",
}

SUGGESTION_COLUMNS: set[str] = {
    "accepted_at",
    "answer",
    "created_at",
    "error",
    "has_suggestion",
    "prompt_text",
    "prompt_name",
    "prompt_version",
    "rejected_at",
    "response_text",
    "request_images_count",
    "status",
    "suggestion_id",
    "thinking",
    "updated_at",
    "used_images_count",
    "user_id",
    "user_reaction",
}

ACTION_COLUMNS: set[str] = {
    "action_id",
    "created_at",
    "error",
    "final_prompt_text",
    "final_output",
    "llm_steps_budget",
    "prompt_name",
    "prompt_version",
    "status",
    "steps_budget",
    "suggestion_id",
    "tool_steps_budget",
    "token_budget",
    "total_completion_tokens",
    "total_llm_steps",
    "total_prompt_tokens",
    "total_steps",
    "total_tool_steps",
    "total_tokens",
    "updated_at",
    "user_id",
}

ACTIVITY_SUMMARY_COLUMNS: set[str] = {
    "summary_id",
    "summary_type",
    "summary",
    "period_start",
    "period_end",
    "created_at",
    "user_id",
    "status",
}

ACTIVITY_DESCRIPTION_COLUMNS: set[str] = {
    "log_id",
    "period_start",
    "period_end",
    "description",
    "created_at",
    "updated_at",
    "user_id",
    "status",
}


class MemorySourceConfig(TypedDict):
    """memory_search 用のソース設定。"""

    table: str
    content_field: str
    id_field: str
    time_column: str
    default_order_column: str
    allowed_columns: set[str]


MEMORY_SOURCE_CONFIG: dict[str, MemorySourceConfig] = {
    "long_term_insight": {
        "table": "agent_insights",
        # DB本文カラムは削除済み。Storage本文を検索するため、論理列として "content" を使用する。
        "content_field": "content",
        "id_field": "insight_id",
        "time_column": "updated_at",
        "default_order_column": "updated_at",
        "allowed_columns": STORAGE_LONG_TERM_INSIGHT_COLUMNS,
    },
    "short_term_insight": {
        "table": "agent_insights",
        "content_field": "short_term_insight_data",
        "id_field": "insight_id",
        "time_column": "updated_at",
        "default_order_column": "updated_at",
        "allowed_columns": INSIGHT_COLUMNS,
    },
    "facts": {
        "table": "agent_facts",
        # DB本文カラムは削除済み。Storage本文を検索するため、論理列として "content" を使用する。
        "content_field": "content",
        "id_field": "fact_id",
        "time_column": "updated_at",
        "default_order_column": "updated_at",
        "allowed_columns": STORAGE_FACTS_COLUMNS,
    },
    "suggestions": {
        "table": "agent_suggestions",
        "content_field": "answer",
        "id_field": "suggestion_id",
        "time_column": "created_at",
        "default_order_column": "created_at",
        "allowed_columns": SUGGESTION_COLUMNS,
    },
    "actions": {
        "table": "agent_actions",
        "content_field": "final_output",
        "id_field": "action_id",
        "time_column": "updated_at",
        "default_order_column": "updated_at",
        "allowed_columns": ACTION_COLUMNS,
    },
    "activity_summary": {
        "table": "activity_summaries",
        "content_field": "summary",
        "id_field": "summary_id",
        "time_column": "period_end",
        "default_order_column": "period_end",
        "allowed_columns": ACTIVITY_SUMMARY_COLUMNS,
    },
    "activity_description": {
        "table": "activity_logs",
        "content_field": "description",
        "id_field": "log_id",
        "time_column": "period_end",
        "default_order_column": "period_end",
        "allowed_columns": ACTIVITY_DESCRIPTION_COLUMNS,
    },
}


class InvalidMemoryColumnError(ValueError):
    """memory_searchで使用できない列が指定されたことを表す例外。"""


def resolve_memory_column(source: str, column: str) -> str:
    """memory_search向けに論理列名を実テーブル列へ正規化する。"""
    cfg = MEMORY_SOURCE_CONFIG.get(source)
    if not cfg:
        raise InvalidMemoryColumnError(
            f"memory_search: source='{source}' はサポートされていません。"
        )

    if not column:
        raise InvalidMemoryColumnError(
            f"memory_search: source='{source}' に列名が指定されていません。"
        )

    normalized = str(column).strip()
    mapped = {
        "content": cfg["content_field"],
        "record_id": cfg["id_field"],
        "created_at": "created_at",
        "updated_at": "updated_at",
    }.get(normalized, normalized)

    if mapped not in cfg["allowed_columns"]:
        raise InvalidMemoryColumnError(
            f"memory_search: source='{source}' で列 '{column}' はサポートされていません。"
        )

    return mapped


def resolve_memory_order_column(source: str, order_by: str) -> str:
    """memory_searchのソートに利用する列名を決定する。"""
    cfg = MEMORY_SOURCE_CONFIG.get(source)
    if not cfg:
        raise InvalidMemoryColumnError(
            f"memory_search: source='{source}' はサポートされていません。"
        )

    # activity_summaries は updated_at を持たないため、擬似的に period_end を updated_at 相当として扱う。
    if source == "activity_summary" and order_by == "updated_at":
        return "period_end"

    return resolve_memory_column(source, order_by)


def resolve_memory_time_column(source: str) -> str:
    """memory_searchの期間フィルタで使用する列名を返す。"""
    cfg = MEMORY_SOURCE_CONFIG.get(source)
    if not cfg:
        raise InvalidMemoryColumnError(
            f"memory_search: source='{source}' はサポートされていません。"
        )
    return cfg["time_column"]
