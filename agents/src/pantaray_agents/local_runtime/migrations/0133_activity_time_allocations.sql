-- Minutes of each short Insight run's observed range, attributed to the
-- endeavors the user worked on. They share the producing log's lifetime and
-- are estimates from event times, not a timesheet.
CREATE TABLE activity_time_allocations (
    allocation_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES activity_logs(log_id) ON DELETE CASCADE,
    observed_start TEXT NOT NULL,
    observed_end TEXT NOT NULL,
    endeavor TEXT NOT NULL CHECK (length(endeavor) > 0),
    organization_name TEXT,
    project_name TEXT,
    minutes INTEGER NOT NULL CHECK (minutes > 0),
    created_at TEXT NOT NULL
);

-- Totals are read over a period of the user's own history.
CREATE INDEX idx_activity_time_allocations_user_observed
ON activity_time_allocations(user_id, observed_start);
