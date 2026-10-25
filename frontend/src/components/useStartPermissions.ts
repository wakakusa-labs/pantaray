import { useEffect, useState } from 'react';

import type { CapturePermissionCheck } from '../../electron/src/context/zaneiProcess';

/**
 * What a start would wait on, as main reads it for that start. Read again whenever the
 * window regains focus, which is how the user comes back from System Settings.
 */
export function useStartPermissions(): CapturePermissionCheck[] | 'unavailable' | null {
  const [checks, setChecks] = useState<CapturePermissionCheck[] | 'unavailable' | null>(null);
  useEffect(() => {
    let active = true;
    let latest = 0;
    const read = () => {
      const call = ++latest;
      window.electron?.screenshot?.getPermissions().then(
        (next) => {
          if (active && call === latest) setChecks(next);
        },
        (error: unknown) => {
          console.error('Failed to read the recording permissions:', error);
          if (active && call === latest) setChecks('unavailable');
        }
      );
    };
    read();
    window.addEventListener('focus', read);
    return () => {
      active = false;
      window.removeEventListener('focus', read);
    };
  }, []);
  return checks;
}
