"""The toolkits Pantaray offers through Composio; adding one is one row here.

A new row needs a look at its read-classified tools first
(`scripts/live/composio_read_tools.py`): Composio's tags are best-effort, so a
tool that reads but returns secrets, costs money or starts another
authorization is lowered here. A probe is a read tool that returns what arrived
since the window start; Slack's search dates and Linear's read tools cannot
express that, so those two have none yet.
"""

from __future__ import annotations

from typing import Final

from pantaray_agents.schema.agent.base import JSONValue

from ..contracts import ChangeProbe, ClassOverride, ToolkitEntry

_LOGO_BASE: Final = "https://logos.composio.dev/api"
# Each probe returns at most this many items; change_probe truncates the rest.
_PROBE_ITEMS: Final = 10


def _entry(
    toolkit: str,
    display_name: str,
    *,
    class_overrides: dict[str, ClassOverride] | None = None,
    probe: tuple[str, dict[str, JSONValue]] | None = None,
) -> ToolkitEntry:
    return ToolkitEntry(
        toolkit=toolkit,
        display_name=display_name,
        # Every toolkit's meta.logo follows this pattern (checked on 2026-10-18).
        logo_url=f"{_LOGO_BASE}/{toolkit}",
        class_overrides=class_overrides or {},
        probe=(
            None
            if probe is None
            else ChangeProbe(tool_slug=probe[0], arguments=probe[1])
        ),
    )


TOOLKITS: Final[tuple[ToolkitEntry, ...]] = (
    _entry("slack", "Slack"),
    _entry(
        "gmail",
        "Gmail",
        probe=(
            "GMAIL_FETCH_EMAILS",
            {
                "query": "in:inbox after:{window_start_epoch}",
                "max_results": _PROBE_ITEMS,
                "include_payload": False,
                "verbose": False,
            },
        ),
    ),
    _entry(
        "googledrive",
        "Google Drive",
        probe=(
            "GOOGLEDRIVE_FIND_FILE",
            {
                "q": "modifiedTime > '{window_start_iso}' and trashed = false",
                "pageSize": _PROBE_ITEMS,
            },
        ),
    ),
    _entry(
        "googlecalendar",
        "Google Calendar",
        probe=(
            "GOOGLECALENDAR_EVENTS_LIST",
            {
                "calendarId": "primary",
                "updatedMin": "{window_start_iso}",
                "singleEvents": True,
                "maxResults": _PROBE_ITEMS,
            },
        ),
    ),
    _entry(
        "box",
        "Box",
        class_overrides={
            # Starts another authorization.
            "BOX_AUTHORIZE_USER": "blocked",
            # Box AI: billed and run on the user's content.
            "BOX_ASK_QUESTION": "write",
            "BOX_GENERATE_TEXT": "write",
            "BOX_EXTRACT_METADATA_FREEFORM": "write",
            "BOX_EXTRACT_METADATA_STRUCTURED": "write",
        },
    ),
    _entry(
        "outlook",
        "Outlook",
        probe=(
            "OUTLOOK_LIST_MESSAGES",
            {
                "folder": "inbox",
                "received_date_time_gt": "{window_start_iso}",
                "top": _PROBE_ITEMS,
            },
        ),
    ),
    _entry("one_drive", "OneDrive"),
    _entry(
        "microsoft_teams",
        "Microsoft Teams",
        probe=(
            "MICROSOFT_TEAMS_SEARCH_MESSAGES",
            {"query": "sent>={window_start_iso}", "size": _PROBE_ITEMS},
        ),
    ),
    _entry("share_point", "SharePoint"),
    _entry("linkedin", "LinkedIn"),
    _entry("notion", "Notion"),
    _entry("linear", "Linear"),
    _entry(
        "github",
        "GitHub",
        class_overrides={
            # Returns the repositories' secrets.
            "GITHUB_MANAGE_SECRETS_IN_SELECTED_REPOSITORIES": "blocked",
            # GitHub Models inference: billed per call.
            "GITHUB_CREATE_INFERENCE_CHAT_COMPLETIONS": "write",
            "GITHUB_CREATE_INFERENCE_EMBEDDINGS": "write",
        },
        probe=(
            "GITHUB_LIST_NOTIFICATIONS",
            {"since": "{window_start_iso}", "per_page": _PROBE_ITEMS},
        ),
    ),
    _entry(
        "supabase",
        "Supabase",
        class_overrides={
            # Returns the project's root encryption key.
            "SUPABASE_BETA_GET_PROJECT_PGSODIUM_CONFIG": "blocked",
            # Starts another authorization.
            "SUPABASE_BETA_AUTHORIZE_USER_THROUGH_OAUTH": "blocked",
        },
    ),
    _entry("youtube", "YouTube"),
)

TOOLKITS_BY_SLUG: Final[dict[str, ToolkitEntry]] = {
    entry.toolkit: entry for entry in TOOLKITS
}
