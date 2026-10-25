import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from '@testing-library/react';
import { useEffect, type ReactNode } from 'react';
import { MemoryRouter, useNavigate } from 'react-router-dom';
import type { CaptureEditingRequest } from '../../../electron/src/screenshot/captureEditing';
import type { CapturePrivacySettings } from '../../../electron/src/privacy/capturePrivacy';
import SettingsPage from '../SettingsPage';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AuthContext, type AuthContextType } from '@/context/AuthContextDef';
import { LocalOwnerContext } from '@/context/localOwnerContext';
import { UiLanguageProvider } from '@/context/UiLanguageContext';
import { useI18n } from '@/context/useI18n';
import { useSettingsPageController } from './useSettingsPageController';

const OWNER = { kind: 'account', id: 'user-1' } as const;
const AUTH = {
  runtimeState: { status: 'ready', message: null, owner: OWNER },
} as AuthContextType;

// The controller is exercised directly inside its scope; a rendered SettingsPage puts its
// own boundary in between, and that boundary reads the runtime state.
function wrapper({ children }: { children: ReactNode }) {
  return (
    <AuthContext.Provider value={AUTH}>
      <LocalOwnerContext.Provider value={OWNER}>
        <UiLanguageProvider initialLanguage="ja">{children}</UiLanguageProvider>
      </LocalOwnerContext.Provider>
    </AuthContext.Provider>
  );
}

type PrivacyBridge = NonNullable<NonNullable<Window['electron']>['privacy']>;

function stubPrivacyBridge(privacy: Partial<PrivacyBridge>) {
  window.electron = {
    privacy: {
      getCaptureSettings: vi.fn(
        async (): Promise<CapturePrivacySettings> => ({
          version: 2,
          apps: { mode: 'exclude', entries: [] },
          websites: { mode: 'exclude', hosts: [] },
          ideFileRules: {
            mode: 'on',
            onFileNameUnavailable: 'allow',
            sensitivePresets: { blockEnvFiles: true },
          },
        })
      ),
      onCaptureSettingsUpdated: vi.fn(() => () => undefined),
      listInstalledApps: vi.fn(async () => []),
      setCaptureEditing: vi.fn(async () => true),
      ...privacy,
    },
    screenshot: {
      getStatus: vi.fn(async () => false),
      onStatusChanged: vi.fn(() => () => undefined),
      start: vi.fn(async () => 'started' as const),
      stop: vi.fn(async () => true),
      getPermissions: vi.fn(async () => []),
    },
  } as unknown as Window['electron'];
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  delete window.electron;
});

describe('useSettingsPageController', () => {
  it('opens the filter dialog only after capture is actually paused', async () => {
    let resolvePause: (paused: boolean) => void = () => undefined;
    const pause = new Promise<boolean>((resolve) => {
      resolvePause = resolve;
    });
    stubPrivacyBridge({ setCaptureEditing: vi.fn(() => pause) });

    const { result } = renderHook(() => useSettingsPageController(), { wrapper });
    await waitFor(() => expect(result.current.isLoadingCaptureFilter).toBe(false));

    let opening: Promise<void> = Promise.resolve();
    await act(async () => {
      opening = result.current.openFilterDialog();
      await Promise.resolve();
    });

    // While the pause is still in flight the dialog must not be on screen: anything
    // typed into the website field would otherwise be recorded.
    expect(result.current.isFilterDialogOpen).toBe(false);

    await act(async () => {
      resolvePause(true);
      await opening;
    });

    expect(result.current.isFilterDialogOpen).toBe(true);
    expect(result.current.captureFilterError).toBeNull();
  });

  it('enumerates installed apps for the summary and once per dialog open', async () => {
    const listInstalledApps = vi.fn(async () => [
      {
        name: 'Google Chrome',
        bundleId: 'com.google.Chrome',
        iconDataUrl: 'data:image/png;base64,chrome',
      },
    ]);
    stubPrivacyBridge({
      getCaptureSettings: vi.fn(
        async (): Promise<CapturePrivacySettings> => ({
          version: 2,
          apps: {
            mode: 'exclude',
            entries: [
              { name: 'Google Chrome', bundleId: 'com.google.Chrome' },
              { name: 'Slack', bundleId: 'com.tinyspeck.slackmacgap' },
            ],
          },
          websites: { mode: 'exclude', hosts: [] },
          ideFileRules: {
            mode: 'on',
            onFileNameUnavailable: 'allow',
            sensitivePresets: { blockEnvFiles: true },
          },
        })
      ),
      listInstalledApps,
    });

    const { result } = renderHook(() => useSettingsPageController(), { wrapper });
    await waitFor(() => expect(result.current.isLoadingCaptureFilter).toBe(false));

    await act(async () => {
      await result.current.openFilterDialog();
    });
    await act(async () => {
      await result.current.closeFilterDialog();
    });
    await act(async () => {
      await result.current.openFilterDialog();
    });

    // Once for the summary, which draws each listed app with its own icon, and then
    // once per open: an app installed while the dialog was closed must be offered.
    // Every listed row reads that one result instead of scanning for its own icon.
    expect(listInstalledApps).toHaveBeenCalledTimes(3);
    expect(result.current.installedApps).toHaveLength(1);
  });

  it('keeps the filter dialog closed and reports the error when pausing fails', async () => {
    stubPrivacyBridge({
      setCaptureEditing: vi.fn(async () => {
        throw new Error('capture transition failed');
      }),
      listInstalledApps: vi.fn(async () => []),
    });

    const { result } = renderHook(() => useSettingsPageController(), { wrapper });
    await waitFor(() => expect(result.current.isLoadingCaptureFilter).toBe(false));

    await act(async () => {
      await result.current.openFilterDialog();
    });

    expect(result.current.isFilterDialogOpen).toBe(false);
    expect(result.current.captureFilterError).not.toBeNull();
    // The picker must not run either: its results would be on screen while recording.
    // This filter lists no app, so the summary has no icon to fetch on its own.
    expect(window.electron?.privacy?.listInstalledApps).not.toHaveBeenCalled();
  });

  it('keeps the editor open and reports the error when capture cannot be resumed', async () => {
    const setCaptureEditing = vi.fn(async (request: CaptureEditingRequest) => {
      if (request.kind === 'end') throw new Error('recorder restart failed');
      return true;
    });
    stubPrivacyBridge({ setCaptureEditing });

    const { result } = renderHook(() => useSettingsPageController(), { wrapper });
    await waitFor(() => expect(result.current.isLoadingCaptureFilter).toBe(false));
    await act(async () => {
      await result.current.openFilterDialog();
    });
    expect(result.current.isFilterDialogOpen).toBe(true);

    await act(async () => {
      await result.current.closeFilterDialog();
    });

    // Closing onto a settings page that looks like recording is running would hide
    // that the recorder is actually stopped.
    expect(result.current.isFilterDialogOpen).toBe(true);
    expect(result.current.captureFilterError).not.toBeNull();
  });

  it('shows the IDE rules error instead of loading forever when settings fail to load', async () => {
    stubPrivacyBridge({
      getCaptureSettings: vi.fn(async () => {
        throw new Error('settings unavailable');
      }),
    });

    const { result } = renderHook(() => useSettingsPageController(), { wrapper });

    await waitFor(() => expect(result.current.isLoadingIdeFileRules).toBe(false));
    expect(result.current.ideFileRules).toBeNull();
    expect(result.current.ideFileRulesError).not.toBeNull();
    expect(result.current.captureFilterError).not.toBeNull();
  });
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

function settingsFor(host: string, blockEnvFiles = true): CapturePrivacySettings {
  return {
    version: 2,
    apps: { mode: 'exclude', entries: [] },
    websites: { mode: 'exclude', hosts: [host] },
    ideFileRules: {
      mode: blockEnvFiles ? 'on' : 'off',
      onFileNameUnavailable: 'allow',
      sensitivePresets: { blockEnvFiles },
    },
  };
}

it.each(['resolve', 'reject'] as const)(
  'keeps a settings notification over an older initial read %s',
  async (outcome) => {
    const initial = deferred<CapturePrivacySettings>();
    let notify!: (settings: CapturePrivacySettings) => void;
    stubPrivacyBridge({
      getCaptureSettings: vi.fn(() => initial.promise),
      onCaptureSettingsUpdated: vi.fn((callback) => {
        notify = callback;
        return () => undefined;
      }),
    });
    const { result } = renderHook(useSettingsPageController, { wrapper });
    act(() => notify(settingsFor('new.example', false)));
    await act(async () => {
      if (outcome === 'resolve') initial.resolve(settingsFor('old.example'));
      else initial.reject(new Error('old read failed'));
    });
    expect(result.current.captureFilter.websites.hosts).toEqual(['new.example']);
    expect(result.current.ideSensitivePresets.blockEnvFiles).toBe(false);
    expect(result.current.isLoadingCaptureFilter).toBe(false);
    expect(result.current.captureFilterError).toBeNull();
    expect(result.current.ideFileRulesError).toBeNull();
  }
);

it('recovers both settings sections on a notification after a failed read', async () => {
  let notify!: (settings: CapturePrivacySettings) => void;
  stubPrivacyBridge({
    getCaptureSettings: vi.fn(async () => {
      throw new Error('read failed');
    }),
    onCaptureSettingsUpdated: vi.fn((callback) => {
      notify = callback;
      return () => undefined;
    }),
  });
  const { result } = renderHook(useSettingsPageController, { wrapper });
  await waitFor(() => expect(result.current.captureFilterError).not.toBeNull());
  expect(result.current.hasCaptureSettings).toBe(false);
  act(() => notify(settingsFor('recovered.example')));
  expect(result.current.hasCaptureSettings).toBe(true);
  expect(result.current.captureFilterError).toBeNull();
  expect(result.current.ideFileRulesError).toBeNull();
});

it('loads settings when the bridge omits its optional notification subscription', async () => {
  stubPrivacyBridge({ onCaptureSettingsUpdated: undefined });
  const { result } = renderHook(useSettingsPageController, { wrapper });
  await waitFor(() => expect(result.current.hasCaptureSettings).toBe(true));
  expect(result.current.captureFilterError).toBeNull();
  expect(result.current.ideFileRulesError).toBeNull();
  await act(async () => {
    await result.current.openFilterDialog();
  });
  expect(result.current.isFilterDialogOpen).toBe(true);
});

it('keeps verified settings and relocalizes errors without rereading on a language change', async () => {
  stubPrivacyBridge({
    getCaptureSettings: vi
      .fn()
      .mockResolvedValueOnce(settingsFor('kept.example'))
      .mockRejectedValue(new Error('unnecessary read failed')),
    updateIdeFileRules: vi.fn(async () => {
      throw new Error('save failed');
    }),
  });
  const { result } = renderHook(() => ({ ...useSettingsPageController(), ...useI18n() }), {
    wrapper,
  });
  await waitFor(() => expect(result.current.hasCaptureSettings).toBe(true));
  await act(async () => {
    await result.current.setIdeSensitivePresetEnv(false);
  });
  expect(result.current.ideFileRulesError).not.toBeNull();
  await act(async () => {
    await result.current.setLanguage('en');
  });
  expect(result.current.hasCaptureSettings).toBe(true);
  expect(result.current.captureFilter.websites.hosts).toEqual(['kept.example']);
  expect(result.current.ideFileRulesError).toBe(
    result.current.t('settings.ideFileRules.saveFailed')
  );
  expect(window.electron!.privacy!.getCaptureSettings).toHaveBeenCalledTimes(1);
});

it.each(['resolve', 'reject'] as const)(
  'ignores an earlier IDE save %s after the next toggle',
  async (outcome) => {
    const first = deferred<CapturePrivacySettings['ideFileRules']>();
    let notify!: (settings: CapturePrivacySettings) => void;
    stubPrivacyBridge({
      updateIdeFileRules: vi
        .fn()
        .mockReturnValueOnce(first.promise)
        .mockImplementation(async (rules) => {
          notify(settingsFor('current.example', true));
          return rules;
        }),
      onCaptureSettingsUpdated: vi.fn((callback) => {
        notify = callback;
        return () => undefined;
      }),
    });
    const { result } = renderHook(useSettingsPageController, { wrapper });
    await waitFor(() => expect(result.current.hasCaptureSettings).toBe(true));
    let saving!: Promise<void>;
    act(() => {
      saving = result.current.setIdeSensitivePresetEnv(false);
      notify(settingsFor('current.example', false));
    });
    await act(async () => {
      await result.current.setIdeSensitivePresetEnv(true);
    });
    await act(async () => {
      if (outcome === 'resolve') first.resolve(settingsFor('current.example', false).ideFileRules);
      else first.reject(new Error('old reply lost'));
      await saving;
    });
    expect(result.current.ideSensitivePresets.blockEnvFiles).toBe(true);
    expect(result.current.ideFileRulesError).toBeNull();
  }
);

it.each(['matching', 'unrelated', 'before'] as const)(
  'handles failed save replies after a %s settings notification',
  async (notification) => {
    const filterSave = deferred<CapturePrivacySettings>();
    const ideSave = deferred<CapturePrivacySettings['ideFileRules']>();
    let notify!: (settings: CapturePrivacySettings) => void;
    const wanted = settingsFor('saved.example', false);
    stubPrivacyBridge({
      updateCaptureSettings: vi.fn(() => filterSave.promise),
      updateIdeFileRules: vi.fn(() => ideSave.promise),
      onCaptureSettingsUpdated: vi.fn((callback) => {
        notify = callback;
        return () => undefined;
      }),
    });
    const { result } = renderHook(useSettingsPageController, { wrapper });
    await waitFor(() => expect(result.current.hasCaptureSettings).toBe(true));
    await act(async () => {
      await result.current.openFilterDialog();
    });
    if (notification === 'before') act(() => notify(wanted));
    let saving!: Promise<void>;
    let savingIde!: Promise<void>;
    act(() => {
      saving = result.current.saveCaptureFilter(wanted);
      savingIde = result.current.setIdeSensitivePresetEnv(false);
      if (notification !== 'before') {
        notify(notification === 'matching' ? wanted : settingsFor('unrelated.example'));
      }
    });
    await act(async () => {
      filterSave.reject(new Error('settings reply lost'));
      ideSave.reject(new Error('IDE reply lost'));
      await Promise.all([saving, savingIde]);
    });
    if (notification === 'matching') {
      expect(result.current.captureFilterError).toBeNull();
      expect(result.current.ideFileRulesError).toBeNull();
      expect(result.current.isFilterDialogOpen).toBe(false);
    } else {
      expect(result.current.captureFilterError).not.toBeNull();
      expect(result.current.ideFileRulesError).not.toBeNull();
      expect(result.current.isFilterDialogOpen).toBe(true);
    }
  }
);

it('keeps newer settings notifications over both filter and IDE save replies', async () => {
  const filterSave = deferred<CapturePrivacySettings>();
  const ideSave = deferred<CapturePrivacySettings['ideFileRules']>();
  let notify!: (settings: CapturePrivacySettings) => void;
  stubPrivacyBridge({
    updateCaptureSettings: vi.fn(() => filterSave.promise),
    updateIdeFileRules: vi.fn(() => ideSave.promise),
    onCaptureSettingsUpdated: vi.fn((callback) => {
      notify = callback;
      return () => undefined;
    }),
  });
  const { result } = renderHook(useSettingsPageController, { wrapper });
  await waitFor(() => expect(result.current.isLoadingCaptureFilter).toBe(false));
  let saving!: Promise<void>;
  let savingIde!: Promise<void>;
  act(() => {
    saving = result.current.saveCaptureFilter(result.current.captureFilter);
    savingIde = result.current.setIdeSensitivePresetEnv(true);
    notify(settingsFor('latest.example', false));
  });
  await act(async () => {
    filterSave.resolve(settingsFor('old.example'));
    ideSave.resolve(settingsFor('old.example').ideFileRules);
    await Promise.all([saving, savingIde]);
  });
  expect(result.current.captureFilter.websites.hosts).toEqual(['latest.example']);
  expect(result.current.ideSensitivePresets.blockEnvFiles).toBe(false);
});

it('sends one pause for duplicate opens and releases an unmounted pending editor', async () => {
  const opening = deferred<boolean>();
  const requests: CaptureEditingRequest[] = [];
  stubPrivacyBridge({
    setCaptureEditing: vi.fn(async (request: CaptureEditingRequest) => {
      requests.push(request);
      return request.kind === 'begin' ? opening.promise : true;
    }),
  });
  const { result, unmount } = renderHook(useSettingsPageController, { wrapper });
  await waitFor(() => expect(result.current.isLoadingCaptureFilter).toBe(false));
  let firstOpening!: Promise<void>;
  await act(async () => {
    firstOpening = result.current.openFilterDialog();
    await result.current.openFilterDialog();
  });
  expect(requests).toHaveLength(1);
  unmount();
  expect(requests[1]).toEqual({ kind: 'end', sessionId: requests[0].sessionId });
  await act(async () => {
    opening.resolve(true);
    await firstOpening;
  });
  expect(window.electron?.privacy?.listInstalledApps).not.toHaveBeenCalled();
});

it('keeps the paused editor mounted when the settings URL changes sections', async () => {
  const showModal = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, 'showModal');
  const close = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, 'close');
  Object.defineProperties(HTMLDialogElement.prototype, {
    showModal: {
      configurable: true,
      value: function (this: HTMLDialogElement) {
        this.setAttribute('open', '');
      },
    },
    close: {
      configurable: true,
      value: function (this: HTMLDialogElement) {
        this.removeAttribute('open');
      },
    },
  });
  const requests: CaptureEditingRequest[] = [];
  stubPrivacyBridge({
    setCaptureEditing: vi.fn(async (request: CaptureEditingRequest) => {
      requests.push(request);
      return true;
    }),
  });
  let navigate!: ReturnType<typeof useNavigate>;
  function Page() {
    const nextNavigate = useNavigate();
    useEffect(() => {
      navigate = nextNavigate;
    }, [nextNavigate]);
    return <SettingsPage />;
  }
  try {
    render(
      wrapper({
        children: (
          <MemoryRouter initialEntries={['/settings?section=screenshots']}>
            <Page />
          </MemoryRouter>
        ),
      })
    );
    const edit = await screen.findByRole('button', { name: '変更' });
    await waitFor(() => expect(edit).toBeEnabled());
    fireEvent.click(edit);
    await screen.findByRole('dialog', { name: '記録するアプリとウェブサイト' });
    act(() => {
      void navigate('/settings?section=language');
    });
    expect(
      screen.getByRole('dialog', { name: '記録するアプリとウェブサイト' })
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'キャンセル' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(requests.map((request) => request.kind)).toEqual(['begin', 'end']);
  } finally {
    cleanup();
    if (showModal) Object.defineProperty(HTMLDialogElement.prototype, 'showModal', showModal);
    else Reflect.deleteProperty(HTMLDialogElement.prototype, 'showModal');
    if (close) Object.defineProperty(HTMLDialogElement.prototype, 'close', close);
    else Reflect.deleteProperty(HTMLDialogElement.prototype, 'close');
  }
});

it('does not open the editor when main refuses the pause', async () => {
  stubPrivacyBridge({ setCaptureEditing: vi.fn(async () => false) });
  const { result } = renderHook(useSettingsPageController, { wrapper });
  await waitFor(() => expect(result.current.isLoadingCaptureFilter).toBe(false));
  await act(async () => {
    await result.current.openFilterDialog();
  });
  expect(result.current.isFilterDialogOpen).toBe(false);
  expect(result.current.captureFilterError).not.toBeNull();
  expect(window.electron?.privacy?.listInstalledApps).not.toHaveBeenCalled();
});

it('releases the editor identity when its start reply fails after main may have paused', async () => {
  let paused = false;
  stubPrivacyBridge({
    setCaptureEditing: vi.fn(async (request: CaptureEditingRequest) => {
      if (request.kind === 'begin') {
        paused = true;
        throw new Error('reply failed');
      }
      paused = false;
      return true;
    }),
  });
  const { result } = renderHook(useSettingsPageController, { wrapper });
  await waitFor(() => expect(result.current.isLoadingCaptureFilter).toBe(false));
  await act(async () => {
    await result.current.openFilterDialog();
  });
  expect(paused).toBe(false);
  expect(result.current.isFilterDialogOpen).toBe(false);
  expect(result.current.captureFilterError).not.toBeNull();
});

it('does not end a reopened editor for the same owner when an earlier save finishes', async () => {
  const saved = deferred<CapturePrivacySettings>();
  const requests: CaptureEditingRequest[] = [];
  stubPrivacyBridge({
    setCaptureEditing: vi.fn(async (request: CaptureEditingRequest) => {
      requests.push(request);
      return true;
    }),
    updateCaptureSettings: vi.fn(() => saved.promise),
  });
  const { result } = renderHook(useSettingsPageController, { wrapper });
  await waitFor(() => expect(result.current.isLoadingCaptureFilter).toBe(false));
  await act(async () => {
    await result.current.openFilterDialog();
  });
  let saving!: Promise<void>;
  act(() => {
    saving = result.current.saveCaptureFilter(result.current.captureFilter);
  });
  await act(async () => {
    await result.current.closeFilterDialog();
  });
  await act(async () => {
    await result.current.openFilterDialog();
  });
  await act(async () => {
    saved.resolve(await window.electron!.privacy!.getCaptureSettings());
    await saving;
  });
  expect(result.current.isFilterDialogOpen).toBe(true);
  expect(requests.filter((request) => request.kind === 'end')).toHaveLength(1);
});
