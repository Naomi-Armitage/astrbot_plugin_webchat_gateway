interface DocumentPictureInPicture {
  requestWindow(options: { width: number; height: number }): Promise<Window>;
}

declare global {
  interface Window { documentPictureInPicture?: DocumentPictureInPicture }
}

/** Adopts live UI into a native always-on-top window and returns it on close. */
export class PictureWindow {
  private current: Window | null = null;
  private generation = 0;
  private pending: Promise<boolean> | null = null;
  private returnContent: (() => void) | null = null;

  private readonly onClose: () => void;
  private readonly onDocument: (doc: Document) => void;

  constructor(onClose: () => void, onDocument: (doc: Document) => void) {
    this.onClose = onClose;
    this.onDocument = onDocument;
  }

  get window(): Window | null { return this.current; }

  open(panel: HTMLElement, size: { width: number; height: number }): Promise<boolean> {
    if (this.current && !this.current.closed) return Promise.resolve(true);
    if (this.pending) return this.pending;
    const api = window.documentPictureInPicture;
    if (!api) return Promise.resolve(false);
    const generation = ++this.generation;
    const request = async (): Promise<boolean> => {
      try {
        // requestWindow must be invoked directly from the user's click.
        const pip = await api.requestWindow(size);
        if (generation !== this.generation) { pip.close(); return false; }
        this.current = pip;
        const mount = document.createComment("picture-in-picture return");
        panel.before(mount);
        const doc = pip.document;
        doc.title = "Drop";
        const base = doc.createElement("base");
        base.href = document.baseURI;
        doc.head.append(base);
        for (const style of document.querySelectorAll('style, link[rel="stylesheet"]')) doc.head.append(style.cloneNode(true));
        doc.documentElement.lang = document.documentElement.lang;
        doc.documentElement.dataset.pip = "true";
        doc.body.classList.add("pip-window");
        const syncTheme = (): void => {
          const theme = document.documentElement.getAttribute("data-theme");
          if (theme) doc.documentElement.setAttribute("data-theme", theme);
          else doc.documentElement.removeAttribute("data-theme");
        };
        syncTheme();
        const observer = new MutationObserver(syncTheme);
        observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
        this.returnContent = () => {
          observer.disconnect();
          mount.after(panel);
          mount.remove();
        };
        pip.addEventListener("pagehide", () => {
          if (this.current !== pip) return;
          this.current = null;
          this.returnContent?.();
          this.returnContent = null;
          this.onClose();
        }, { once: true });
        this.onDocument(doc);
        return true;
      } catch {
        if (generation === this.generation) this.close();
        return false;
      }
    };
    const pending = request();
    this.pending = pending;
    void pending.finally(() => { if (this.pending === pending) this.pending = null; });
    return pending;
  }

  close(): void {
    ++this.generation;
    this.pending = null;
    const pip = this.current;
    this.current = null;
    this.returnContent?.();
    this.returnContent = null;
    pip?.close();
  }
}
