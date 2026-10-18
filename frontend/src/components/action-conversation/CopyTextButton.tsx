import { Check, CircleAlert, Copy } from 'lucide-react';

import { useClipboardCopy } from '@/components/agent-overlay/useClipboardCopy';

/** Copies `text` as given, so Markdown source and typed line breaks survive pasting. */
export function CopyTextButton({
  text,
  label,
  failedLabel,
  className = '',
}: {
  text: string;
  label: string;
  failedLabel: string;
  className?: string;
}) {
  const { status, copy } = useClipboardCopy();
  const failed = status === 'failed' ? failedLabel : null;
  const Icon = status === 'copied' ? Check : status === 'failed' ? CircleAlert : Copy;
  return (
    <>
      <button
        className={`action-conversation__copy ${className}`.trim()}
        type="button"
        // Stays clickable in the collapsed preview (useCollapsedFocusBoundary).
        data-collapsed-clickable
        aria-label={label}
        data-tooltip={failed ?? label}
        onClick={() => void copy(() => text)}
      >
        <Icon size={14} strokeWidth={1.75} aria-hidden />
      </button>
      {failed ? (
        <span className="action-conversation__sr-only" role="alert">
          {failed}
        </span>
      ) : null}
    </>
  );
}
