from __future__ import annotations

from fastapi import FastAPI

from pantaray_agents.orchestration import orchestration_ws_router
from pantaray_agents.routers import (
    action,
    action_approval,
    action_browser,
    action_messages,
    action_screen_capture,
    action_tool_output,
    activity,
    chat,
    context_source,
    history,
    hooks,
    integrations,
    suggestion,
    todo_imports,
    todo_progress,
    todos,
    workspace_organizations,
    workspace_settings,
)
from pantaray_agents.routers.history_overlay import router as history_overlay_router


def register_local_routers(app: FastAPI) -> None:
    app.include_router(suggestion.router)
    app.include_router(action.router)
    app.include_router(action_approval.router)
    app.include_router(action_messages.router)
    app.include_router(action_tool_output.router)
    app.include_router(action_screen_capture.router)
    app.include_router(action_browser.router)
    app.include_router(workspace_settings.router)
    app.include_router(workspace_organizations.router)
    app.include_router(integrations.router)
    app.include_router(context_source.router)
    app.include_router(activity.router)
    app.include_router(history.router)
    app.include_router(chat.router)
    app.include_router(todos.router)
    app.include_router(todo_progress.router)
    app.include_router(todo_imports.router)
    app.include_router(hooks.router)
    app.include_router(history_overlay_router)
    app.include_router(orchestration_ws_router)
