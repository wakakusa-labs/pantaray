import { useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';

import { buildActionImageUrl } from '../../../electron/src/protocol/imageStoragePath';

type ImageLightboxCopy = {
  dialogLabel: string;
  imageAlt: string;
  close: string;
  reveal: string;
};

/**
 * Full-size view of one attached image.
 *
 * Rendered into `document.body` because the overlay window resizes itself to the measured height of the conversation subtree, and a full-size
 * image inside it would drag the window to full screen. Fixed positioning outside that subtree
 * keeps the window exactly where the user left it.
 */
export function ImageLightbox({
  storagePath,
  copy,
  onClose,
  onReveal,
}: {
  storagePath: string;
  copy: ImageLightboxCopy;
  onClose: () => void;
  onReveal: ((storagePath: string) => void) | null;
}) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => closeButtonRef.current?.focus(), []);

  const handleKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape') {
      event.stopPropagation();
      onClose();
      return;
    }
    if (event.key !== 'Tab') return;
    const focusable = Array.from(
      dialogRef.current?.querySelectorAll<HTMLButtonElement>('button') ?? []
    );
    if (focusable.length === 0) return;
    const edge = event.shiftKey ? focusable[0] : focusable[focusable.length - 1];
    if (document.activeElement !== edge) return;
    event.preventDefault();
    (event.shiftKey ? focusable[focusable.length - 1] : focusable[0]).focus();
  };

  return createPortal(
    <div
      className="action-conversation__lightbox-backdrop"
      // Clicking the backdrop dismisses; clicks inside the dialog must not.
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label={copy.dialogLabel}
        className="action-conversation__lightbox"
        onKeyDown={handleKeyDown}
      >
        <img src={buildActionImageUrl(storagePath)} alt={copy.imageAlt} decoding="async" />
        <div className="action-conversation__lightbox-actions">
          {onReveal ? (
            <button type="button" onClick={() => onReveal(storagePath)}>
              {copy.reveal}
            </button>
          ) : null}
          <button ref={closeButtonRef} type="button" onClick={onClose}>
            {copy.close}
          </button>
        </div>
      </div>
    </div>,
    document.body
  );
}
