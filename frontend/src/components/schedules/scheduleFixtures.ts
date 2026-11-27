import type { HookView } from '../../../electron/src/settings/hooksFetch';

/** A schedule as the list returns it, for tests: on, every day, continuing its conversation. */
export function hook(overrides: Partial<HookView> = {}): HookView {
  return {
    hook_id: 'task.one',
    at: null,
    every: 'day',
    offset: null,
    match: [],
    until: null,
    message: '仕入れ表を見て、発注が必要な食材を書き出してください。',
    enabled: true,
    next_fire_at: null,
    created_from_action_id: 'action-1',
    created_from_title: '店舗の発注チェック',
    target_action_id: 'action-1',
    title: null,
    organization_id: null,
    project_id: null,
    last_result: null,
    failure_count: 0,
    stopped_reason: null,
    ...overrides,
  };
}
