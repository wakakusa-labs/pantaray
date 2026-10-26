import type { OverlayBootstrapResponse, OverlayLiveResume } from '../orchestration/contracts';
import type { createLocalBackendClient } from '../localBackend/client';

type RawOverlayLiveResume = {
  kind: 'none' | 'suggestion' | 'action';
  process_id: string | null;
  action_id: string | null;
  command_id: string | null;
  accepted_at: string | null;
};

type RawOverlayBootstrapResponse = {
  suggestion_id: string;
  artifacts_root: string;
  snapshot: OverlayBootstrapResponse['snapshot'];
  last_sequence: number;
  live_resume: RawOverlayLiveResume;
};

function normalizeLiveResume(raw: RawOverlayLiveResume): OverlayLiveResume {
  return {
    kind: raw.kind,
    processId: raw.process_id,
    actionId: raw.action_id,
    commandId: raw.command_id,
    acceptedAt: raw.accepted_at,
  };
}

function normalizeBootstrapResponse(raw: RawOverlayBootstrapResponse): OverlayBootstrapResponse {
  return {
    suggestionId: raw.suggestion_id,
    artifactsRoot: raw.artifacts_root,
    snapshot: raw.snapshot,
    lastSequence: raw.last_sequence,
    liveResume: normalizeLiveResume(raw.live_resume),
  };
}

function buildOverlayBootstrapUrl(suggestionId: string): string {
  return `/api/agent/history/${encodeURIComponent(String(suggestionId))}/overlay-bootstrap`;
}

export function createOverlayBootstrapFetcher(params: {
  requestJson: ReturnType<typeof createLocalBackendClient>['requestJson'];
}): (suggestionId: string) => Promise<OverlayBootstrapResponse | null> {
  return async function fetchOverlayBootstrap(
    suggestionId: string
  ): Promise<OverlayBootstrapResponse | null> {
    const normalizedSuggestionId = String(suggestionId || '').trim();
    if (!normalizedSuggestionId) {
      return null;
    }
    return normalizeBootstrapResponse(
      await params.requestJson<RawOverlayBootstrapResponse>({
        path: buildOverlayBootstrapUrl(normalizedSuggestionId),
        method: 'GET',
      })
    );
  };
}
