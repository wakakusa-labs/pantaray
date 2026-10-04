import { Check, CircleAlert, Copy } from 'lucide-react';

import { useClipboardCopy } from '@/components/agent-overlay/useClipboardCopy';
import { useI18n } from '@/context/useI18n';

/** Copies one final answer's Markdown source, so lists and emphasis survive pasting. */
export function CopyAnswerButton({ markdown }: { markdown: string }) {
  const { t } = useI18n();
  const { status, copy } = useClipboardCopy();
  const failed = status === 'failed' ? t('overlay.copyThisAnswerFailed') : null;
  const Icon = status === 'copied' ? Check : status === 'failed' ? CircleAlert : Copy;
  return (
    <div className="action-conversation__answer-actions">
      <button
        className="action-conversation__copy"
        type="button"
        aria-label={t('overlay.copyThisAnswer')}
        title={failed ?? t('overlay.copyThisAnswer')}
        onClick={() => void copy(() => markdown)}
      >
        <Icon size={14} strokeWidth={1.75} aria-hidden />
      </button>
      {failed ? (
        <span className="action-conversation__sr-only" role="alert">
          {failed}
        </span>
      ) : null}
    </div>
  );
}
