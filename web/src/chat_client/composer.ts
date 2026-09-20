export type ComposerTarget = "chat" | "drop";

/** Both conversations instantiate the same markup, styles and event bindings. */
export interface ComposerView {
  root: HTMLElement;
  inputEl: HTMLTextAreaElement;
  sendBtn: HTMLButtonElement;
  fileInputEl: HTMLInputElement;
  plusBtn: HTMLButtonElement;
  plusMenu: HTMLDivElement;
  menuUpload: HTMLButtonElement;
  menuImage: HTMLButtonElement;
  imgRatioBar: HTMLDivElement;
  composerAttachmentsEl: HTMLElement;
  dropOverlayEl: HTMLElement;
}

export function createComposer(source: HTMLElement, target: ComposerTarget): ComposerView {
  const root = target === "chat" ? source : source.cloneNode(true) as HTMLElement;
  const find = <T extends HTMLElement>(id: string): T => root.querySelector<T>(`[id="${id}"]`)!;
  const view: ComposerView = {
    root, inputEl: find("input"), sendBtn: find("send"), fileInputEl: find("fileInput"),
    plusBtn: find("plusBtn"), plusMenu: find("plusMenu"), menuUpload: find("menuUpload"),
    menuImage: find("menuImage"), imgRatioBar: find("imgRatioBar"),
    composerAttachmentsEl: find("composer-attachments"), dropOverlayEl: find("dropOverlay"),
  };
  root.dataset.composer = target;
  view.sendBtn.classList.add("composer-send");
  view.composerAttachmentsEl.classList.add("composer-attachments");
  if (target === "drop") {
    root.id = "dropComposer";
    for (const element of root.querySelectorAll<HTMLElement>("[id]")) element.id = `drop-${element.id}`;
    for (const element of root.querySelectorAll<HTMLElement>("[aria-controls]")) {
      element.setAttribute("aria-controls", `drop-${element.getAttribute("aria-controls")}`);
    }
    view.inputEl.setAttribute("aria-label", "Drop 消息");
    view.fileInputEl.accept = "";
    view.menuImage.hidden = true;
    view.menuUpload.querySelector("span")!.textContent = "上传文件";
    view.dropOverlayEl.querySelector(".drop-overlay-inner")!.textContent = "松开以添加文件";
  }
  return view;
}

export function autosizeComposer(view: ComposerView): void {
  view.inputEl.style.height = "auto";
  view.inputEl.style.height = `${view.inputEl.scrollHeight}px`;
}
