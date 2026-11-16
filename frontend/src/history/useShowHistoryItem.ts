import { useEffect } from 'react';
import { useNavigate } from 'react-router-dom';

import { DEFAULT_TODO_SELECTION, todoSelectionSearch } from '@/todos/todoSelection';

import { historySelectionSearch, parseWorkKey } from './historySelection';

/** A TODO main names: a panel added it, and the main window opens on its conversation. */
const TODO_KEY = /^todo:(.+)$/;

/**
 * Main's request to select a work in History, or a TODO in the list, from any page: a task a
 * panel started opens here. It is an in-app navigation, so the chat and task drafts held above
 * the pages stay as they are.
 */
export function useShowHistoryItem(): void {
  const navigate = useNavigate();
  useEffect(() => {
    const onShowItem = window.electron?.history?.onShowItem;
    if (!onShowItem) return;
    return onShowItem((payload) => {
      const request = typeof payload === 'object' && payload !== null ? payload : null;
      const value = request && 'item' in request ? request.item : null;
      const todoId = typeof value === 'string' ? TODO_KEY.exec(value)?.[1] : undefined;
      // Ids are canonical: never blank, never padded.
      if (todoId !== undefined && todoId === todoId.trim()) {
        navigate({
          pathname: '/history',
          search: todoSelectionSearch({ ...DEFAULT_TODO_SELECTION, todoId }),
        });
        return;
      }
      const item = parseWorkKey(value);
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
