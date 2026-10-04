import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { Translate } from '../types';
import { useWorkspaceProjectDragController } from '../useWorkspaceProjectDragController';
import { SETTINGS_MESSAGES } from '@/i18n/messageCatalog/settings';
import { WorkspaceProjectList } from './WorkspaceProjectList';
import type { WorkspaceProject } from './workspaceSettingsModel';

const translate = ((key: string, vars?: { name?: string }) =>
  vars?.name ? `${key}:${vars.name}` : key) as Translate;

const initialProjects: WorkspaceProject[] = [
  { project_id: 'project-a', display_name: 'Alpha', sort_order: 0, organization_ids: [] },
  { project_id: 'project-b', display_name: 'Beta', sort_order: 1, organization_ids: [] },
];

function DndHarness({
  authoritativeProjects,
  disabled = false,
  onPersist,
}: {
  authoritativeProjects?: WorkspaceProject[];
  disabled?: boolean;
  onPersist: (projectIds: string[]) => Promise<void>;
}) {
  const [previewProjects, setPreviewProjects] = useState(initialProjects);
  const [failed, setFailed] = useState(false);
  const projects = authoritativeProjects ?? previewProjects;
  const dragController = useWorkspaceProjectDragController({
    disabled,
    generation: authoritativeProjects ? 1 : 0,
    projects,
    t: translate,
    onPreview: setPreviewProjects,
    onPersist,
    onFailure: () => setFailed(true),
  });
  return (
    <>
      {failed ? <div role="alert">save failed</div> : null}
      <WorkspaceProjectList
        disabled={disabled}
        dragController={dragController}
        folders={[]}
        projects={projects}
        selectedProjectId={null}
        t={translate}
        onSelect={() => {}}
      />
    </>
  );
}

describe('WorkspaceProjectList drag and drop', () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('always renders drag handles and disables them only while saving', () => {
    const onPersist = vi.fn(async () => {});
    const { rerender } = render(<DndHarness onPersist={onPersist} />);

    expect(
      screen.getAllByRole('button', { name: /settings\.workspace\.drag\.handle/u })
    ).toHaveLength(2);
    expect(
      screen.getByRole('button', { name: 'settings.workspace.drag.handle:Alpha' })
    ).toBeEnabled();

    rerender(<DndHarness disabled onPersist={onPersist} />);

    expect(
      screen.getAllByRole('button', { name: /settings\.workspace\.drag\.handle/u })
    ).toHaveLength(2);
    expect(
      screen.getByRole('button', { name: 'settings.workspace.drag.handle:Alpha' })
    ).toBeDisabled();
  });

  it('persists keyboard reordering through the accessible drag handle', async () => {
    const onPersist = vi.fn(async () => {});
    const { container } = render(<DndHarness onPersist={onPersist} />);
    const handle = screen.getByRole('button', {
      name: 'settings.workspace.drag.handle:Alpha',
    });
    const cards = Array.from(container.querySelectorAll<HTMLElement>('[data-project-id]'));
    vi.spyOn(cards[0], 'getBoundingClientRect').mockReturnValue(rectAt(0));
    vi.spyOn(cards[1], 'getBoundingClientRect').mockReturnValue(rectAt(80));
    handle.focus();

    fireEvent.keyDown(handle, { key: ' ', code: 'Space' });
    await waitFor(() => expect(handle).toHaveAttribute('aria-pressed', 'true'));
    fireEvent.keyDown(document, { key: 'ArrowDown', code: 'ArrowDown' });
    await waitFor(() => {
      expect(
        Array.from(container.querySelectorAll<HTMLElement>('[data-project-id]')).map(
          (card) => card.dataset.projectId
        )
      ).toEqual(['project-b', 'project-a']);
    });
    fireEvent.keyDown(document, { key: ' ', code: 'Space' });

    await waitFor(() => {
      expect(onPersist).toHaveBeenCalledWith(['project-b', 'project-a']);
    });
    expect(
      Array.from(container.querySelectorAll<HTMLElement>('[data-project-id]')).map(
        (card) => card.dataset.projectId
      )
    ).toEqual(['project-b', 'project-a']);
  });

  it('persists pointer reordering after the activation distance is crossed', async () => {
    const onPersist = vi.fn(async () => {});
    const { container } = render(<DndHarness onPersist={onPersist} />);
    const handle = screen.getByRole('button', {
      name: 'settings.workspace.drag.handle:Alpha',
    });
    const cards = Array.from(container.querySelectorAll<HTMLElement>('[data-project-id]'));
    vi.spyOn(cards[0], 'getBoundingClientRect').mockReturnValue(rectAt(0));
    vi.spyOn(cards[1], 'getBoundingClientRect').mockReturnValue(rectAt(80));

    fireEvent.pointerDown(handle, {
      button: 0,
      clientX: 20,
      clientY: 20,
      isPrimary: true,
      pointerId: 1,
    });
    fireEvent.pointerMove(document, {
      clientX: 20,
      clientY: 110,
      isPrimary: true,
      pointerId: 1,
    });
    await waitFor(() => expect(handle).toHaveAttribute('aria-pressed', 'true'));
    fireEvent.pointerMove(document, {
      clientX: 20,
      clientY: 130,
      isPrimary: true,
      pointerId: 1,
    });
    await waitFor(() => {
      expect(
        Array.from(container.querySelectorAll<HTMLElement>('[data-project-id]')).map(
          (card) => card.dataset.projectId
        )
      ).toEqual(['project-b', 'project-a']);
    });
    fireEvent.pointerUp(document, {
      clientX: 20,
      clientY: 130,
      isPrimary: true,
      pointerId: 1,
    });

    await waitFor(() => {
      expect(onPersist).toHaveBeenCalledWith(['project-b', 'project-a']);
    });
  });

  it('restores the snapshot when a keyboard drag is cancelled', async () => {
    const onPersist = vi.fn(async () => {});
    const { container } = render(<DndHarness onPersist={onPersist} />);
    const handle = screen.getByRole('button', {
      name: 'settings.workspace.drag.handle:Alpha',
    });
    const cards = Array.from(container.querySelectorAll<HTMLElement>('[data-project-id]'));
    vi.spyOn(cards[0], 'getBoundingClientRect').mockReturnValue(rectAt(0));
    vi.spyOn(cards[1], 'getBoundingClientRect').mockReturnValue(rectAt(80));

    fireEvent.keyDown(handle, { key: ' ', code: 'Space' });
    await waitFor(() => expect(handle).toHaveAttribute('aria-pressed', 'true'));
    fireEvent.keyDown(document, { key: 'ArrowDown', code: 'ArrowDown' });
    await waitFor(() => {
      expect(
        Array.from(container.querySelectorAll<HTMLElement>('[data-project-id]')).map(
          (card) => card.dataset.projectId
        )
      ).toEqual(['project-b', 'project-a']);
    });
    fireEvent.keyDown(document, { key: 'Escape', code: 'Escape' });

    await waitFor(() => {
      expect(
        Array.from(container.querySelectorAll<HTMLElement>('[data-project-id]')).map(
          (card) => card.dataset.projectId
        )
      ).toEqual(['project-a', 'project-b']);
    });
    expect(onPersist).not.toHaveBeenCalled();
  });

  it('discards a drag when authoritative projects change mid-drag', async () => {
    const onPersist = vi.fn(async () => {});
    const { container, rerender } = render(<DndHarness onPersist={onPersist} />);
    const handle = screen.getByRole('button', {
      name: 'settings.workspace.drag.handle:Alpha',
    });
    const cards = Array.from(container.querySelectorAll<HTMLElement>('[data-project-id]'));
    vi.spyOn(cards[0], 'getBoundingClientRect').mockReturnValue(rectAt(0));
    vi.spyOn(cards[1], 'getBoundingClientRect').mockReturnValue(rectAt(80));

    fireEvent.keyDown(handle, { key: ' ', code: 'Space' });
    await waitFor(() => expect(handle).toHaveAttribute('aria-pressed', 'true'));
    fireEvent.keyDown(document, { key: 'ArrowDown', code: 'ArrowDown' });
    await waitFor(() => {
      expect(
        Array.from(container.querySelectorAll<HTMLElement>('[data-project-id]')).map(
          (card) => card.dataset.projectId
        )
      ).toEqual(['project-b', 'project-a']);
    });

    rerender(
      <DndHarness
        authoritativeProjects={[
          initialProjects[0],
          { ...initialProjects[1], display_name: 'Beta refreshed' },
          {
            project_id: 'project-c',
            display_name: 'Gamma',
            sort_order: 2,
            organization_ids: [],
          },
        ]}
        onPersist={onPersist}
      />
    );
    await waitFor(() => expect(screen.getByText('Gamma')).toBeInTheDocument());
    fireEvent.keyDown(document, { key: ' ', code: 'Space' });

    await waitFor(() => {
      expect(
        Array.from(container.querySelectorAll<HTMLElement>('[data-project-id]')).map(
          (card) => card.dataset.projectId
        )
      ).toEqual(['project-a', 'project-b', 'project-c']);
    });
    expect(screen.getByText('Beta refreshed')).toBeInTheDocument();
    expect(onPersist).not.toHaveBeenCalled();
  });

  it('rolls back the preview and reports a failed reorder request', async () => {
    const onPersist = vi.fn(async () => {
      throw new Error('save failed');
    });
    const { container } = render(<DndHarness onPersist={onPersist} />);
    const handle = screen.getByRole('button', {
      name: 'settings.workspace.drag.handle:Alpha',
    });
    const cards = Array.from(container.querySelectorAll<HTMLElement>('[data-project-id]'));
    vi.spyOn(cards[0], 'getBoundingClientRect').mockReturnValue(rectAt(0));
    vi.spyOn(cards[1], 'getBoundingClientRect').mockReturnValue(rectAt(80));

    fireEvent.keyDown(handle, { key: ' ', code: 'Space' });
    await waitFor(() => expect(handle).toHaveAttribute('aria-pressed', 'true'));
    fireEvent.keyDown(document, { key: 'ArrowDown', code: 'ArrowDown' });
    await waitFor(() => {
      expect(
        Array.from(container.querySelectorAll<HTMLElement>('[data-project-id]')).map(
          (card) => card.dataset.projectId
        )
      ).toEqual(['project-b', 'project-a']);
    });
    fireEvent.keyDown(document, { key: ' ', code: 'Space' });

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('save failed'));
    expect(
      Array.from(container.querySelectorAll<HTMLElement>('[data-project-id]')).map(
        (card) => card.dataset.projectId
      )
    ).toEqual(['project-a', 'project-b']);
  });

  it('keeps the new drag active when an older generation save fails', async () => {
    let reject!: (error: Error) => void;
    const oldSave = new Promise<void>((_resolve, rejectSave) => {
      reject = rejectSave;
    });
    const onPersist = vi.fn().mockReturnValueOnce(oldSave).mockResolvedValue(undefined);
    const { container, rerender } = render(<DndHarness onPersist={onPersist} />);
    const start = async () => {
      const handle = screen.getByRole('button', { name: 'settings.workspace.drag.handle:Alpha' });
      const cards = Array.from(container.querySelectorAll<HTMLElement>('[data-project-id]'));
      cards.forEach((card, index) =>
        vi.spyOn(card, 'getBoundingClientRect').mockReturnValue(rectAt(index * 80))
      );
      handle.focus();
      fireEvent.keyDown(handle, { key: ' ', code: 'Space' });
      await waitFor(() => expect(handle).toHaveAttribute('aria-pressed', 'true'));
    };
    await start();
    fireEvent.keyDown(document, { key: 'ArrowDown', code: 'ArrowDown' });
    await waitFor(() =>
      expect(container.querySelector('[data-project-id]')).toHaveAttribute(
        'data-project-id',
        'project-b'
      )
    );
    fireEvent.keyDown(document, { key: ' ', code: 'Space' });
    await waitFor(() => expect(onPersist).toHaveBeenCalledTimes(1));

    rerender(<DndHarness authoritativeProjects={initialProjects} onPersist={onPersist} />);
    await start();
    await act(async () => reject(new Error('old owner failed')));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    fireEvent.keyDown(document, { key: 'ArrowDown', code: 'ArrowDown' });
    fireEvent.keyDown(document, { key: ' ', code: 'Space' });
    await waitFor(() => expect(onPersist).toHaveBeenCalledTimes(2));
    expect(onPersist.mock.calls[1][0]).toEqual(['project-b', 'project-a']);
  });

  it('provides English and Japanese screen-reader instructions and announcements', () => {
    expect(SETTINGS_MESSAGES.en['settings.workspace.drag.instructions']).toContain('Press Space');
    expect(SETTINGS_MESSAGES.ja['settings.workspace.drag.instructions']).toContain('スペースキー');
    expect(SETTINGS_MESSAGES.en['settings.workspace.drag.start']).toContain('{name}');
    expect(SETTINGS_MESSAGES.ja['settings.workspace.drag.start']).toContain('{name}');
  });
});

function rectAt(top: number): DOMRect {
  return {
    bottom: top + 60,
    height: 60,
    left: 0,
    right: 400,
    top,
    width: 400,
    x: 0,
    y: top,
    toJSON: () => ({}),
  };
}
