import { useRef, useState } from 'react';

import type { ActionImageReference } from '../../../electron/src/actions/actionContracts';
import { buildActionImageUrl } from '../../../electron/src/protocol/imageStoragePath';

import { ImageLightbox } from './ImageLightbox';

import './attachedImages.css';

/** The thumbnail's intrinsic box, mirrored in CSS so a late decode cannot shift the layout. */
const THUMBNAIL_SIZE_PX = 96;

/**
 * Every label one image grid needs. A user attachment and a screenshot the agent took are
 * the same picture to the DOM and different things to the reader, so the caller names them.
 */
export type ImageGridCopy = {
  list: (count: number) => string;
  imageAlt: (position: number, count: number) => string;
  open: (position: number, count: number) => string;
  missing: string;
  lightbox: { dialogLabel: string; close: string; reveal: string };
};

/**
 * Thumbnails for the images carried by one message or one tool step.
 *
 * The images are served over `pantaray-image://`, never inlined as `data:` URLs: an 8 MB image
 * would otherwise become an ~11 MB string in React state and in the DOM, copied on every
 * overlay re-render. Through the scheme, Chromium owns the bytes and the cache.
 */
export function AttachedImages({
  images,
  copy,
}: {
  images: readonly ActionImageReference[];
  copy: ImageGridCopy;
}) {
  const [openIndex, setOpenIndex] = useState<number | null>(null);
  const [missing, setMissing] = useState<ReadonlySet<string>>(() => new Set());
  const thumbnailRefs = useRef(new Map<number, HTMLButtonElement>());
  const revealImage = window.electron?.actions?.revealImage;
  const open = openIndex === null ? null : { image: images[openIndex], position: openIndex + 1 };

  const closeLightbox = () => {
    const trigger = openIndex === null ? null : (thumbnailRefs.current.get(openIndex) ?? null);
    setOpenIndex(null);
    trigger?.focus();
  };

  return (
    <>
      <ul className="action-conversation__attachments" aria-label={copy.list(images.length)}>
        {images.map((image, index) => (
          <li key={image.storage_path}>
            {missing.has(image.storage_path) ? (
              <span className="action-conversation__thumb action-conversation__thumb--missing">
                {copy.missing}
              </span>
            ) : (
              <button
                ref={(element) => {
                  if (element) thumbnailRefs.current.set(index, element);
                  else thumbnailRefs.current.delete(index);
                }}
                type="button"
                className="action-conversation__thumb"
                aria-label={copy.open(index + 1, images.length)}
                onClick={() => setOpenIndex(index)}
              >
                <img
                  src={buildActionImageUrl(image.storage_path)}
                  alt={copy.imageAlt(index + 1, images.length)}
                  loading="lazy"
                  decoding="async"
                  width={THUMBNAIL_SIZE_PX}
                  height={THUMBNAIL_SIZE_PX}
                  // A deleted or unreadable file must read as a stated absence, not as a broken
                  // image icon with alt text nobody sees.
                  onError={() => setMissing((current) => new Set(current).add(image.storage_path))}
                />
              </button>
            )}
          </li>
        ))}
      </ul>
      {open ? (
        <ImageLightbox
          storagePath={open.image.storage_path}
          copy={{
            ...copy.lightbox,
            imageAlt: copy.imageAlt(open.position, images.length),
          }}
          onClose={closeLightbox}
          onReveal={
            revealImage ? (storagePath) => void revealImage({ storagePath }).catch(() => {}) : null
          }
        />
      ) : null}
    </>
  );
}
