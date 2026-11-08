"""The switch between the TODO list experience and the app as it was before it.

On, Pantaray picks up TODOs as Actions, offers a TODO's Action its TODO tools,
shows the suggestion judge the open TODOs, tells an Action which TODO it works
on, and keeps TODO conversations out of the dated history list. Off, none of
that reaches the user or the AI; the TODO tables and routes stay, unused.

The main window reads the same switch from
`frontend/src/todos/todoUxFeature.ts`, and
`tests/unit/test_todo_ux_feature.py` fails when the two differ. Read it as
`todo_ux_feature.TODO_UX_ENABLED` at the point of use, so a test can flip it.
"""

# On: the requester is trying the TODO list now. Flip it here and in
# `frontend/src/todos/todoUxFeature.ts` to go back to the app before it.
TODO_UX_ENABLED: bool = True
