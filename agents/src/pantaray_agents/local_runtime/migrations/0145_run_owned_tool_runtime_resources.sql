-- A resource can belong to the run that started it instead of to the call: a
-- process that keeps running after its bash call returns, and the temp folder
-- it uses. The run is its actor process (the Action's or a subagent's), which
-- stays the same across approval waits. Such a row still names the call that
-- started it; it is cleaned up when the run's process or its execution session
-- ends, not when that call ends. NULL keeps today's owners: the call when one
-- is named, otherwise the execution session.
ALTER TABLE tool_runtime_resources ADD COLUMN owner_process_id TEXT
    REFERENCES processes(process_id) ON DELETE SET NULL;
