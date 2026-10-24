import { useCallback, useEffect, useRef, useState } from 'react';

import type { RecordingStartResult } from '../../../electron/src/screenshot/screenshotSync';

type CaptureStatus = 'active' | 'paused' | 'unavailable';

/**
 * Recording filters select inputs; they are not a prerequisite for starting capture.
 *
 * The status is the owner's. Below `LocalOwnerBoundary` the hook lives as long as one owner;
 * a caller outside it passes the published owner id so the read runs again once it exists.
 */
export function useScreenshotCaptureStatus(ownerId?: string | null) {
  const pending = useRef(false);
  const [isProcessing, setIsProcessing] = useState(false);
  const [status, setStatus] = useState<CaptureStatus | null>(null);
  const captureStatusUnavailable = status === 'unavailable';
  const isScreenshotCaptureAvailable = !captureStatusUnavailable;
  const isCapturingScreenshots = status === 'active' ? true : status === 'paused' ? false : null;

  useEffect(() => {
    let active = true;
    let receivedNotification = false;
    setStatus(null);
    const bridge = window.electron?.screenshot;
    const unsubscribe = bridge?.onStatusChanged?.((enabled) => {
      if (!active) return;
      receivedNotification = true;
      setStatus(enabled ? 'active' : 'paused');
    });
    const fetchInitialStatus = async () => {
      try {
        if (!bridge) throw new Error('Screenshot bridge is unavailable');
        const enabled = await bridge.getStatus();
        if (!active || receivedNotification) return;
        setStatus(enabled ? 'active' : 'paused');
      } catch {
        if (!active || receivedNotification) return;
        console.error('Failed to get screenshot status');
        setStatus('unavailable');
      }
    };
    void fetchInitialStatus();
    return () => {
      active = false;
      unsubscribe?.();
    };
  }, [ownerId]);

  // Both IPC writes share the synchronous duplicate-dispatch guard.
  const runCaptureOperation = useCallback(
    async <T>(operation: () => Promise<T>, failureResult: T): Promise<T> => {
      if (!isScreenshotCaptureAvailable || pending.current) return failureResult;
      pending.current = true;
      setIsProcessing(true);
      try {
        return await operation();
      } catch {
        console.error('Failed to change screenshot capture');
        return failureResult;
      } finally {
        pending.current = false;
        setIsProcessing(false);
      }
    },
    [isScreenshotCaptureAvailable]
  );

  /**
   * Always send this permit request to main, even when capture already reports on:
   * a collector awaiting macOS permission is on but has not granted a recording permit.
   */
  const startRecording = useCallback(
    (): Promise<RecordingStartResult> =>
      runCaptureOperation(() => window.electron!.screenshot!.start(), 'failed'),
    [runCaptureOperation]
  );

  const setScreenshotsEnabled = useCallback(
    async (enabled: boolean): Promise<boolean> => {
      if (!isScreenshotCaptureAvailable) return false;
      if (enabled === isCapturingScreenshots) return true;
      if (enabled) return (await startRecording()) === 'started';
      return runCaptureOperation(() => window.electron!.screenshot!.stop(), false);
    },
    [isScreenshotCaptureAvailable, isCapturingScreenshots, startRecording, runCaptureOperation]
  );

  return {
    isCapturingScreenshots,
    captureStatusUnavailable,
    isProcessing,
    isScreenshotCaptureAvailable,
    startRecording,
    setScreenshotsEnabled,
  };
}
