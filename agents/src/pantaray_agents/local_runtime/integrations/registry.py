"""The process-wide integration gateway and its tool index.

No gateway is registered until a connection method configures one, so a user
without integrations sees no change. Callers read both through the getters on
every use rather than holding them, because registering a gateway replaces the
tool index: tools listed by the previous connection method must not stay
searchable.
"""

from __future__ import annotations

from .contracts import IntegrationGateway
from .tool_index import ToolIndex

_GATEWAY: IntegrationGateway | None = None
_TOOL_INDEX = ToolIndex()


def set_integration_gateway(gateway: IntegrationGateway | None) -> None:
    global _GATEWAY, _TOOL_INDEX
    _GATEWAY = gateway
    _TOOL_INDEX = ToolIndex()


def get_integration_gateway() -> IntegrationGateway | None:
    return _GATEWAY


def get_integration_tool_index() -> ToolIndex:
    return _TOOL_INDEX
