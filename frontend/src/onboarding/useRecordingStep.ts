import { useState } from 'react';

import { useAuth } from '@/hooks/useAuth';

import type { RecordingStartResult } from '../../electron/src/screenshot/screenshotSync';
import { useScreenshotCaptureStatus } from '../pages/settings/useScreenshotCaptureStatus';

/**
 * Recording as setup counts it, held by the step's parent so it can tell whether the step is
 * done. The capture status already reads on while a start waits on another macOS permission
 * (the recorder is up but has not been activated), so the start's own answer decides until a
 * later start reports `started`.
 *
 * Design limit: a recorder the app restored at launch that is still waiting reads as
 * recording, because only main knows the activation state; expose it if setup is seen to
 * finish with nothing recorded.
 */
export function useRecordingStep() {
  const { runtimeState } = useAuth();
  // Setup draws outside the owner boundary, so the status is read again once an owner exists.
  const capture = useScreenshotCaptureStatus(
    runtimeState.status === 'ready' ? (runtimeState.owner?.id ?? null) : null
  );
  const [startOutcome, setStartOutcome] = useState<Exclude<RecordingStartResult, 'started'> | null>(
    null
  );

  const start = async () => {
    setStartOutcome(null);
    const result = await capture.startRecording();
    if (result !== 'started') setStartOutcome(result);
  };

  const isRecording =
    capture.isCapturingScreenshots === null
      ? null
      : capture.isCapturingScreenshots && startOutcome !== 'permission_pending';
  return { capture, startOutcome, isRecording, start };
}
