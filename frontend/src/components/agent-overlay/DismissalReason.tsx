import { useI18n } from '@/context/useI18n';
import '../action-conversation/actionConversationView.css';

/** What the user wrote as they dismissed a suggestion, shown as their words. */
export function DismissalReason({ reason }: { reason: string }) {
  const { t } = useI18n();
  return (
    <article
      className="action-conversation__user"
      aria-label={t('overlay.dismissReason')}
      style={{ marginTop: 'var(--space-sm)' }}
    >
      <p>{reason}</p>
    </article>
  );
}
