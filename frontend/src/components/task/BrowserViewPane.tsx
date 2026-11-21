import { useEffect, useId, useLayoutEffect, useRef, useState, type MutableRefObject } from 'react';
import { X } from 'lucide-react';

import type {
  BrowserViewEndReason,
  BrowserViewUpdate,
} from '../../../electron/src/browser/pageViewer';
import { useI18n } from '@/context/useI18n';

import './taskFiles.css';
import './browserViewPane.css';

type Frame = Extract<BrowserViewUpdate, { kind: 'frame' }>;
type Page = Readonly<{ url: string; title: string }>;
type Phase = 'connecting' | 'live' | 'not_live' | 'failed' | BrowserViewEndReason;

const STATUS_KEYS = {
  connecting: 'overlay.browserView.connecting',
  live: null,
  not_live: 'overlay.browserView.notLive',
  failed: 'overlay.browserView.failed',
  work_ended: 'overlay.browserView.workEnded',
  page_crashed: 'overlay.browserView.pageCrashed',
} as const;

/**
 * The latest frame. Frames arrive many times a second, so only this image renders for each one;
 * the pane hands it each frame through `sinkRef`.
 */
function FrameImage({
  sinkRef,
  alt,
  ended,
}: {
  sinkRef: MutableRefObject<((frame: Frame) => void) | null>;
  alt: string;
  ended: boolean;
}) {
  const [frame, setFrame] = useState<Frame | null>(null);
  useLayoutEffect(() => {
    sinkRef.current = setFrame;
    return () => {
      sinkRef.current = null;
    };
  }, [sinkRef]);
  if (frame === null) return null;
  return (
    <img
      className={ended ? 'browser-view__frame browser-view__frame--ended' : 'browser-view__frame'}
      src={`data:image/jpeg;base64,${frame.jpeg}`}
      width={frame.width}
      height={frame.height}
      alt={alt}
    />
  );
}

/**
 * The app's page an Action works on, view only: main shows the page the Action touched last and
 * follows it. Frames stay in memory. The page's title and URL are the page's own text, so they
 * are shown as text only, never as markup or a link.
 */
export function BrowserViewPane({ actionId, onClose }: { actionId: string; onClose: () => void }) {
  const { t } = useI18n();
  const headingId = useId();
  const browserView = window.electron?.browserView;
  if (!browserView) throw new Error('Missing browser view bridge.');
  const [phase, setPhase] = useState<Phase>('connecting');
  const [page, setPage] = useState<Page | null>(null);
  const showFrame = useRef<((frame: Frame) => void) | null>(null);

  useEffect(() => {
    let active = true;
    let watchId: string | null = null;
    let framed = false;
    // Subscribed first; until the watch id is known, nothing is this pane's.
    const stopUpdates = browserView.onUpdate((update) => {
      if (watchId === null || update.watchId !== watchId) return;
      if (update.kind === 'frame') {
        showFrame.current?.(update);
        if (!framed) {
          framed = true;
          setPhase((current) => (current === 'connecting' ? 'live' : current));
        }
      } else if (update.kind === 'page') {
        setPage({ url: update.url, title: update.title });
      } else {
        setPhase(update.reason);
      }
    });
    const unwatch = (id: string) =>
      browserView.unwatch({ watchId: id }).catch((error: unknown) => {
        console.error('Failed to stop watching the browser page:', error);
      });
    browserView.watch({ actionId }).then(
      (result) => {
        if (result.kind === 'not_live') {
          if (active) setPhase('not_live');
        } else if (active) {
          watchId = result.watchId;
        } else {
          void unwatch(result.watchId);
        }
      },
      (error: unknown) => {
        console.error('Failed to watch the browser page:', error);
        if (active) setPhase('failed');
      }
    );
    return () => {
      active = false;
      stopUpdates();
      if (watchId !== null) void unwatch(watchId);
    };
  }, [browserView, actionId]);

  const name = page?.title || page?.url || '';
  const statusKey = STATUS_KEYS[phase];
  return (
    <section className="task-file-preview browser-view" aria-labelledby={headingId}>
      <header className="task-file-preview__header">
        <div className="task-file-preview__title">
          <h2 id={headingId}>{page?.title || t('overlay.browserView.heading')}</h2>
          {page?.url ? <p className="browser-view__url">{page.url}</p> : null}
        </div>
        <button
          type="button"
          className="task-file-preview__close"
          aria-label={t('overlay.browserView.close')}
          data-tooltip={t('overlay.browserView.close')}
          onClick={onClose}
        >
          <X size={16} strokeWidth={1.8} aria-hidden />
        </button>
      </header>
      <div className="task-file-preview__body">
        {/* Only the state is announced; a new frame changes nothing in here. */}
        <p className="task-file-preview__status" role="status">
          {statusKey === null ? '' : t(statusKey)}
        </p>
        {phase === 'not_live' || phase === 'failed' ? null : (
          <div className="browser-view__screen">
            <FrameImage
              sinkRef={showFrame}
              alt={
                name
                  ? t('overlay.browserView.screen', { page: name })
                  : t('overlay.browserView.untitledScreen')
              }
              ended={phase === 'work_ended' || phase === 'page_crashed'}
            />
          </div>
        )}
      </div>
    </section>
  );
}
