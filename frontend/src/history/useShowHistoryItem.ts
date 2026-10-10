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
      const item =
        typeof payload === 'object' && payload !== null && 'item' in payload
          ? parseWorkKey(payload.item)
          : null;
      if (item === null) {
        console.error('Ignored a malformed request to show a History item.');
        return;
      }
      navigate({ pathname: '/history', search: historySelectionSearch(item) });
    });
  }, [navigate]);
}
