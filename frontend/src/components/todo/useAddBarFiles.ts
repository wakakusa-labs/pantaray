import { useEffect, useRef, useState } from 'react';

import {
  addStagedAttachments,
  attachmentKey,
  attachmentPayload,
  discardDocuments,
  stageFiles,
  type AttachmentFailure,
  type ComposerAttachment,
} from '@/components/agent-overlay/attachmentStaging';

/**
 * The documents and images staged in the add bar for an import, through the composers'
 * staging. Documents are discarded when the bar goes away unsent, including those whose write
 * lands after it went.
 */
export function useAddBarFiles() {
  const actions = window.electron?.actions;
  const [{ attachments, failure }, setStaged] = useState<{
    attachments: readonly ComposerAttachment[];
    failure: AttachmentFailure | null;
  }>({ attachments: [], failure: null });
  const [inFlight, setInFlight] = useState(0);
  const unsentRef = useRef<readonly ComposerAttachment[]>([]);
  const closedRef = useRef(false);
  useEffect(() => {
    unsentRef.current = attachments;
  });
  useEffect(() => {
    closedRef.current = false;
    return () => {
      closedRef.current = true;
      discardDocuments(actions, unsentRef.current);
    };
  }, [actions]);

  const attach = async (picked: readonly File[]) => {
    if (!actions || picked.length === 0) return;
    setInFlight((count) => count + 1);
    const staged = await stageFiles(actions, picked, attachments);
    if (closedRef.current) {
      discardDocuments(actions, staged.accepted);
      return;
    }
    setStaged((current) =>
      addStagedAttachments(actions, current.attachments, staged.accepted, staged.failure)
    );
    setInFlight((count) => count - 1);
  };

  return {
    attachments,
    failure,
    /** A file is still being written; sending waits for it. */
    busy: inFlight > 0,
    attach,
    remove: (removed: ComposerAttachment) => {
      discardDocuments(actions, [removed]);
      setStaged((current) => ({
        attachments: current.attachments.filter(
          (attachment) => attachmentKey(attachment) !== attachmentKey(removed)
        ),
        failure: null,
      }));
    },
    /** The documents and images as an import's start takes them. */
    payload: attachmentPayload(attachments),
    /** The import took the files, so they are no longer the bar's to discard. */
    clearSent: () => {
      unsentRef.current = [];
      setStaged({ attachments: [], failure: null });
    },
  };
}
