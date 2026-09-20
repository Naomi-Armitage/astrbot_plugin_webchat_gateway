import { PictureWindow } from "./picture-window.ts";
export type PanelLayout = "full" | "float" | "dock";
export interface PanelGeometry { left: number; top: number; width: number; height: number }
interface Viewport { width: number; height: number }

const LAYOUT_KEY = "wcg.drop.layout";
const GEOMETRY_KEY = "wcg.drop.geometry";
const GAP = 12;

export function effectivePanelLayout(preferred: PanelLayout, width: number): PanelLayout {
  if (width < 720 || (preferred === "dock" && width < 1100)) return "full";
  return preferred;
}

export function fitPanelGeometry(value: PanelGeometry, viewport: Viewport): PanelGeometry {
  const availableWidth = Math.max(1, viewport.width - 2 * GAP);
  const availableHeight = Math.max(1, viewport.height - 2 * GAP);
  const width = Math.min(availableWidth, Math.max(320, value.width));
  const height = Math.min(availableHeight, Math.max(320, value.height));
  return {
    width, height,
    left: Math.max(Math.min(GAP, viewport.width - width), Math.min(value.left, viewport.width - width - GAP)),
    top: Math.max(Math.min(GAP, viewport.height - height), Math.min(value.top, viewport.height - height - GAP)),
  };
}

export function parsePanelGeometry(raw: string | null): PanelGeometry | null {
  try {
    const value = JSON.parse(raw ?? "null") as Partial<PanelGeometry> | null;
    if (!value || ![value.left, value.top, value.width, value.height]
      .every((n) => typeof n === "number" && Number.isFinite(n))) return null;
    if (value.width! <= 0 || value.height! <= 0) return null;
    return { left: value.left!, top: value.top!, width: value.width!, height: value.height! };
  } catch { return null; }
}

interface PanelElements {
  panel: HTMLElement;
  workspace: HTMLElement;
  main: HTMLElement;
  chatMessages: HTMLElement;
  chatComposer: HTMLElement;
  composer: HTMLElement;
  header: HTMLElement;
  resizeHandle: HTMLElement;
  layoutSwitch: HTMLElement;
  closeButton: HTMLButtonElement;
}
interface PanelOptions {
  onClose: () => void;
  onLayout: () => void;
  onDocument: (doc: Document) => void;
  onExternalChange: () => void;
  onNotice: (message: string) => void;
}
interface Gesture {
  id: number;
  target: HTMLElement;
  kind: "move" | "resize";
  x: number;
  y: number;
  start: PanelGeometry;
}

/** Owns presentation only. Message state, uploads and sending stay in the chat. */
export class ConversationPanel {
  private readonly elements: PanelElements;
  private readonly options: PanelOptions;
  private readonly events = new AbortController();
  private readonly panelMount = document.createComment("conversation panel");
  private readonly layoutMount = document.createComment("Drop layout controls");
  private preferred: PanelLayout = "full";
  private geometry: PanelGeometry;
  private layout: PanelLayout = "full";
  private opened = false;
  private gesture: Gesture | null = null;
  private readonly picture: PictureWindow;

  constructor(elements: PanelElements, options: PanelOptions) {
    this.elements = elements;
    this.options = options;
    this.picture = new PictureWindow(() => {
      options.onClose();
      options.onExternalChange();
    }, options.onDocument);
    elements.panel.before(this.panelMount);
    elements.layoutSwitch.before(this.layoutMount);
    const viewport = this.viewport();
    this.geometry = fitPanelGeometry({
      width: viewport.width * .34, height: viewport.height * .75,
      left: viewport.width * .64, top: viewport.height * .15,
    }, viewport);
    try {
      const saved = localStorage.getItem(LAYOUT_KEY);
      if (saved === "float" || saved === "dock") this.preferred = saved;
      this.geometry = parsePanelGeometry(localStorage.getItem(GEOMETRY_KEY)) ?? this.geometry;
    } catch { /* Storage may be unavailable in private browsing. */ }
    const eventOptions = { signal: this.events.signal };
    elements.layoutSwitch.addEventListener("click", (event) => {
      const button = (event.target as Element).closest<HTMLButtonElement>("button[data-layout]");
      if (!button || button.disabled) return;
      const layout = button.dataset.layout;
      if (layout === "full" || layout === "float" || layout === "dock") this.selectLayout(layout);
    }, eventOptions);
    elements.layoutSwitch.addEventListener("keydown", (event) => {
      const buttons = [...elements.layoutSwitch.querySelectorAll<HTMLButtonElement>("button:not(:disabled)")];
      const current = buttons.indexOf(elements.layoutSwitch.ownerDocument.activeElement as HTMLButtonElement);
      if (current < 0) return;
      let next: number;
      if (event.key === "ArrowRight" || event.key === "ArrowDown") next = (current + 1) % buttons.length;
      else if (event.key === "ArrowLeft" || event.key === "ArrowUp") next = (current - 1 + buttons.length) % buttons.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = buttons.length - 1;
      else return;
      event.preventDefault();
      buttons[next]!.click();
      buttons[next]!.focus();
    }, eventOptions);
    elements.closeButton.addEventListener("click", options.onClose, eventOptions);
    window.addEventListener("resize", () => {
      this.finishGesture(false);
      if (this.opened) this.render();
    }, eventOptions);
    for (const [handle, kind] of [[elements.header, "move"], [elements.resizeHandle, "resize"]] as const) {
      handle.addEventListener("pointerdown", (event) => this.startGesture(event, kind, handle), eventOptions);
      handle.addEventListener("pointermove", (event) => this.moveGesture(event), eventOptions);
      handle.addEventListener("pointerup", (event) => {
        if (event.pointerId === this.gesture?.id) this.finishGesture(false);
      }, eventOptions);
      handle.addEventListener("pointercancel", (event) => {
        if (event.pointerId === this.gesture?.id) this.finishGesture(true);
      }, eventOptions);
      handle.addEventListener("lostpointercapture", (event) => {
        if (event.target === handle && event.pointerId === this.gesture?.id) this.finishGesture(true);
      }, eventOptions);
    }
    const keyboardHandles: [HTMLElement, Gesture["kind"]][] = [[elements.header, "move"], [elements.resizeHandle, "resize"]];
    for (const [handle, kind] of keyboardHandles) {
      handle.addEventListener("keydown", (event) => {
        if (!this.opened || this.picture.window || this.layout !== "float" || event.target !== handle || !event.key.startsWith("Arrow")) return;
        event.preventDefault();
        const step = event.shiftKey ? 40 : 10;
        this.adjust(kind, event.key === "ArrowRight" ? step : event.key === "ArrowLeft" ? -step : 0,
          event.key === "ArrowDown" ? step : event.key === "ArrowUp" ? -step : 0, this.geometry);
        this.persist();
      }, eventOptions);
    }
    elements.panel.addEventListener("keydown", (event) => {
      if (event.key !== "Escape" || event.defaultPrevented || event.isComposing) return;
      // Other overlays (attachment menu, lightbox) own their first Escape.
      if (elements.panel.querySelector('[aria-expanded="true"]')) return;
      event.preventDefault();
      if (this.gesture) this.finishGesture(true);
      else options.onClose();
    }, eventOptions);
  }

  private selectLayout(layout: PanelLayout): void {
    this.finishGesture(false);
    this.preferred = layout;
    this.persist();
    if (layout !== "float") this.picture.close();
    this.render();
    if (this.layout === "float") void this.openPictureWindow();
    this.options.onExternalChange();
  }

  get isExclusive(): boolean { return this.opened && this.layout === "full"; }

  get externalVisible(): boolean { return !!this.picture.window && !this.picture.window.document.hidden; }

  open(): void {
    this.opened = true;
    this.render();
    if (this.layout === "float") void this.openPictureWindow();
  }

  private async openPictureWindow(): Promise<void> {
    const opened = await this.picture.open(this.elements.panel, this.geometry);
    if (!this.opened || this.preferred !== "float") return;
    if (opened) {
      this.render();
      this.options.onExternalChange();
      this.elements.composer.querySelector("textarea")?.focus();
    } else {
      this.options.onNotice("当前浏览器未能打开画中画窗口，已使用页内浮窗。");
    }
  }

  close(): void {
    this.finishGesture(false);
    this.opened = false;
    this.picture.close();
    const { panel, chatMessages, chatComposer, workspace, layoutSwitch, closeButton } = this.elements;
    this.panelMount.after(panel);
    chatComposer.hidden = false;
    this.layoutMount.after(layoutSwitch);
    this.setHeaderMovable(false);
    layoutSwitch.hidden = true;
    closeButton.hidden = true;
    panel.hidden = true;
    chatMessages.hidden = false;
    chatMessages.inert = false;
    workspace.removeAttribute("data-panel-layout");
  }

  destroy(): void {
    this.close();
    this.events.abort();
    this.panelMount.remove();
    this.layoutMount.remove();
  }

  private viewport(): Viewport { return { width: window.innerWidth, height: window.innerHeight }; }

  private persist(): void {
    try {
      localStorage.setItem(LAYOUT_KEY, this.preferred);
      localStorage.setItem(GEOMETRY_KEY, JSON.stringify(this.geometry));
    } catch { /* Layout still works without storage. */ }
  }

  private render(): void {
    if (!this.opened) return;
    const { panel, workspace, main, chatMessages, chatComposer, composer, header, layoutSwitch, closeButton, resizeHandle } = this.elements;
    const focused = panel.ownerDocument.activeElement as HTMLElement | null;
    const messages = panel.querySelector<HTMLElement>(".message-list");
    const scrollTop = messages?.scrollTop ?? 0;
    this.layout = this.picture.window ? "float" : effectivePanelLayout(this.preferred, window.innerWidth);
    const parent = this.picture.window?.document.body ?? (this.layout === "full" ? main : workspace);
    if (panel.parentElement !== parent) {
      if (this.layout === "full") this.panelMount.after(panel);
      else parent.append(panel);
    }
    if (composer.parentElement !== panel) panel.append(composer);
    header.hidden = this.layout === "full";
    if (this.layout === "full") this.layoutMount.after(layoutSwitch);
    else header.querySelector(".actions")!.prepend(layoutSwitch);
    panel.hidden = false;
    panel.dataset.layout = this.layout;
    workspace.dataset.panelLayout = this.layout;
    panel.setAttribute("role", this.layout === "float" ? "dialog" : "region");
    layoutSwitch.hidden = !this.picture.window && window.innerWidth < 720;
    closeButton.hidden = this.layout === "full";
    for (const button of layoutSwitch.querySelectorAll<HTMLButtonElement>("button[data-layout]")) {
      const mode = button.dataset.layout as PanelLayout;
      const selected = mode === this.layout;
      button.setAttribute("aria-checked", String(selected));
      button.tabIndex = selected ? 0 : -1;
      button.disabled = mode !== "float" && effectivePanelLayout(mode, window.innerWidth) !== mode;
      if (mode === "float") button.title = window.documentPictureInPicture
        ? "浮窗：悬浮在其他窗口上方" : "浮窗：当前浏览器仅支持页内悬浮";
      if (mode === "dock") button.title = button.disabled ? "右侧栏：需要更宽的窗口" : "右侧栏";
    }
    chatMessages.hidden = this.layout === "full";
    chatMessages.inert = false;
    chatComposer.hidden = this.layout === "full";
    const inPageFloat = this.layout === "float" && !this.picture.window;
    this.setHeaderMovable(inPageFloat);
    resizeHandle.hidden = !inPageFloat;
    if (inPageFloat) this.paintGeometry();
    else for (const name of ["left", "top", "width", "height"]) panel.style.removeProperty(name);
    if (focused && (panel.contains(focused) || header.contains(focused))) {
      if (layoutSwitch.contains(focused) && layoutSwitch.hidden) composer.querySelector("textarea")?.focus({ preventScroll: true });
      else if (focused !== panel.ownerDocument.activeElement) focused.focus({ preventScroll: true });
    }
    this.options.onLayout();
    if (messages) messages.scrollTop = scrollTop;
  }

  private paintGeometry(): void {
    this.geometry = fitPanelGeometry(this.geometry, this.viewport());
    const { panel } = this.elements;
    for (const [name, value] of Object.entries(this.geometry)) panel.style.setProperty(name, `${value}px`);
  }

  private setHeaderMovable(movable: boolean): void {
    const { header } = this.elements;
    header.toggleAttribute("data-panel-movable", movable);
    if (movable) {
      header.tabIndex = 0;
      header.setAttribute("aria-label", "对话工具栏，可拖动或使用方向键移动浮窗");
    } else {
      header.removeAttribute("tabindex");
      header.removeAttribute("aria-label");
    }
  }

  private startGesture(event: PointerEvent, kind: Gesture["kind"], target: HTMLElement): void {
    if (!this.opened || this.picture.window || this.layout !== "float" || !event.isPrimary || event.button !== 0) return;
    const interactive = (event.target as Element).closest("button, select, a, input");
    if (kind === "move" && interactive) return;
    event.preventDefault();
    this.gesture = { id: event.pointerId, target, kind, x: event.clientX, y: event.clientY, start: { ...this.geometry } };
    target.setPointerCapture(event.pointerId);
    this.elements.panel.classList.add("is-adjusting");
  }

  private moveGesture(event: PointerEvent): void {
    const gesture = this.gesture;
    if (!gesture || event.pointerId !== gesture.id) return;
    this.adjust(gesture.kind, event.clientX - gesture.x, event.clientY - gesture.y, gesture.start);
  }

  private adjust(kind: Gesture["kind"], dx: number, dy: number, start: PanelGeometry): void {
    this.geometry = kind === "move" ? { ...start, left: start.left + dx, top: start.top + dy }
      : { ...start, width: Math.min(start.width + dx, window.innerWidth - start.left - GAP),
        height: Math.min(start.height + dy, window.innerHeight - start.top - GAP) };
    this.paintGeometry();
  }

  private finishGesture(cancel: boolean): void {
    const gesture = this.gesture;
    if (!gesture) return;
    this.gesture = null;
    if (cancel) { this.geometry = gesture.start; this.paintGeometry(); }
    this.elements.panel.classList.remove("is-adjusting");
    if (gesture.target.hasPointerCapture(gesture.id)) gesture.target.releasePointerCapture(gesture.id);
    this.persist();
    this.options.onLayout();
  }
}
