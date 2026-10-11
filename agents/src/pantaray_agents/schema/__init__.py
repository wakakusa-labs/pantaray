"""
スキーマ定義モジュール

このモジュールは以下を提供します：
- 基本的なデータモデル
- エージェントのスキーマ
- ワークフローのスキーマ
- ツールのスキーマ
"""

from .agent.action import ActionAgentRequest, ActionAgentResponse
from .agent.base import (
    AgentContext,
    AgentError,
    AgentRequest,
    AgentResponse,
    ErrorSeverity,
    ErrorType,
    StatusType,
    StepStatusType,
    TaskStatusType,
    UserAction,
)
from .agent.streaming import (
    ActionStreamEndData,
    CompletionChunk,
    StreamEndData,
)
from .agent.suggestion import (
    SuggestionAgentRequest,
    SuggestionAgentResponse,
)
from .repositories.repository import RepositoryResult
from .variables import AgentVariables, CaptureData, TaskContext
from .websocket import (
    CompletionChunkMessage,
    DismissSuggestionMessage,
    ErrorMessage,
    ExecuteActionMessage,
    ProcessCompletedMessage,
    ProcessStartedMessage,
    RejectSuggestionMessage,
    SuggestionChunkMessage,
    SuggestionReactionCommittedMessage,
)

__all__ = [
    # Base types
    "AgentRequest",
    "AgentResponse",
    "AgentError",
    "AgentContext",
    "UserAction",
    "StatusType",
    "TaskStatusType",
    "StepStatusType",
    "ErrorType",
    "ErrorSeverity",
    # Agent types
    "ActionAgentRequest",
    "ActionAgentResponse",
    "SuggestionAgentRequest",
    "SuggestionAgentResponse",
    # Streaming types
    "StreamEndData",
    "CompletionChunk",
    "ActionStreamEndData",
    # Repository types
    "RepositoryResult",
    # Variable types
    "CaptureData",
    "TaskContext",
    "AgentVariables",
    # WebSocket Message types
    "ExecuteActionMessage",
    "DismissSuggestionMessage",
    "RejectSuggestionMessage",
    "ProcessStartedMessage",
    "SuggestionChunkMessage",
    "SuggestionReactionCommittedMessage",
    "CompletionChunkMessage",
    "ProcessCompletedMessage",
    "ErrorMessage",
]
