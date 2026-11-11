from __future__ import annotations

from pathlib import Path

from pantaray_agents.local_runtime.todos import core
from pantaray_agents.local_runtime.todos.conversation import todo_message_id
from pantaray_agents.local_runtime.todos.model import ActionConversation, Todo
from pantaray_agents.local_runtime.todos.reads import (
    read_todo,
)
from pantaray_agents.local_runtime.todos.retention import purge_expired_todos
from pantaray_agents.local_runtime.todos.writes import (
    create_todo_item,
    update_todo,
)

from .store_support import NOW, USER, count, execute, seed_action, seed_suggestion

AFTER_RETENTION = "2026-12-04T03:00:00.000Z"


def test_retention_erases_the_todo_with_its_conversation_unless_a_run_is_active(
    db: Path,
) -> None:
    ids = []
    for client_id in ("done", "busy"):
        created = create_todo_item(
            user_id=USER,
            client_todo_id=client_id,
            project_id=None,
            text=client_id,
            starts_on="2026-11-04",
            due=None,
            clock=NOW,
        )
        assert isinstance(created, Todo)
        update_todo(
            user_id=USER,
            todo_id=created.todo_id,
            step=lambda todo: core.delete_by_user(todo, now=NOW.now),
        )
        ids.append(created.todo_id)
    done_id, busy_id = ids
    seed_action(db, "done", message_id=todo_message_id(done_id))
    seed_action(
        db,
        "busy",
        message_id=todo_message_id(busy_id),
        status="processing",
        run="running",
    )
    seed_suggestion(db, "s-1")
    execute(
        db,
        f"""INSERT INTO todo_items(todo_id, user_id, text, author, origin_kind,
          suggestion_id, starts_on, deleted_by, deleted_at, created_at) VALUES
          ('todo-s', '{USER}', 'From a suggestion', 'ai', 'suggestion', 's-1',
           '2026-11-02', 'user', '{NOW.now}', '{NOW.now}')""",
    )

    purge_expired_todos(user_id=USER, now="2026-12-04T02:59:59.999Z")
    assert count(db, "SELECT COUNT(*) FROM todo_items") == 3

    purge_expired_todos(user_id=USER, now=AFTER_RETENTION)

    assert read_todo(user_id=USER, todo_id=done_id) is None
    assert read_todo(user_id=USER, todo_id="todo-s") is None
    assert count(db, "SELECT COUNT(*) FROM agent_actions WHERE action_id = 'done'") == 0
    assert count(db, "SELECT COUNT(*) FROM agent_suggestions") == 0
    kept = read_todo(user_id=USER, todo_id=busy_id)
    assert kept is not None and isinstance(kept.conversation, ActionConversation)

    execute(
        db,
        "UPDATE agent_actions SET status = 'success' WHERE action_id = 'busy'; "
        "UPDATE processes SET status = 'completed' WHERE action_id = 'busy'; "
        "UPDATE jobs SET status = 'completed' WHERE job_id = 'job-busy';",
    )
    purge_expired_todos(user_id=USER, now=AFTER_RETENTION)

    assert count(db, "SELECT COUNT(*) FROM todo_items") == 0
    assert count(db, "SELECT COUNT(*) FROM agent_actions") == 0
