import { useCallback, useEffect, useRef, useState } from 'react';

import { useAuth } from '@/hooks/useAuth';

import type {
  MeetingAvailability,
  MeetingPermission,
} from '../../electron/src/context/meetingTranscript';
import { PermissionCheckItem } from '../components/RecordingChecklist';
import type { MessageKey } from '../i18n/types';
import type { Translate } from '../pages/settings/types';

const ROWS = [
  {
    permission: 'microphone',
    title: 'onboarding.recording.microphoneTitle',
    note: 'onboarding.recording.microphoneNote',
    waiting: 'onboarding.recording.microphoneWaiting',
  },
  {
    permission: 'audio_capture',
    title: 'onboarding.recording.systemAudioTitle',
    note: 'onboarding.recording.systemAudioNote',
    waiting: 'onboarding.recording.systemAudioWaiting',
  },
] as const satisfies readonly {
  permission: MeetingPermission;
  title: MessageKey;
  note: MessageKey;
  waiting: MessageKey;
}[];

/** How long and how often the rows read the permissions again after an ask or a return. */
export const RECHECK = { everyMs: 3_000, forMs: 30_000 } as const;

/** Granted only when the recorder says so; it reports nothing about them while it is off. */
const isGranted = (availability: MeetingAvailability | null, permission: MeetingPermission) =>
  availability === 'available' ||
  (availability !== null &&
    'action_required' in availability &&
    !availability.action_required.includes(permission));

/**
 * Call transcription's macOS permissions, on a Mac that can transcribe calls. Recording never
 * waits on them. The running recorder asks for both at once and reports them, so they wait on
 * the start row.
 */
export function CallPermissionRows({
  t,
  recorderRunning,
}: {
  t: Translate;
  recorderRunning: boolean;
}) {
  const { runtimeState } = useAuth();
  const ownerId = runtimeState.status === 'ready' ? (runtimeState.owner?.id ?? null) : null;
  const [supported, setSupported] = useState(false);
  const [availability, setAvailability] = useState<MeetingAvailability | null>(null);
  // macOS asks once; after that only System Settings can change the answer.
  const [asked, setAsked] = useState(false);

  useEffect(() => {
    if (ownerId === null) return;
    let active = true;
    window.electron?.meeting?.getSettings().then(
      (next) => {
        if (active) setSupported(next.supported);
      },
      (error: unknown) => console.error('Failed to read call transcription support:', error)
    );
    return () => {
      active = false;
    };
  }, [ownerId]);

  const live = supported && recorderRunning;
  const latestRead = useRef(0);
  const read = useCallback(() => {
    const call = ++latestRead.current;
    window.electron?.meeting?.getAvailability().then(
      (next) => {
        if (call === latestRead.current) setAvailability(next);
      },
      (error: unknown) => console.error('Failed to read call transcription availability:', error)
    );
  }, []);
  // Starts another round of reads; the recorder can take a while to see an answer to macOS.
  const [rechecks, setRechecks] = useState(0);
  const allGranted = ROWS.every((row) => isGranted(availability, row.permission));

  // Read again whenever the window regains focus, which is how the user comes back from
  // macOS's popup or System Settings.
  useEffect(() => {
    if (!live) return;
    const onFocus = () => {
      read();
      setRechecks((count) => count + 1);
    };
    read();
    window.addEventListener('focus', onFocus);
    return () => {
      latestRead.current += 1;
      window.removeEventListener('focus', onFocus);
    };
  }, [live, read]);

  useEffect(() => {
    if (!live || rechecks === 0 || allGranted) return;
    const startedAt = Date.now();
    const timer = window.setInterval(() => {
      if (Date.now() - startedAt >= RECHECK.forMs) window.clearInterval(timer);
      else read();
    }, RECHECK.everyMs);
    return () => window.clearInterval(timer);
  }, [live, rechecks, allGranted, read]);

  if (!supported) return null;

  // `prepare` shows macOS's popup for each permission not yet asked about.
  const ask = async () => {
    setAsked(true);
    try {
      await window.electron?.meeting?.prepare();
    } catch (error) {
      // The rows stay waiting with System Settings one press away.
      console.error('Failed to ask for the call transcription permissions:', error);
    }
    setRechecks((count) => count + 1);
  };
  const openSettings = async (permission: MeetingPermission) => {
    try {
      await window.electron?.screenshot?.openPermissionSettings(permission);
    } catch (error) {
      console.error('Failed to open System Settings:', error);
    }
  };

  return ROWS.map((row) => {
    const granted = isGranted(availability, row.permission);
    return (
      <PermissionCheckItem
        key={row.permission}
        t={t}
        title={t(row.title)}
        granted={granted}
        note={t(!granted && asked ? row.waiting : row.note)}
        waiting={asked}
        disabled={!recorderRunning}
        onAsk={() => void ask()}
        onOpenSettings={() => void openSettings(row.permission)}
      />
    );
  });
}
