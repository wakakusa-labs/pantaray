"""The toolkits Pantaray offers through Composio; adding one is one row here.

A new row needs a look at its read-classified tools first
(`scripts/live/composio_read_tools.py`): Composio's tags are best-effort, so a
tool that reads but returns a secret's value, costs money or starts another
authorization is lowered here. A tool that returns only secrets' names or
metadata, public keys, or values the service masks stays read. A probe is a read tool that returns what arrived
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
    core_tools: tuple[str, ...],
    class_overrides: dict[str, ClassOverride] | None = None,
    probe: tuple[str, dict[str, JSONValue]] | None = None,
    note: str | None = None,
) -> ToolkitEntry:
    return ToolkitEntry(
        toolkit=toolkit,
        display_name=display_name,
        # Every toolkit's meta.logo follows this pattern (checked on 2026-10-18).
        logo_url=f"{_LOGO_BASE}/{toolkit}",
        class_overrides=class_overrides or {},
        core_tools=core_tools,
        note=note,
        probe=(
            None
            if probe is None
            else ChangeProbe(tool_slug=probe[0], arguments=probe[1])
        ),
    )


TOOLKITS: Final[tuple[ToolkitEntry, ...]] = (
    _entry(
        "slack",
        "Slack",
        core_tools=(
            "SLACK_SEARCH_MESSAGES",
            "SLACK_FETCH_CONVERSATION_HISTORY",
            "SLACK_FETCH_MESSAGE_THREAD_FROM_A_CONVERSATION",
            "SLACK_LIST_UNREAD_CHANNEL_MESSAGES",
            "SLACK_GET_UNREAD_MESSAGES_FROM_USER",
            "SLACK_FIND_CHANNELS",
            "SLACK_FIND_USERS",
            "SLACK_SEND_MESSAGE",
            "SLACK_UPDATES_A_SLACK_MESSAGE",
            "SLACK_ADD_REACTION_TO_AN_ITEM",
            "SLACK_SCHEDULE_MESSAGE",
        ),
    ),
    _entry(
        "gmail",
        "Gmail",
        core_tools=(
            "GMAIL_FETCH_EMAILS",
            "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID",
            "GMAIL_FETCH_MESSAGE_BY_THREAD_ID",
            "GMAIL_GET_ATTACHMENT",
            "GMAIL_LIST_LABELS",
            "GMAIL_SEARCH_PEOPLE",
            "GMAIL_SEND_EMAIL",
            "GMAIL_REPLY_TO_THREAD",
            "GMAIL_CREATE_EMAIL_DRAFT",
            "GMAIL_FORWARD_MESSAGE",
            "GMAIL_ADD_LABEL_TO_EMAIL",
        ),
        # Composio's display_url is /u/0/, which opens whichever account the
        # browser signed in to first, and /u/<email>/ answers 404 even for a
        # signed-in account; ?authuser=<email> opens that account (checked on
        # 2026-10-19). An API id in the fragment (#all/<id>) opens only in a
        # Gmail tab already loaded; a Message-ID search opens from a cold click
        # too (googleworkspace/cli#858).
        note=(
            "To link a message for the user, write "
            "https://mail.google.com/mail/?authuser=<the account's email, "
            "URL-encoded>#search/rfc822msgid%3A<its Message-ID header without "
            "the angle brackets, URL-encoded>, with the account the message was "
            "read from. Never pass on display_url or a link with an API id or "
            "/u/<number or email>/: it opens the wrong account or an error."
        ),
        class_overrides={
            # The S/MIME private key's password.
            "GMAIL_LIST_SMIME_INFO": "blocked",
        },
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
        core_tools=(
            "GOOGLEDRIVE_FIND_FILE",
            "GOOGLEDRIVE_GET_FILE_METADATA",
            "GOOGLEDRIVE_DOWNLOAD_FILE",
            "GOOGLEDRIVE_EXPORT_GOOGLE_WORKSPACE_FILE",
            "GOOGLEDRIVE_READ_SPREADSHEET_VALUES",
            "GOOGLEDRIVE_LIST_COMMENTS",
            "GOOGLEDRIVE_CREATE_FILE_FROM_TEXT",
            "GOOGLEDRIVE_UPLOAD_FILE",
            "GOOGLEDRIVE_CREATE_FOLDER",
            "GOOGLEDRIVE_CREATE_COMMENT",
        ),
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
        core_tools=(
            "GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS",
            "GOOGLECALENDAR_FIND_EVENT",
            "GOOGLECALENDAR_EVENTS_GET",
            "GOOGLECALENDAR_FIND_FREE_SLOTS",
            "GOOGLECALENDAR_LIST_CALENDARS",
            "GOOGLECALENDAR_CREATE_EVENT",
            "GOOGLECALENDAR_PATCH_EVENT",
        ),
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
        core_tools=(
            "BOX_SEARCH_FOR_CONTENT",
            "BOX_LIST_ITEMS_IN_FOLDER",
            "BOX_LIST_RECENTLY_ACCESSED_ITEMS",
            "BOX_GET_FILE_INFORMATION",
            "BOX_DOWNLOAD_FILE",
            "BOX_LIST_FILE_COMMENTS",
            "BOX_UPLOAD_FILE",
            "BOX_CREATE_FOLDER",
            "BOX_CREATE_COMMENT",
        ),
        class_overrides={
            # Starts another authorization.
            "BOX_AUTHORIZE_USER": "blocked",
            # Returns an access token that can upload.
            "BOX_PREFLIGHT_CHECK_BEFORE_UPLOAD": "blocked",
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
        core_tools=(
            "OUTLOOK_LIST_MESSAGES",
            "OUTLOOK_SEARCH_MESSAGES",
            "OUTLOOK_GET_MESSAGE",
            "OUTLOOK_LIST_OUTLOOK_ATTACHMENTS",
            "OUTLOOK_DOWNLOAD_OUTLOOK_ATTACHMENT",
            "OUTLOOK_GET_CALENDAR_VIEW",
            "OUTLOOK_GET_EVENT",
            "OUTLOOK_GET_SCHEDULE",
            "OUTLOOK_SEND_EMAIL",
            "OUTLOOK_REPLY_EMAIL",
            "OUTLOOK_CREATE_DRAFT",
            "OUTLOOK_FORWARD_MESSAGE",
            "OUTLOOK_CALENDAR_CREATE_EVENT",
            "OUTLOOK_UPDATE_CALENDAR_EVENT",
            "OUTLOOK_ACCEPT_EVENT",
            "OUTLOOK_DECLINE_EVENT",
        ),
        probe=(
            "OUTLOOK_LIST_MESSAGES",
            {
                "folder": "inbox",
                "received_date_time_gt": "{window_start_iso}",
                "top": _PROBE_ITEMS,
            },
        ),
    ),
    _entry(
        "one_drive",
        "OneDrive",
        core_tools=(
            "ONE_DRIVE_SEARCH_DRIVE_ITEMS",
            "ONE_DRIVE_LIST_FOLDER_CHILDREN",
            "ONE_DRIVE_GET_ITEM",
            "ONE_DRIVE_DOWNLOAD_FILE",
            "ONE_DRIVE_LIST_ONEDRIVE_SHARED_ITEMS",
            "ONE_DRIVE_ONEDRIVE_UPLOAD_FILE",
            "ONE_DRIVE_ONEDRIVE_CREATE_TEXT_FILE",
            "ONE_DRIVE_ONEDRIVE_CREATE_FOLDER",
            "ONE_DRIVE_MOVE_ITEM",
        ),
    ),
    _entry(
        "microsoft_teams",
        "Microsoft Teams",
        core_tools=(
            "MICROSOFT_TEAMS_SEARCH_MESSAGES",
            "MICROSOFT_TEAMS_CHATS_GET_ALL_CHATS",
            "MICROSOFT_TEAMS_LIST_USER_CHAT_MESSAGES",
            "MICROSOFT_TEAMS_TEAMS_LIST_CHANNEL_MESSAGES",
            "MICROSOFT_TEAMS_LIST_MESSAGE_REPLIES",
            "MICROSOFT_TEAMS_LIST_USER_JOINED_TEAMS",
            "MICROSOFT_TEAMS_TEAMS_LIST_CHANNELS",
            "MICROSOFT_TEAMS_LIST_MEETING_TRANSCRIPTS",
            "MICROSOFT_TEAMS_GET_MEETING_TRANSCRIPT_CONTENT",
            "MICROSOFT_TEAMS_TEAMS_POST_CHAT_MESSAGE",
            "MICROSOFT_TEAMS_TEAMS_POST_CHANNEL_MESSAGE",
            "MICROSOFT_TEAMS_POST_MESSAGE_REPLY",
        ),
        probe=(
            "MICROSOFT_TEAMS_SEARCH_MESSAGES",
            {"query": "sent>={window_start_iso}", "size": _PROBE_ITEMS},
        ),
    ),
    _entry(
        "share_point",
        "SharePoint",
        core_tools=(
            "SHARE_POINT_SEARCH_FILES",
            "SHARE_POINT_LIST_SITES",
            "SHARE_POINT_LIST_FILES_IN_FOLDER",
            "SHARE_POINT_DOWNLOAD_FILE_BY_SERVER_RELATIVE_URL",
            "SHARE_POINT_GET_SITE_PAGE_CONTENT",
            "SHARE_POINT_GET_LIST_ITEMS",
            "SHARE_POINT_LIST_DRIVE_RECENT_ITEMS",
            "SHARE_POINT_UPLOAD_FILE",
            "SHARE_POINT_SHAREPOINT_CREATE_FOLDER",
            "SHARE_POINT_SHAREPOINT_CREATE_LIST_ITEM",
            "SHARE_POINT_UPDATE_LIST_ITEM",
        ),
        class_overrides={
            # Returns the form digest, the request token for write calls.
            "SHARE_POINT_GET_CONTEXT_INFO": "blocked",
        },
    ),
    _entry(
        "linkedin",
        "LinkedIn",
        core_tools=(
            "LINKEDIN_GET_MY_INFO",
            "LINKEDIN_GET_POST_CONTENT",
            "LINKEDIN_GET_SHARE_STATS",
            "LINKEDIN_CREATE_LINKED_IN_POST",
            "LINKEDIN_CREATE_COMMENT_ON_POST",
        ),
    ),
    _entry(
        "notion",
        "Notion",
        core_tools=(
            "NOTION_SEARCH_NOTION_PAGE",
            "NOTION_GET_PAGE_MARKDOWN",
            "NOTION_FETCH_DATABASE",
            "NOTION_QUERY_DATABASE",
            "NOTION_FETCH_COMMENTS",
            "NOTION_CREATE_NOTION_PAGE",
            "NOTION_UPDATE_PAGE_MARKDOWN",
            "NOTION_INSERT_ROW_DATABASE",
            "NOTION_UPDATE_ROW_DATABASE",
            "NOTION_CREATE_COMMENT",
        ),
    ),
    _entry(
        "linear",
        "Linear",
        core_tools=(
            "LINEAR_SEARCH_ISSUES",
            "LINEAR_LIST_LINEAR_ISSUES",
            "LINEAR_GET_LINEAR_ISSUE",
            "LINEAR_LIST_COMMENTS",
            "LINEAR_LIST_LINEAR_TEAMS",
            "LINEAR_LIST_LINEAR_PROJECTS",
            "LINEAR_LIST_LINEAR_STATES",
            "LINEAR_CREATE_LINEAR_ISSUE",
            "LINEAR_UPDATE_ISSUE",
            "LINEAR_CREATE_LINEAR_COMMENT",
        ),
    ),
    _entry(
        "github",
        "GitHub",
        core_tools=(
            "GITHUB_LIST_NOTIFICATIONS",
            "GITHUB_SEARCH_ISSUES_AND_PULL_REQUESTS",
            "GITHUB_GET_AN_ISSUE",
            "GITHUB_LIST_ISSUE_COMMENTS",
            "GITHUB_GET_A_PULL_REQUEST",
            "GITHUB_LIST_PULL_REQUESTS_FILES",
            "GITHUB_LIST_REVIEWS_FOR_A_PULL_REQUEST",
            "GITHUB_GET_REPOSITORY_CONTENT",
            "GITHUB_LIST_COMMITS",
            "GITHUB_LIST_REPOSITORIES_FOR_THE_AUTHENTICATED_USER",
            "GITHUB_CREATE_AN_ISSUE",
            "GITHUB_CREATE_AN_ISSUE_COMMENT",
            "GITHUB_UPDATE_AN_ISSUE",
            "GITHUB_CREATE_A_REVIEW_FOR_A_PULL_REQUEST",
        ),
        class_overrides={
            # Returns the repositories' secrets.
            "GITHUB_MANAGE_SECRETS_IN_SELECTED_REPOSITORIES": "blocked",
            # Return a secret's value: detected secret literals, the OAuth
            # token being checked, a runner download bearer token.
            "GITHUB_LIST_SECRET_SCANNING_ALERTS_FOR_A_REPOSITORY": "blocked",
            "GITHUB_CHECK_TOKEN": "blocked",
            "GITHUB_LIST_RUNNER_APPLICATIONS_FOR_AN_ORGANIZATION": "blocked",
            "GITHUB_LIST_RUNNER_APPLICATIONS_FOR_A_REPOSITORY": "blocked",
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
        core_tools=(
            "SUPABASE_LIST_ALL_PROJECTS",
            "SUPABASE_LIST_TABLES",
            "SUPABASE_GET_TABLE_SCHEMAS",
            "SUPABASE_RUN_READ_ONLY_QUERY",
            "SUPABASE_GET_PROJECT_LOGS",
        ),
        class_overrides={
            # Return a secret's value: the root encryption key, the API keys
            # including service_role, OAuth client secrets and SMS provider
            # keys, the JWT secret, the database password, Edge Function secrets.
            "SUPABASE_BETA_GET_PROJECT_PGSODIUM_CONFIG": "blocked",
            "SUPABASE_GET_PROJECT_API_KEY": "blocked",
            "SUPABASE_GET_PROJECT_API_KEYS": "blocked",
            "SUPABASE_GET_AUTH_CONFIG": "blocked",
            "SUPABASE_GETS_PROJECT_S_AUTH_CONFIG": "blocked",
            "SUPABASE_GET_PROJECT_POSTGREST_CONFIG": "blocked",
            "SUPABASE_GET_DATABASE_BRANCH_CONFIG": "blocked",
            "SUPABASE_LIST_SECRETS": "blocked",
            "SUPABASE_LIST_PROJECT_SECRETS": "blocked",
            # Starts another authorization.
            "SUPABASE_BETA_AUTHORIZE_USER_THROUGH_OAUTH": "blocked",
        },
    ),
    _entry(
        "youtube",
        "YouTube",
        core_tools=(
            "YOUTUBE_SEARCH_YOU_TUBE",
            "YOUTUBE_GET_VIDEO_DETAILS_BATCH",
            "YOUTUBE_LIST_CHANNEL_VIDEOS",
            "YOUTUBE_GET_CHANNEL_STATISTICS",
            "YOUTUBE_LIST_COMMENT_THREADS2",
            "YOUTUBE_CREATE_COMMENT_REPLY",
        ),
    ),
)

TOOLKITS_BY_SLUG: Final[dict[str, ToolkitEntry]] = {
    entry.toolkit: entry for entry in TOOLKITS
}
