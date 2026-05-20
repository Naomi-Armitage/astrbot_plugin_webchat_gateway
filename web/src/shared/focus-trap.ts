// Shared modal / overlay focus trap.
//
// Modeled on the admin_panel implementation (examples/admin_panel/index.html
// ~line 1607-1621) — extracted here so chat_client's mobile drawer and image
// lightbox can stop letting Tab escape into the background page (the v0.2.1
// known limitation called out in CHANGELOG). The admin_panel keeps its
// inline copy for now since admin_panel isn't on the Vite pipeline; if it
// joins later, prefer this util.
//
// Contract:
//   * Trap is installed against a container element. All focusable
//     descendants of the container participate in the Tab cycle.
//   * Tab from the last focusable wraps to the first; Shift+Tab from the
//     first wraps to the last; focus that drifts outside the container
//     (e.g. via screen reader gestures) is yanked back to the first
//     focusable on the next Tab.
//   * `onEscape` is optional. If provided, Escape preventDefault's and
//     fires the callback. Keep it simple — the caller owns the close
//     decision.
//   * `initialFocus` defaults to the first focusable inside the container.
//     Pass an explicit element to override (e.g. for a primary CTA).
//   * `release()` restores focus to the element that held it at install
//     time (best-effort).
//
// Stacking: the keydown listener is attached in the capture phase so the
// outermost trap wins. Callers MUST `release()` in the inverse order they
// installed, otherwise an inner trap can outlive its container and
// continue to swallow Tab. This matches how the lightbox + drawer cases
// nest naturally (drawer can open lightbox, but not vice versa).

export interface FocusTrap {
  release(): void;
}

const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

function getFocusable(container: HTMLElement): HTMLElement[] {
  return Array.from(
    container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
  ).filter((el) => !el.hasAttribute("hidden") && el.offsetParent !== null);
}

export function installFocusTrap(
  container: HTMLElement,
  opts: { onEscape?: () => void; initialFocus?: HTMLElement | null } = {},
): FocusTrap {
  const prevFocus = document.activeElement as HTMLElement | null;

  const onKeyDown = (e: KeyboardEvent): void => {
    if (e.key === "Escape" && opts.onEscape) {
      e.preventDefault();
      opts.onEscape();
      return;
    }
    if (e.key !== "Tab") return;
    const focusables = getFocusable(container);
    if (focusables.length === 0) {
      // Nothing focusable — pin focus on the container itself so Tab
      // can't escape. Container must have tabindex="-1" or "0" for
      // focus() to take; if not, this is a soft no-op.
      e.preventDefault();
      try {
        container.focus();
      } catch {
        /* noop */
      }
      return;
    }
    const first = focusables[0]!;
    const last = focusables[focusables.length - 1]!;
    const active = document.activeElement as HTMLElement | null;
    if (active && !container.contains(active)) {
      // Focus drifted outside (e.g. browser ate it after a node was
      // removed) — yank it back to the first focusable.
      e.preventDefault();
      first.focus();
      return;
    }
    if (e.shiftKey && active === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && active === last) {
      e.preventDefault();
      first.focus();
    }
  };

  document.addEventListener("keydown", onKeyDown, true);

  // Defer initial focus so callers that install the trap synchronously
  // after appending the container don't fight the browser's own focus
  // logic on the just-mounted node.
  setTimeout(() => {
    const target =
      opts.initialFocus ?? getFocusable(container)[0] ?? null;
    if (target) {
      try {
        target.focus();
      } catch {
        /* noop */
      }
    }
  }, 0);

  return {
    release(): void {
      document.removeEventListener("keydown", onKeyDown, true);
      if (prevFocus && typeof prevFocus.focus === "function") {
        try {
          prevFocus.focus();
        } catch {
          /* noop — element may have been removed from the DOM */
        }
      }
    },
  };
}
