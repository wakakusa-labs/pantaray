"""local runtime の調整値を checked-in TOML から読み込む。

環境変数が持つのは secret と machine / deployment 固有の値だけで、非機密かつ環境非依存
な調整値は ``config_tunables.toml`` が唯一の source of truth である。同名の環境変数に
よる override は存在しない。ファイル欠落・TOML 不正・schema 違反はいずれも起動失敗と
して送出する。
"""

from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Final

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError

LOCAL_RUNTIME_TUNABLES_PATH: Final[Path] = (
    Path(__file__).resolve().parent / "config_tunables.toml"
)
_USER_ID_PLACEHOLDER: Final[str] = "{user_id}"


def _require_user_id_placeholder(value: str) -> str:
    if _USER_ID_PLACEHOLDER not in value:
        raise ValueError(
            f"storage path template must include '{_USER_ID_PLACEHOLDER}' "
            "to avoid collisions between users"
        )
    return value


StoragePathTemplate = Annotated[
    str,
    Field(min_length=1),
    AfterValidator(_require_user_id_placeholder),
]
PositiveInt = Annotated[int, Field(gt=0)]
PositiveFloat = Annotated[float, Field(gt=0)]


class _TunableSection(BaseModel):
    """全 section 共通の strict 設定（未知キー禁止・型変換禁止・不変）。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ActionAgentTunables(_TunableSection):
    """ActionAgent の 1 プロセスあたりの実行上限。"""

    max_steps: PositiveInt
    max_tool_steps: PositiveInt
    max_parallel_memory_queries: PositiveInt
    max_parallel_tool_calls: PositiveInt
    context_window_tokens: PositiveInt
    cancel_check_max_consecutive_failures: PositiveInt
    cancel_check_failure_grace_seconds: PositiveInt


class ChatTunables(_TunableSection):
    """The chat's input budget per request."""

    context_window_tokens: PositiveInt


class ArtifactPathTunables(_TunableSection):
    """artifact root からの相対 storage path template。"""

    long_term_insight_storage_path_template: StoragePathTemplate
    structured_facts_storage_path_template: StoragePathTemplate


class WebSocketTunables(_TunableSection):
    """WebSocket message の受け入れ上限と rate limit。"""

    max_message_bytes: PositiveInt
    rate_limit_window_seconds: PositiveFloat
    rate_limit_max_messages: PositiveInt


class WebSocketCapacityTunables(_TunableSection):
    """同時 WS 接続数の上限。"""

    max_connections_total: PositiveInt
    max_connections_per_user: PositiveInt


class WebSocketSessionTunables(_TunableSection):
    """in-memory session store の保持上限と resume 許容範囲。"""

    max_age_seconds: PositiveInt
    max_resume_missing_chunks: PositiveInt
    store_max_sessions: PositiveInt
    store_max_processes_per_session: PositiveInt
    store_max_events_per_session: PositiveInt
    store_max_chunks_per_process: PositiveInt


class LocalRuntimeTunables(_TunableSection):
    """``config_tunables.toml`` 全体の schema。"""

    action_agent: ActionAgentTunables
    chat: ChatTunables
    artifact_paths: ArtifactPathTunables
    websocket: WebSocketTunables
    websocket_capacity: WebSocketCapacityTunables
    websocket_session: WebSocketSessionTunables


def load_tunables_file(path: Path) -> LocalRuntimeTunables:
    """指定した TOML を strict 検証して読み込む。

    Raises:
        RuntimeError: ファイルが読めない / TOML が壊れている / schema に違反する場合。
    """

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"Missing or unreadable local runtime tunables file: {path}: {exc}"
        ) from exc
    try:
        parsed = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(
            f"Invalid TOML in local runtime tunables file: {path}: {exc}"
        ) from exc
    try:
        return LocalRuntimeTunables.model_validate(parsed)
    except ValidationError as exc:
        raise RuntimeError(f"Invalid local runtime tunables in {path}: {exc}") from exc


@lru_cache(maxsize=1)
def load_local_runtime_tunables() -> LocalRuntimeTunables:
    """checked-in の local runtime tunables を読み込む（プロセス内で 1 回だけ）。"""

    return load_tunables_file(LOCAL_RUNTIME_TUNABLES_PATH)


__all__ = [
    "LOCAL_RUNTIME_TUNABLES_PATH",
    "ActionAgentTunables",
    "ArtifactPathTunables",
    "ChatTunables",
    "LocalRuntimeTunables",
    "WebSocketCapacityTunables",
    "WebSocketSessionTunables",
    "WebSocketTunables",
    "load_local_runtime_tunables",
    "load_tunables_file",
]
