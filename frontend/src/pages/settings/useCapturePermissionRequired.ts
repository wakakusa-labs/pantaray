import { useEffect, useState } from 'react';

/**
 * Whether recording cannot run until macOS grants a permission, as the recorder's own status
 * says. Main pushes every change it reads; the window reads again when it regains focus,
 * which is how the user comes back from System Settings.
 */
export function useCapturePermissionRequired(): boolean {
  const [isRequired, setIsRequired] = useState(false);
  useEffect(() => {
    const { getPermissionRequired, onPermissionRequiredChanged } =
      window.electron?.screenshot ?? {};
    if (!getPermissionRequired || !onPermissionRequiredChanged) return;
    let active = true;
    const unsubscribe = onPermissionRequiredChanged((next) => {
      if (active) setIsRequired(next);
    });
    const read = () => {
      getPermissionRequired().then(
        (next) => {
          if (active) setIsRequired(next);
        },
        (error: unknown) => console.error('Failed to read the recording status:', error)
      );
    };
    read();
    window.addEventListener('focus', read);
    return () => {
      active = false;
      unsubscribe();
      window.removeEventListener('focus', read);
    };
  }, []);
  return isRequired;
}
