import React, { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';

import { AiConnectionNotice } from '@/components/AiConnectionNotice';
import { LocalOwnerBoundary } from '@/components/LocalOwnerBoundary';
import { RecordingChecklist } from '@/components/RecordingChecklist';
import { useStartPermissions } from '@/components/useStartPermissions';
import { useI18n } from '@/context/useI18n';
import type { MessageKey } from '@/i18n/types';
import type { CapturePermission } from '../../electron/src/context/zaneiProcess';
import { LanguageSection } from './settings/components/LanguageSection';
import { AiConnectionSettingsSection } from './settings/components/AiConnectionSettingsSection';
import { ApprovalModeSection } from './settings/components/ApprovalModeSection';
import { IdeFileRulesSection } from './settings/components/IdeFileRulesSection';
import { IntegrationSettings } from './settings/components/IntegrationSettings';
import { OverlayPositionSection } from './settings/components/OverlayPositionSection';
import { RecordingFilterDialog } from './settings/components/RecordingFilterDialog';
import { RecordingFilterSection } from './settings/components/RecordingFilterSection';
import { RecordingSwitch } from './settings/components/RecordingSwitch';
import { ShortcutSection } from './settings/components/ShortcutSection';
import type { Translate } from './settings/types';
import { useCapturePermissionRequired } from './settings/useCapturePermissionRequired';
import { useSettingsPageController } from './settings/useSettingsPageController';
import './settings/settingsPage.css';

type SettingsSectionId =
  | 'ai_connection'
  | 'language'
  | 'execution'
  | 'integrations'
  | 'shortcuts'
  | 'overlay_position'
  | 'screenshots';

const SETTINGS_SECTIONS: SettingsSectionId[] = [
  'ai_connection',
  'language',
  'execution',
  'integrations',
  'shortcuts',
  'overlay_position',
  'screenshots',
];

/**
 * Recording and execution act on the local owner's own data. The AI connection, the
 * language, the shortcut and the overlay position belong to the installation, and the connection section owns a
 * browser sign-in that its unmount cancels, so none of them may hang off the owner.
 */
const OWNER_SCOPED_SECTIONS: SettingsSectionId[] = ['execution', 'screenshots'];

const PERMISSION_TITLES = {
  read_accessibility_tree: 'settings.screenshotCapture.permission.accessibility',
  observe_input: 'settings.screenshotCapture.permission.inputMonitoring',
  capture_screen: 'settings.screenshotCapture.permission.screenRecording',
  automate_browser: 'settings.screenshotCapture.permission.chrome',
  automate_safari: 'settings.screenshotCapture.permission.safari',
} as const satisfies Record<CapturePermission, MessageKey>;

function getSectionFromSearch(value: string | null): SettingsSectionId {
  return SETTINGS_SECTIONS.includes(value as SettingsSectionId)
    ? (value as SettingsSectionId)
    : 'language';
}

const SettingsPage: React.FC = () => {
  const { t, language, setLanguage } = useI18n();
  const [searchParams, setSearchParams] = useSearchParams();
  const [activeSection, setActiveSection] = useState<SettingsSectionId>(() =>
    getSectionFromSearch(searchParams.get('section'))
  );

  useEffect(() => {
    setActiveSection(getSectionFromSearch(searchParams.get('section')));
  }, [searchParams]);

  const sectionLabels: Record<SettingsSectionId, string> = {
    ai_connection: t('settings.aiConnection.title'),
    language: t('settings.language.title'),
    execution: t('settings.approvalMode.title'),
    integrations: t('settings.integrations.title'),
    shortcuts: t('settings.shortcut.title'),
    overlay_position: t('settings.overlayPosition.title'),
    screenshots: t('settings.screenshotCapture.title'),
  };

  return (
    <div className="app-split">
      <aside className="app-split-master settings-page-nav" aria-label={t('nav.settings')}>
        <h2 className="app-split-title settings-page-nav-title">{t('nav.settings')}</h2>
        {SETTINGS_SECTIONS.map((sectionId) => {
          const isActive = activeSection === sectionId;
          return (
            <button
              key={sectionId}
              type="button"
              className={[
                'settings-page-nav-item',
                isActive ? 'settings-page-nav-item--active' : null,
              ]
                .filter(Boolean)
                .join(' ')}
              aria-current={isActive ? 'page' : undefined}
              onClick={() => {
                setActiveSection(sectionId);
                setSearchParams({ section: sectionId });
              }}
            >
              {sectionLabels[sectionId]}
            </button>
          );
        })}
      </aside>

      <div className="app-split-detail settings-page-content">
        <AiConnectionNotice />
        {activeSection === 'ai_connection' ? <AiConnectionSettingsSection part="all" /> : null}
        {activeSection === 'language' ? (
          <LanguageSection language={language} setLanguage={setLanguage} t={t} />
        ) : null}
        {activeSection === 'integrations' ? <IntegrationSettings t={t} /> : null}
        {activeSection === 'shortcuts' ? <ShortcutSection t={t} /> : null}
        {activeSection === 'overlay_position' ? <OverlayPositionSection t={t} /> : null}
        {/* Editing the filter pauses the recorder, and that pause outlives a section
            change, so this stays mounted for every section. Only a section that is itself
            owner-scoped has anything to report when there is no owner. */}
        <LocalOwnerBoundary
          fallback={OWNER_SCOPED_SECTIONS.includes(activeSection) ? undefined : null}
        >
          <OwnerScopedSections activeSection={activeSection} t={t} />
        </LocalOwnerBoundary>
      </div>
    </div>
  );
};

function OwnerScopedSections({
  activeSection,
  t,
}: {
  activeSection: SettingsSectionId;
  t: Translate;
}) {
  const controller = useSettingsPageController();

  return (
    <>
      {activeSection === 'execution' ? <ApprovalModeSection t={t} /> : null}
      {activeSection === 'screenshots' ? (
        <ScreenshotSections controller={controller} t={t} />
      ) : null}
      <RecordingFilterDialog
        filter={controller.captureFilter}
        installedApps={controller.installedApps}
        installedAppsError={controller.installedAppsError}
        isLoadingInstalledApps={controller.isLoadingInstalledApps}
        isOpen={controller.isFilterDialogOpen}
        saveError={controller.captureFilterError}
        t={t}
        onCancel={() => void controller.closeFilterDialog()}
        onSubmit={(filter) => void controller.saveCaptureFilter(filter)}
      />
    </>
  );
}

function ScreenshotSections({
  controller,
  t,
}: {
  controller: ReturnType<typeof useSettingsPageController>;
  t: Translate;
}) {
  if (controller.isCapturingScreenshots === null) {
    return (
      <div className="dashboard-section">
        <h3 className="dashboard-section-title">{t('settings.screenshotCapture.title')}</h3>
        <p
          className="dashboard-section-description"
          role={controller.captureStatusUnavailable ? 'alert' : undefined}
        >
          {t(
            controller.captureStatusUnavailable
              ? 'settings.screenshotCapture.unavailable'
              : 'settings.loadingStatus'
          )}
        </p>
      </div>
    );
  }

  return (
    <>
      <ScreenshotCaptureSection
        isCapturingScreenshots={controller.isCapturingScreenshots}
        isProcessing={controller.isProcessing}
        isScreenshotCaptureAvailable={controller.isScreenshotCaptureAvailable}
        setScreenshotsEnabled={controller.setScreenshotsEnabled}
        t={t}
      />
      <RecordingFilterSection
        hasCaptureSettings={controller.hasCaptureSettings}
        captureFilter={controller.captureFilter}
        captureFilterError={controller.captureFilterError}
        installedApps={controller.installedApps}
        isLoadingCaptureFilter={controller.isLoadingCaptureFilter}
        openFilterDialog={controller.openFilterDialog}
        t={t}
      />
      <IdeFileRulesSection
        ideFileRules={controller.ideFileRules}
        ideFileRulesError={controller.ideFileRulesError}
        ideSensitivePresets={controller.ideSensitivePresets}
        isLoadingIdeFileRules={controller.isLoadingIdeFileRules}
        setIdeSensitivePresetEnv={controller.setIdeSensitivePresetEnv}
        t={t}
      />
    </>
  );
}

function ScreenshotCaptureSection(props: {
  isCapturingScreenshots: boolean;
  isProcessing: boolean;
  isScreenshotCaptureAvailable: boolean;
  setScreenshotsEnabled: (enabled: boolean) => Promise<boolean>;
  t: Translate;
}) {
  const checks = useStartPermissions();
  const permissionRequired = useCapturePermissionRequired();
  const unavailable =
    (!props.isScreenshotCaptureAvailable && !props.isCapturingScreenshots) ||
    checks === 'unavailable';
  return (
    <div className="dashboard-section">
      <div className="settings-section-header">
        <div className="settings-section-heading">
          <h3 className="dashboard-section-title">{props.t('settings.screenshotCapture.title')}</h3>
          <div className="settings-section-status">
            {props.t(
              permissionRequired
                ? 'settings.screenshotCapture.permissionRequired'
                : props.isCapturingScreenshots
                  ? 'common.active'
                  : 'common.paused'
            )}
          </div>
        </div>
        <RecordingSwitch
          isCapturing={props.isCapturingScreenshots}
          canStart={props.isScreenshotCaptureAvailable}
          isProcessing={props.isProcessing}
          onChange={props.setScreenshotsEnabled}
        />
      </div>
      <p className="dashboard-section-description">
        {props.t('settings.screenshotCapture.description')}
      </p>
      {unavailable ? (
        <p className="dashboard-section-description settings-section-error">
          {props.t('settings.screenshotCapture.unavailable')}
        </p>
      ) : null}
      {Array.isArray(checks) ? (
        <RecordingChecklist t={props.t} checks={checks} titles={PERMISSION_TITLES} />
      ) : null}
    </div>
  );
}

export default SettingsPage;
