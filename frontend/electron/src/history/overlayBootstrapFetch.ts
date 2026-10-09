import type { OverlayBootstrapResponse } from '../orchestration/contracts';
import type { createLocalBackendClient } from '../localBackend/client';

type RawOverlayBootstrapResponse = {
  suggestion_id: string;
  snapshot: OverlayBootstrapResponse['snapshot'];
  last_sequence: number;
};

function normalizeBootstrapResponse(raw: RawOverlayBootstrapResponse): OverlayBootstrapResponse {
  return {
    suggestionId: raw.suggestion_id,
    snapshot: raw.snapshot,
    lastSequence: raw.last_sequence,
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
