"""エラー生成と共通ハンドリングのミックスイン。

ホストクラス（BaseAgent想定）が以下のメソッド/属性を持つことを前提とする:
- get_response_class() -> type[AgentResponse]
- get_error_code_prefix() -> str
- _handle_agent_error(error: AgentError, response_params: dict) -> AgentResponse
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import Enum
from typing import Protocol, cast

from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.proxy_errors import build_llm_proxy_agent_error
from pantaray_agents.schema.agent.base import (
    AgentError,
    AgentResponse,
    ErrorSeverity,
    ErrorType,
    JSONValue,
    StatusType,
)
from pantaray_agents.schema.repository_errors import AgentRepositoryError
from pantaray_llm.errors import LlmProxyExecutionError

_URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)


def _sanitize_error_message(message: str) -> str:
    """エラーメッセージから機密になり得る情報（URL等）を除去する。

    背景:
        signedURL やトークンを含む URL が例外文字列に混入し得るため、
        DB/HTTP/WS にそのまま載せると漏洩リスクになる。

    方針:
        - URL は一律で置換する（詳細はログ側で追う）。
        - 例外の本質（何が失敗したか）を損ねない範囲で最小限の正規化に留める。
    """
    if not isinstance(message, str) or not message:
        return ""
    return _URL_RE.sub("<redacted_url>", message)


class ErrorHandlingMixin:
    """エラーオブジェクト生成と共通ハンドラを提供する。"""

    @staticmethod
    def _enum_to_str(value: object, enum_cls: type[Enum]) -> str:
        """Enum/文字列/その他を統一して文字列に正規化する。

        - enum メンバー: `.value` が文字列ならそのまま、そうでなければ `str(.value)`
        - 文字列: そのまま
        - その他: `str(value)`
        """
        if isinstance(value, enum_cls):
            raw = value.value
            return raw if isinstance(raw, str) else str(raw)
        return value if isinstance(value, str) else str(value)

    def _create_error(
        self,
        exception: Exception,
        error_type: ErrorType | str,
        error_code: str,
        severity: ErrorSeverity | str = ErrorSeverity.ERROR,
    ) -> AgentError:
        """AgentError を生成する（Enum/str を安全に正規化）。"""
        metadata: dict[str, JSONValue] | None = None
        if isinstance(exception, AgentRepositoryError):
            metadata = {
                "repository_stage": exception.stage,
                "repository_error_kind": (
                    exception.error_kind.value
                    if exception.error_kind is not None
                    else None
                ),
                "retryable": exception.retryable,
            }
        return AgentError(
            error_type=self._enum_to_str(error_type, ErrorType),
            error_code=error_code,
            error_message=_sanitize_error_message(str(exception)),
            severity=self._enum_to_str(severity, ErrorSeverity),
            metadata=metadata,
        )

    def _create_mapped_proxy_error(
        self, exception: LlmProxyExecutionError
    ) -> AgentError:
        """LLM proxy 例外を agent ドメインのエラーへ写像する。"""
        host = cast(_ErrorHandlingHost, self)
        return build_llm_proxy_agent_error(
            exception=exception,
            error_code_prefix=host.get_error_code_prefix(),
        )

    def get_safe_id(
        self, obj: object | None, id_attr: str, default: str = "error_id"
    ) -> str:
        """オブジェクトから安全にIDを取得する。"""
        if obj is None:
            return default
        return getattr(obj, id_attr, default)

    def _create_error_response(
        self, error: AgentError, response_params: Mapping[str, object]
    ) -> AgentResponse:
        """エラーレスポンスを生成する。"""
        host = cast(_ErrorHandlingHost, self)
        response_class = host.get_response_class()
        params = {
            "created_at": now_utc_iso(),
            "status": StatusType.ERROR.value
            if hasattr(StatusType, "ERROR")
            else "error",
            "error": error,
            **response_params,
        }
        return response_class(**params)

    async def _handle_validation_error(
        self,
        e: Exception,
        request_object: object | None,
        id_attrs: Mapping[str, str],
    ) -> AgentResponse:
        host = cast(_ErrorHandlingHost, self)
        error = self._create_error(
            e, ErrorType.VALIDATION_ERROR, f"{host.get_error_code_prefix()}_DATA_ERROR"
        )
        response_params: dict[str, object] = {
            p: self.get_safe_id(request_object, a) for p, a in id_attrs.items()
        }
        response_params.setdefault("result", None)
        response_params.setdefault("thinking", None)
        # user_id は id_attrs 経由で解決されることが多いので、上書きしない
        response_params.setdefault("user_id", None)
        return await host._handle_agent_error(error, response_params)

    async def _handle_connection_error(
        self,
        e: Exception,
        request_object: object | None,
        id_attrs: Mapping[str, str],
    ) -> AgentResponse:
        host = cast(_ErrorHandlingHost, self)
        error = self._create_error(
            e,
            ErrorType.INTERNAL_ERROR,
            f"{host.get_error_code_prefix()}_CONNECTION_ERROR",
        )
        response_params: dict[str, object] = {
            p: self.get_safe_id(request_object, a) for p, a in id_attrs.items()
        }
        response_params.setdefault("result", None)
        response_params.setdefault("thinking", None)
        response_params.setdefault("user_id", None)
        return await host._handle_agent_error(error, response_params)

    async def _handle_fetch_context_error(
        self,
        e: Exception,
        request_object: object | None,
        id_attrs: Mapping[str, str],
    ) -> AgentResponse:
        """コンテキスト取得（DB/外部API）フェーズの失敗を repository_error として返す。"""
        host = cast(_ErrorHandlingHost, self)
        error = self._create_error(
            e,
            ErrorType.REPOSITORY_ERROR,
            f"{host.get_error_code_prefix()}_FETCH_CONTEXT_ERROR",
        )
        response_params: dict[str, object] = {
            p: self.get_safe_id(request_object, a) for p, a in id_attrs.items()
        }
        response_params.setdefault("result", None)
        response_params.setdefault("thinking", None)
        response_params.setdefault("user_id", None)
        return await host._handle_agent_error(error, response_params)

    async def _handle_save_response_error(
        self,
        e: Exception,
        request_object: object | None,
        id_attrs: Mapping[str, str],
    ) -> AgentResponse:
        """保存（DB/Storage 永続化）フェーズの失敗を repository_error として返す。"""
        host = cast(_ErrorHandlingHost, self)
        error = self._create_error(
            e,
            ErrorType.REPOSITORY_ERROR,
            f"{host.get_error_code_prefix()}_SAVE_RESPONSE_ERROR",
        )
        response_params: dict[str, object] = {
            p: self.get_safe_id(request_object, a) for p, a in id_attrs.items()
        }
        response_params.setdefault("result", None)
        response_params.setdefault("thinking", None)
        response_params.setdefault("user_id", None)
        return await host._handle_agent_error(error, response_params)

    async def _handle_system_error(
        self,
        e: Exception,
        request_object: object | None,
        id_attrs: Mapping[str, str],
    ) -> AgentResponse:
        host = cast(_ErrorHandlingHost, self)
        # LLMGenerationMixin は upstream 呼び出し失敗を RuntimeError(...) にラップする。
        # このケースは SYSTEM_ERROR ではなく LLM_API_ERROR として扱う。
        err_msg = str(e)
        if isinstance(e, LlmProxyExecutionError):
            error = self._create_mapped_proxy_error(e)
        elif isinstance(e, RuntimeError) and (
            "Gemini API error" in err_msg
            or "LLM API error" in err_msg
            or "LLM upstream error" in err_msg
        ):
            error = self._create_error(
                e,
                ErrorType.LLM_API_ERROR,
                f"{host.get_error_code_prefix()}_LLM_RESPONSE_ERROR",
            )
        else:
            error = self._create_error(
                e,
                ErrorType.INTERNAL_ERROR,
                f"{host.get_error_code_prefix()}_SYSTEM_ERROR",
            )
        response_params: dict[str, object] = {
            p: self.get_safe_id(request_object, a) for p, a in id_attrs.items()
        }
        response_params.setdefault("result", None)
        response_params.setdefault("thinking", None)
        response_params.setdefault("user_id", None)
        return await host._handle_agent_error(error, response_params)

    async def _handle_general_error(
        self,
        e: Exception,
        request_object: object | None,
        id_attrs: Mapping[str, str],
    ) -> AgentResponse:
        host = cast(_ErrorHandlingHost, self)
        error = self._create_error(
            e,
            ErrorType.INTERNAL_ERROR,
            f"{host.get_error_code_prefix()}_PROCESSING_ERROR",
        )
        response_params: dict[str, object] = {
            p: self.get_safe_id(request_object, a) for p, a in id_attrs.items()
        }
        response_params.setdefault("result", None)
        response_params.setdefault("thinking", None)
        response_params.setdefault("user_id", None)
        return await host._handle_agent_error(error, response_params)


class _ErrorHandlingHost(Protocol):
    def get_response_class(self) -> type[AgentResponse]: ...

    def get_error_code_prefix(self) -> str: ...

    async def _handle_agent_error(
        self, error: AgentError, response_params: Mapping[str, object]
    ) -> AgentResponse: ...
