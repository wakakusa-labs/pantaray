-- Memory update moves onto the builtin.memory_update hook. What the
-- dispatcher had not handed yet becomes that hook's inbox, so the first run
-- after the upgrade is handed it, once: the pending triggers, the chat
-- messages after each user's chat cursor, and the dismissals after each
-- user's dismissal cursor that are still unanswered (a user without a cursor
-- had every message or dismissal pending). Then the trigger table and the
-- cursors go.
INSERT INTO hook_inbox(hook_id, user_id, event_name, payload_json, occurred_at)
SELECT 'builtin.memory_update', user_id,
       CASE trigger_kind
           WHEN 'memory_from_short_insight' THEN 'insight.recorded'
           WHEN 'memory_from_24h_summary' THEN 'activity_summary.recorded'
           ELSE 'action.turn_ended'
       END,
       CASE trigger_kind
           WHEN 'memory_from_short_insight' THEN json_object('insight_id', source_id)
           WHEN 'memory_from_24h_summary'
               THEN json_object('summary_id', source_id, 'summary_type', '24h')
           ELSE json_object(
               'source_id', source_id,
               'action_id', action_id,
               'action_completed_at', action_completed_at,
               'source_action_revision_id', source_action_revision_id,
               'turn_start_step_number', turn_start_step_number,
               'turn_end_step_number', turn_end_step_number,
               'action_prompt_name', action_prompt_name,
               'action_prompt_version', action_prompt_version,
               'suggestion_id', suggestion_id
           )
       END,
       created_at
FROM memory_agent_triggers
WHERE status = 'pending'
  AND trigger_kind IN (
      'memory_from_short_insight',
      'memory_from_24h_summary',
      'memory_from_action_terminal'
  )
ORDER BY created_at, trigger_kind, source_id;

INSERT INTO hook_inbox(hook_id, user_id, event_name, payload_json, occurred_at)
SELECT 'builtin.memory_update', items.user_id, 'chat.message_recorded',
       json_object('sequence', items.sequence), items.created_at
FROM chat_items AS items
LEFT JOIN memory_chat_cursors AS cursors ON cursors.user_id = items.user_id
WHERE items.kind IN ('user_message', 'assistant_message')
  AND items.sequence > COALESCE(cursors.through_sequence, 0)
  AND items.user_id IN (SELECT user_id FROM users)
ORDER BY items.sequence;

INSERT INTO hook_inbox(hook_id, user_id, event_name, payload_json, occurred_at)
SELECT 'builtin.memory_update', suggestions.user_id, 'suggestion.dismissed',
       json_object('suggestion_id', suggestions.suggestion_id),
       suggestions.rejected_at
FROM agent_suggestions AS suggestions
LEFT JOIN memory_dismissal_cursors AS cursors
  ON cursors.user_id = suggestions.user_id
WHERE suggestions.user_reaction = 'rejected'
  AND suggestions.rejected_at IS NOT NULL
  AND (suggestions.rejected_at, suggestions.suggestion_id) > (
      COALESCE(cursors.through_rejected_at, ''),
      COALESCE(cursors.through_suggestion_id, '')
  )
  AND NOT EXISTS (
      SELECT 1 FROM agent_action_steps AS reply
      WHERE reply.user_id = suggestions.user_id
        AND reply.source_suggestion_id = suggestions.suggestion_id
  )
  AND suggestions.user_id IN (SELECT user_id FROM users)
ORDER BY suggestions.rejected_at, suggestions.suggestion_id;

DROP TABLE memory_agent_triggers;
DROP TABLE memory_chat_cursors;
DROP INDEX idx_agent_suggestions_user_dismissals;
DROP TABLE memory_dismissal_cursors;
