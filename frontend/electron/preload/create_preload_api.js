const { createAuthHistoryApi } = require('./auth_history_api');
const { createActionsApi } = require('./actions_api');
const { createCaptureApi } = require('./capture_api');
const { createChatApi } = require('./chat_api');
const { createCoreApi } = require('./core_api');
const { createOrchestrationApi } = require('./orchestration_api');
const { createOverlayApi } = require('./overlay_api');
const { createSettingsApi } = require('./settings_api');
const { createSuggestionsApi } = require('./suggestions_api');
const { createTodosApi } = require('./todos_api');

function createPreloadApi(params) {
  return {
    ...createCoreApi(params),
    ...createAuthHistoryApi(params),
    ...createActionsApi(params),
    ...createChatApi(params),
    ...createTodosApi(params),
    ...createSuggestionsApi(params),
    ...createCaptureApi(params),
    ...createSettingsApi(params),
    ...createOverlayApi(params),
    ...createOrchestrationApi(params),
  };
}

module.exports = { createPreloadApi };
