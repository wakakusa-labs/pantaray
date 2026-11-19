/**
 * The notice Electron main shows while the user's browser asks the user to allow Pantaray in. A
 * system notification takes no focus, so the browser's own dialog stays in front.
 */

import { Notification } from 'electron';

import type { UiLanguage } from '../ui/uiLanguage';

const BODY: Record<UiLanguage, (browserName: string) => string> = {
  en: (browserName) => `${browserName} will ask you to allow Pantaray to use it now.`,
  ja: (browserName) => `${browserName} に、Pantaray が使ってもよいかの確認が出ます。`,
};

/** Shows the notice without waiting for it; the returned function takes it down. */
export function showUserBrowserNotice(browserName: string, language: UiLanguage): () => void {
  const notification = new Notification({ body: BODY[language](browserName), silent: true });
  notification.show();
  return () => notification.close();
}
