import React from 'react';

import { AiConnectionNotice } from '@/components/AiConnectionNotice';
import { useI18n } from '@/context/useI18n';
import { WorkspaceSettingsSection } from './settings/components/WorkspaceSettingsSection';

const WorkspacePage: React.FC = () => {
  const { t } = useI18n();
  return <WorkspaceSettingsSection notice={<AiConnectionNotice />} t={t} />;
};

export default WorkspacePage;
