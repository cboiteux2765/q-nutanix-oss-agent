import { useLayoutEffect, useSyncExternalStore, type RefObject } from 'react';

const overlayWidth = '(max-width: 1100px)';
const dialogSelector = 'dialog[open], [role="dialog"][aria-modal="true"]';
const focusableSelector = 'a[href], button, input, textarea, select, summary, [tabindex]';

function subscribeViewport(onChange: () => void) {
  const media = window.matchMedia(overlayWidth);
  media.addEventListener('change', onChange);
  return () => media.removeEventListener('change', onChange);
}

function narrowViewport() {
  return window.matchMedia(overlayWidth).matches;
}

function workPanel(workspace: HTMLDivElement | null) {
  return workspace?.querySelector<HTMLElement>(':scope > .activity-panel');
}

function hasOtherDialog(panel: HTMLElement) {
  return Array.from(document.querySelectorAll(dialogSelector)).some((dialog) => dialog !== panel);
}

function focusPanel(panel: HTMLElement) {
  const closeButton = panel.querySelector<HTMLElement>('.activity-heading button');
  (closeButton || panel).focus({ preventScroll: true });
}

export function useWorkPanelKeyboard(
  workspace: RefObject<HTMLDivElement | null>,
  open: boolean,
  loadedRunId: string | undefined,
  onClose: () => void,
) {
  const narrow = useSyncExternalStore(subscribeViewport, narrowViewport);
  const overlay = open && narrow;

  useLayoutEffect(() => {
    if (!overlay) return;
    const trigger = document.activeElement;

    const keydown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' && event.key !== 'Tab') return;
      const panel = workPanel(workspace.current);
      if (!panel || hasOtherDialog(panel)) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        onClose();
        return;
      }
      const stops = Array.from(panel.querySelectorAll<HTMLElement>(focusableSelector)).filter(
        (element) =>
          element.tabIndex >= 0 &&
          !element.matches(':disabled') &&
          element.getClientRects().length > 0 &&
          getComputedStyle(element).visibility === 'visible',
      );
      const first = stops[0];
      const last = stops.at(-1);
      const active = document.activeElement;
      if (
        !(active instanceof HTMLElement) ||
        !stops.includes(active) ||
        (event.shiftKey ? active === first : active === last)
      ) {
        event.preventDefault();
        const target = event.shiftKey ? last : first;
        if (target) target.focus({ preventScroll: true });
        else focusPanel(panel);
      }
    };
    const focusin = (event: FocusEvent) => {
      const panel = workPanel(workspace.current);
      if (
        panel &&
        event.target instanceof Node &&
        !panel.contains(event.target) &&
        !hasOtherDialog(panel)
      ) {
        focusPanel(panel);
      }
    };

    document.addEventListener('keydown', keydown, true);
    document.addEventListener('focusin', focusin);
    return () => {
      document.removeEventListener('keydown', keydown, true);
      document.removeEventListener('focusin', focusin);
      if (trigger instanceof HTMLElement && narrowViewport()) {
        // Wait until the panel and its dialogs are removed before restoring the trigger.
        requestAnimationFrame(() => {
          if (trigger.isConnected && !workPanel(workspace.current)) {
            trigger.focus({ preventScroll: true });
          }
        });
      }
    };
  }, [onClose, overlay, workspace]);

  useLayoutEffect(() => {
    if (!overlay) return;
    const panel = workPanel(workspace.current);
    if (!panel) return;
    const priorLabel = panel.getAttribute('aria-label');
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-modal', 'true');
    panel.setAttribute('aria-label', 'Work');
    if (!hasOtherDialog(panel)) focusPanel(panel);
    return () => {
      panel.removeAttribute('role');
      panel.removeAttribute('aria-modal');
      if (priorLabel === null) panel.removeAttribute('aria-label');
      else panel.setAttribute('aria-label', priorLabel);
    };
    // Initial run loading and the loaded inspector are distinct panel elements.
  }, [loadedRunId, overlay, workspace]);
}
