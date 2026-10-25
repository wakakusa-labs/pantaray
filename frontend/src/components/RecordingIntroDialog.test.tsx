import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { LocalOwnerContext } from '@/context/localOwnerContext';
import { UiLanguageProvider } from '@/context/UiLanguageContext';
import { RecordingIntroDialog } from './RecordingIntroDialog';

const originalShowModal = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, 'showModal');
const showModal = vi.fn(function (this: HTMLDialogElement) {
  this.setAttribute('open', '');
});
const OWNER = { kind: 'account', id: 'user-1' } as const;

// The screen is mounted inside the owner boundary, which is what ends it when the
// owner changes.
function wrap(children: ReactNode) {
  return (
    <LocalOwnerContext.Provider value={OWNER}>
      <UiLanguageProvider initialLanguage="ja">{children}</UiLanguageProvider>
    </LocalOwnerContext.Provider>
  );
}

/** `waiting`: a conversation was asked for without the permissions, which opens the screen. */
function stubBridge(start = vi.fn(async () => 'started'), waiting = true) {
  // The gate lives in main: "later" drops the waiting conversation there, so the stub
  // answers it the same way rather than in the window that asked.
  const gate = {
    permissions: { accessibility: false, screenRecording: false },
    introRequested: waiting,
  };
  const dismissIntro = vi.fn(async (_ownerId: string) =>
    Object.assign(gate, { introRequested: false })
  );
  const askForPermission = vi.fn(async (_permission: string) => undefined);
  let askForReread: () => void = () => undefined;
  window.electron = {
    screenshot: {
      getStatus: vi.fn(async () => false),
      onStatusChanged: vi.fn(() => () => undefined),
      start,
      stop: vi.fn(async () => true),
      getGateState: vi.fn(async () => ({ ...gate, permissions: { ...gate.permissions } })),
      dismissIntro,
      askForPermission,
      onGateStateChanged: vi.fn((callback: () => void) => {
        askForReread = callback;
        return () => {
          askForReread = () => undefined;
        };
      }),
    },
  } as unknown as Window['electron'];
  return {
    dismissIntro,
    askForPermission,
    start,
    // A conversation asked for without the macOS permissions opens no window: main
    // holds the request and brings this window forward, which re-reads the gate.
    deferConversation: () => Object.assign(gate, { introRequested: true }),
    // The permissions arriving, which main learns from this very read: a conversation
    // it was holding opens there and then, so the read reports it no longer waiting.
    grantPermissions: () =>
      Object.assign(gate, {
        permissions: { accessibility: true, screenRecording: true },
        introRequested: false,
      }),
    // Accessibility granted while Screen Recording still waits on a reopen.
    grantAccessibility: () => Object.assign(gate.permissions, { accessibility: true }),
    // The user taking the permissions away again in System Settings.
    revokePermissions: () =>
      Object.assign(gate, { permissions: { accessibility: false, screenRecording: false } }),
    // Main asking this window to read the gate again, with no focus to react to.
    askForReread: async () => {
      await act(async () => {
        askForReread();
      });
    },
  };
}

/** Main brings this window forward, and the browser reports the focus it was given. */
async function focusWindow() {
  await act(async () => {
    window.dispatchEvent(new Event('focus'));
  });
}

describe('RecordingIntroDialog', () => {
  beforeEach(() => {
    localStorage.setItem('pantaray_ui_language', 'ja');
    Object.defineProperty(HTMLDialogElement.prototype, 'showModal', {
      configurable: true,
      value: showModal,
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    localStorage.clear();
    delete window.electron;
    if (originalShowModal) {
      Object.defineProperty(HTMLDialogElement.prototype, 'showModal', originalShowModal);
    } else {
      Reflect.deleteProperty(HTMLDialogElement.prototype, 'showModal');
    }
  });

  it('does not open on its own while nothing waits on the permissions', async () => {
    // First-run setup is where recording is turned on; this screen is only what a
    // conversation stopped by the missing permissions turns into.
    stubBridge(undefined, false);
    render(wrap(<RecordingIntroDialog />));

    await waitFor(() => expect(window.electron?.screenshot?.getGateState).toHaveBeenCalled());
    expect(showModal).not.toHaveBeenCalled();
    expect(screen.queryByText('記録を始める', { selector: 'h2' })).not.toBeInTheDocument();
  });

  it('shows the screen for a waiting conversation, and closes it on a start', async () => {
    const { dismissIntro, start } = stubBridge();
    render(wrap(<RecordingIntroDialog />));

    expect(await screen.findByText('記録を始める', { selector: 'h2' })).toBeInTheDocument();

    fireEvent.click(await screen.findByRole('button', { name: '記録を始める' }));

    await waitFor(() => expect(start).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(dismissIntro).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.queryByText('記録を始める', { selector: 'h2' })).not.toBeInTheDocument()
    );
  });

  it('explains recording in four paragraphs and nothing else', async () => {
    stubBridge();
    render(wrap(<RecordingIntroDialog />));

    const dialog = await screen.findByRole('dialog');
    expect(
      Array.from(dialog.querySelectorAll('.recording-intro-body'), (line) => line.textContent)
    ).toEqual([
      'Pantaray はこの Mac での操作を記録し、それをもとに提案や作業をします。',
      '記録するのは、使っているアプリ、ウィンドウの名前、画面の文字、入力した内容です。',
      'ページの中身は、Chrome ではシークレットウィンドウを除いて記録します。Safari や Firefox などでは記録しません。ウィンドウの名前はどのブラウザでも記録します。',
      'パスワード管理アプリと、サインイン・決済のページの中身は記録しません。記録そのものは、この Mac に最大 48 時間だけ保存されます。そこから作る作業のまとめは、この Mac に残ります。提案や作業に必要な部分は、選んだ AI に送ります。',
    ]);
    // The earlier screen spelled the same facts out in a browser table and a bullet
    // list; the short version replaces both, so neither may come back unnoticed.
    expect(dialog.querySelector('table')).toBeNull();
    expect(dialog.querySelector('ul')).toBeNull();
    expect(screen.getByText('除外するアプリやサイトは設定で変えられます')).toBeInTheDocument();
  });

  it('keeps the screen open with an error when recording does not start', async () => {
    // `screenshotSync.start()` reports a failed start when the runtime is not ready yet.
    const { dismissIntro, start } = stubBridge(vi.fn(async () => 'failed'));
    render(wrap(<RecordingIntroDialog />));

    fireEvent.click(await screen.findByRole('button', { name: '記録を始める' }));

    await waitFor(() => expect(start).toHaveBeenCalledTimes(1));
    await screen.findByText('記録を開始できませんでした。もう一度お試しください。');
    // A failure that is not about permissions has no pane to send the user to.
    expect(screen.queryByRole('button', { name: 'システム設定を開く' })).toBeNull();
    // Answering the screen without the permissions would leave the user with no way back.
    expect(dismissIntro).not.toHaveBeenCalled();
    expect(screen.getByText('記録を始める', { selector: 'h2' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '記録を始める' })).toBeEnabled();
  });

  it('keeps the screen open with what to grant while macOS permission is missing', async () => {
    // The collector is up but recording nothing, so the screen has no permit to record.
    const { dismissIntro, start, askForPermission, grantAccessibility, askForReread } = stubBridge(
      vi.fn(async () => 'permission_pending')
    );
    render(wrap(<RecordingIntroDialog />));

    fireEvent.click(await screen.findByRole('button', { name: '記録を始める' }));

    await waitFor(() => expect(start).toHaveBeenCalledTimes(1));
    await screen.findByText(
      'アクセシビリティ、入力監視、画面収録の許可が必要です。システム設定で許可してから、もう一度「記録を始める」を押してください。画面収録を許可したら、Pantaray を終了して開き直してください。'
    );
    expect(dismissIntro).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: '記録を始める' })).toBeEnabled();

    // The permission is granted outside the app, so the screen offers the pane it
    // is granted in rather than leaving the user to find it: the first one missing.
    fireEvent.click(screen.getByRole('button', { name: 'システム設定を開く' }));
    expect(askForPermission).toHaveBeenLastCalledWith('read_accessibility_tree');
    grantAccessibility();
    await askForReread();
    fireEvent.click(screen.getByRole('button', { name: 'システム設定を開く' }));
    expect(askForPermission).toHaveBeenLastCalledWith('capture_screen');
  });

  it('cannot be answered while a start it cannot cancel is still running', async () => {
    let release: (result: string) => void = () => undefined;
    const { dismissIntro } = stubBridge(
      vi.fn(
        () =>
          new Promise<string>((resolve) => {
            release = resolve;
          })
      )
    );
    render(wrap(<RecordingIntroDialog />));

    fireEvent.click(await screen.findByRole('button', { name: '記録を始める' }));

    const later = screen.getByRole('button', { name: 'あとで' });
    await waitFor(() => expect(later).toBeDisabled());
    fireEvent(screen.getByRole('dialog'), new Event('cancel', { cancelable: true }));
    expect(dismissIntro).not.toHaveBeenCalled();

    release('started');
    await waitFor(() => expect(dismissIntro).toHaveBeenCalledTimes(1));
  });

  it('keeps the screen open with an error when starting recording rejects', async () => {
    const { dismissIntro } = stubBridge(
      vi.fn(async () => {
        throw new Error('collector crashed');
      })
    );
    render(wrap(<RecordingIntroDialog />));

    fireEvent.click(await screen.findByRole('button', { name: '記録を始める' }));

    await screen.findByText('記録を開始できませんでした。もう一度お試しください。');
    expect(dismissIntro).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: '記録を始める' })).toBeEnabled();
  });

  it('drops the waiting conversation without starting recording when the user chooses later', async () => {
    const { dismissIntro, start } = stubBridge();
    render(wrap(<RecordingIntroDialog />));

    fireEvent.click(await screen.findByRole('button', { name: 'あとで' }));

    await waitFor(() => expect(dismissIntro).toHaveBeenCalledTimes(1));
    expect(start).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(screen.queryByText('記録を始める', { selector: 'h2' })).not.toBeInTheDocument()
    );
  });

  it('opens when a conversation starts waiting, and "later" drops it for good', async () => {
    // A conversation asked for without the macOS permissions opens no window: main
    // holds the request and brings this window forward, and the gate says so.
    const { dismissIntro, deferConversation } = stubBridge(undefined, false);
    render(wrap(<RecordingIntroDialog />));
    await waitFor(() => expect(window.electron?.screenshot?.getGateState).toHaveBeenCalled());

    deferConversation();
    await focusWindow();
    expect(await screen.findByText('記録を始める', { selector: 'h2' })).toBeInTheDocument();

    // "Later" drops the waiting conversation in main, so the screen stops being asked
    // for and does not come back on the next focus.
    fireEvent.click(screen.getByRole('button', { name: 'あとで' }));
    await waitFor(() => expect(dismissIntro).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.queryByText('記録を始める', { selector: 'h2' })).not.toBeInTheDocument()
    );
    await focusWindow();
    expect(screen.queryByText('記録を始める', { selector: 'h2' })).not.toBeInTheDocument();
  });

  it('opens for a waiting conversation when this window was already in front', async () => {
    // `focus()` on the frontmost window fires no focus event, so main asks for the
    // re-read itself — the gate it reads back stays the source of truth.
    const { deferConversation, askForReread } = stubBridge(undefined, false);
    render(wrap(<RecordingIntroDialog />));
    await waitFor(() => expect(window.electron?.screenshot?.getGateState).toHaveBeenCalled());

    deferConversation();
    await askForReread();

    expect(await screen.findByText('記録を始める', { selector: 'h2' })).toBeInTheDocument();
  });

  it('closes a screen it no longer has anything to ask once the permissions are granted', async () => {
    const { grantPermissions } = stubBridge();
    render(wrap(<RecordingIntroDialog />));
    expect(await screen.findByText('記録を始める', { selector: 'h2' })).toBeInTheDocument();

    // Recording started from the tray while this screen was open: macOS granted the
    // permissions, main opened the conversation it deferred, so the screen has
    // nothing left to ask for.
    grantPermissions();
    await focusWindow();
    await waitFor(() =>
      expect(screen.queryByText('記録を始める', { selector: 'h2' })).not.toBeInTheDocument()
    );
  });

  it('reopens for a waiting conversation without the failure notice of an earlier attempt', async () => {
    const { deferConversation } = stubBridge(vi.fn(async () => 'failed'));
    render(wrap(<RecordingIntroDialog />));

    fireEvent.click(await screen.findByRole('button', { name: '記録を始める' }));
    await screen.findByText('記録を開始できませんでした。もう一度お試しください。');
    fireEvent.click(screen.getByRole('button', { name: 'あとで' }));
    await waitFor(() =>
      expect(screen.queryByText('記録を始める', { selector: 'h2' })).not.toBeInTheDocument()
    );

    deferConversation();
    await focusWindow();
    expect(await screen.findByText('記録を始める', { selector: 'h2' })).toBeInTheDocument();
    expect(
      screen.queryByText('記録を開始できませんでした。もう一度お試しください。')
    ).not.toBeInTheDocument();
  });

  it('reopens without the notice of an attempt made before the permissions were granted', async () => {
    // Granting the permissions closes the screen without it being answered, so the
    // notice of the attempt that asked for them is still there when taking them away
    // again and asking for another conversation puts the screen back in front.
    const { grantPermissions, revokePermissions, deferConversation } = stubBridge(
      vi.fn(async () => 'permission_pending')
    );
    render(wrap(<RecordingIntroDialog />));

    fireEvent.click(await screen.findByRole('button', { name: '記録を始める' }));
    await screen.findByText(
      'アクセシビリティ、入力監視、画面収録の許可が必要です。システム設定で許可してから、もう一度「記録を始める」を押してください。画面収録を許可したら、Pantaray を終了して開き直してください。'
    );

    grantPermissions();
    await focusWindow();
    revokePermissions();
    deferConversation();
    await focusWindow();

    expect(await screen.findByText('記録を始める', { selector: 'h2' })).toBeInTheDocument();
    expect(
      screen.queryByText(
        'アクセシビリティ、入力監視、画面収録の許可が必要です。システム設定で許可してから、もう一度「記録を始める」を押してください。画面収録を許可したら、Pantaray を終了して開き直してください。'
      )
    ).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'システム設定を開く' })).toBeNull();
  });

  it('never opens once macOS has granted the permissions', async () => {
    // The screen exists to obtain them; nothing else may put it in front of a user.
    const { grantPermissions } = stubBridge();
    grantPermissions();
    render(wrap(<RecordingIntroDialog />));

    await waitFor(() => expect(window.electron?.screenshot?.getGateState).toHaveBeenCalled());
    expect(showModal).not.toHaveBeenCalled();
    expect(screen.queryByText('記録を始める', { selector: 'h2' })).not.toBeInTheDocument();
  });

  it('answers the screen in the name of the owner it was shown to', async () => {
    // Granting the permissions happens in System Settings, so a start waits there long
    // enough for a browser login to change the local owner and end this screen with it.
    // Main holds one gate for the whole app run, so the answer carried by that late
    // reply names the owner that asked for it, and main takes it only while that owner
    // is still the current one.
    let finishStart!: (result: string) => void;
    const { dismissIntro, start } = stubBridge(
      vi.fn(
        () =>
          new Promise<string>((resolve) => {
            finishStart = resolve;
          })
      )
    );
    const view = render(wrap(<RecordingIntroDialog />));

    fireEvent.click(await screen.findByRole('button', { name: '記録を始める' }));
    await waitFor(() => expect(start).toHaveBeenCalledTimes(1));
    view.unmount();

    await act(async () => finishStart('started'));

    expect(dismissIntro).toHaveBeenCalledWith(OWNER.id);
  });
});
