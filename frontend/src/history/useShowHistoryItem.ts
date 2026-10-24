import { useEffect } from 'react';
import { useNavigate } from 'react-router-dom';

import { historySelectionSearch, parseWorkKey } from './historySelection';

/**
 * Main's request to select a work in History, from any page: a task a panel started opens here.
 * It is an in-app navigation, so the chat and task drafts held above the pages stay as they are.
 */
export function useShowHistoryItem(): void {
  const navigate = useNavigate();
  useEffect(() => {
    const onShowItem = window.electron?.history?.onShowItem;
    if (!onShowItem) return;
    return onShowItem((payload) => {
      const request = typeof payload === 'object' && payload !== null ? payload : null;
      const item = request && 'item' in request ? parseWorkKey(request.item) : null;
      if (item === null) {
        console.error('Ignored a malformed request to show a History item.');
        return;
      }
      // A Suggestion file whose Overlay chip was pressed opens in the work's preview.
      const file =
        request && 'file' in request && typeof request.file === 'string' ? request.file : null;
      navigate({ pathname: '/history', search: historySelectionSearch(item, file) });
    });
  }, [navigate]);
}
