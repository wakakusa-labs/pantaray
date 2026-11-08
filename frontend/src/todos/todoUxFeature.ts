// The switch between the TODO list and the main window as it was before it.
// On: the home page is the TODO list, the rail holds the organizations, and the sidebar holds
// the TODO entries without the search, the chat or the other conversations.
// Off: the rail's Home opens the chat, and the sidebar holds the search, the projects, the chat
// and the conversations by day.
// The backend reads the same switch from `agents/src/pantaray_agents/todo_ux_feature.py`, and
// `agents/tests/unit/test_todo_ux_feature.py` fails when the two differ: flip both together.
// On: the requester is trying the TODO list now.
export const TODO_UX_ENABLED = true;
