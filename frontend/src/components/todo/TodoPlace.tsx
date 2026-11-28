import { useI18n } from '@/context/useI18n';
import type { TodoPlaceName } from '@/todos/groupTodosByProject';

/**
 * Where a TODO sits, as one quiet line: 「組織 › プロジェクト」, the project alone in no
 * organization, and the list's 「その他」 for no project. The Overlay and the main window's
 * conversation headers both show it, each placing it with `className`.
 */
export function TodoPlace({ place, className }: { place: TodoPlaceName; className?: string }) {
  const { t } = useI18n();
  const project = place.project ?? t('todo.noProject');
  return (
    <p className={className}>
      {place.organization === null
        ? project
        : t('todo.place', { organization: place.organization, project })}
    </p>
  );
}
