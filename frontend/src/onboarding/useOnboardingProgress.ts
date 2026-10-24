import { useEffect, useRef, useState } from 'react';

import type { OnboardingState } from '../../electron/src/ipc/schemas/onboarding';

export type OnboardingStep = Extract<OnboardingState, { status: 'in_progress' }>['step'];

function onboardingBridge() {
  const bridge = window.electron?.onboarding;
  if (!bridge) throw new Error('The onboarding bridge is unavailable.');
  return bridge;
}

/**
 * Where setup stands, saved as the step changes so a relaunch (the Screen Recording grant
 * needs one) resumes there. Leaving waits for `completed` to be saved, or the next launch
 * would show setup again.
 */
export function useOnboardingProgress() {
  const [state, setState] = useState<OnboardingState | null>(null);
  const [finishing, setFinishing] = useState<'idle' | 'saving' | 'failed'>('idle');
  // Set before the completed write is sent, so no step write can be sent after it.
  const saving = useRef(false);

  useEffect(() => {
    let active = true;
    (async () => onboardingBridge().getState())()
      .then((read) => {
        if (active) setState(read);
      })
      .catch((error: unknown) => console.error('Failed to read the setup state:', error));
    return () => {
      active = false;
    };
  }, []);

  const goTo = (step: OnboardingStep) => {
    if (saving.current) return;
    const next = { status: 'in_progress', step } as const;
    setState(next);
    // Losing this only resumes setup one step earlier; every step reads what is set up.
    (async () => onboardingBridge().setState(next))().catch((error: unknown) =>
      console.error('Failed to save the setup step:', error)
    );
  };

  const finish = async () => {
    if (saving.current) return;
    saving.current = true;
    setFinishing('saving');
    try {
      await onboardingBridge().setState({ status: 'completed' });
    } catch (error) {
      console.error('Failed to save that setup is complete:', error);
      saving.current = false;
      setFinishing('failed');
      return;
    }
    setState({ status: 'completed' });
  };

  return { state, goTo, finish, finishing };
}

export type OnboardingProgress = Omit<ReturnType<typeof useOnboardingProgress>, 'state'>;
