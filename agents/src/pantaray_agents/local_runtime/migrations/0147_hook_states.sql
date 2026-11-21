-- When each hook fires next. A hook without a row has never fired, so its
-- latest occurrence is due; a NULL time is a one-shot hook that has fired.
CREATE TABLE hook_states (
    hook_id TEXT PRIMARY KEY,
    next_fire_at TEXT
);

-- The summary schedulers' next run times become their hooks' next fire times,
-- so the upgrade neither repeats nor delays a summary. A row without a time had
-- not run yet and stays without state.
INSERT INTO hook_states(hook_id, next_fire_at)
SELECT 'builtin.summary_' || substr(job_name, length('activity_summary_') + 1),
       next_run_after
FROM scheduler_jobs
WHERE job_kind = 'activity_summary'
  AND job_name IN (
      'activity_summary_1h',
      'activity_summary_24h',
      'activity_summary_1w',
      'activity_summary_1m',
      'activity_summary_3m'
  )
  AND next_run_after IS NOT NULL;

ALTER TABLE scheduler_jobs DROP COLUMN next_run_after;
