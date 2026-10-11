import { Check, CircleAlert, Link } from 'lucide-react';

import { useClipboardCopy } from '@/components/agent-overlay/useClipboardCopy';
import { useI18n } from '@/context/useI18n';

import { workRefLink, type WorkRefKind } from './workRefLink';

/**
 * 「リンクをコピー」 for a header: copies the task's or suggestion's link, which any composer turns
 * into its chip. `failed` is the alert to announce, and the button's title while it shows.
 */
export function useCopyWorkLink(kind: WorkRefKind, id: string) {
  const { t } = useI18n();
  const { status, copy } = useClipboardCopy();
  const label = t('overlay.copyLink');
  const failed = status === 'failed' ? t('overlay.copyLinkFailed') : null;
  return {
    label,
    title: failed ?? label,
    failed,
    Icon: status === 'copied' ? Check : status === 'failed' ? CircleAlert : Link,
    copy: () => void copy(() => workRefLink(kind, id)),
  };
}
