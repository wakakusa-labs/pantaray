const { createAuthHistoryApi } = require('./auth_history_api');
const { createActionsApi } = require('./actions_api');
const { createCaptureApi } = require('./capture_api');
const { createCoreApi } = require('./core_api');
const { createOrchestrationApi } = require('./orchestration_api');
const { createOverlayApi } = require('./overlay_api');
const { createSettingsApi } = require('./settings_api');

function createPreloadApi(params) {
  return {
    ...createCoreApi(params),
    ...createAuthHistoryApi(params),
    ...createActionsApi(params),
    ...createCaptureApi(params),
    ...createSettingsApi(params),
    ...createOverlayApi(params),
    ...createOrchestrationApi(params),
  };
}

module.exports = { createPreloadApi };
