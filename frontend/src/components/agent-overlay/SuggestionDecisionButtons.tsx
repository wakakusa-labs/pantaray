import { useI18n } from '@/context/useI18n';
import { ApprovalDecisionButton } from './ApprovalDecisionButton';

/**
 * An unanswered offer's two answers, for the composer's right end: decline, then the way forward.
 * Both use what the user wrote: 承認 as the approval's extra conditions, 見送る as a reply.
 */
export function SuggestionDecisionButtons({
  canDismiss,
  canAccept,
  onDismiss,
  onAccept,
}: {
  canDismiss: boolean;
  canAccept: boolean;
  onDismiss: () => void;
  onAccept: () => void;
}) {
  const { t } = useI18n();
  return (
    <>
      <ApprovalDecisionButton
        type="button"
        $variant="secondary"
        disabled={!canDismiss}
        title={t('overlay.dismissTitle')}
        onClick={onDismiss}
      >
        {t('overlay.dismissSuggestion')}
      </ApprovalDecisionButton>
      <ApprovalDecisionButton
        type="button"
        $variant="primary"
        disabled={!canAccept}
        onClick={onAccept}
      >
        {t('overlay.accept')}
      </ApprovalDecisionButton>
    </>
  );
}
