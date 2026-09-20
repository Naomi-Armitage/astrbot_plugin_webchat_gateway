const IMAGE_MIME = new Set([
  "image/jpeg", "image/png", "image/webp", "image/gif",
  "image/jpg", "image/pjpeg", "image/x-png",
]);

// A filename is only a hint to request a preview. The server validates the
// bytes before serving any image, including old octet-stream uploads.
export function isDropImage(mime?: string, filename?: string): boolean {
  const normalized = (mime ?? "").split(";", 1)[0]!.trim().toLowerCase();
  return IMAGE_MIME.has(normalized) || /\.(?:jpe?g|jpe|jepg|png|webp|gif)$/i.test(filename?.trim() ?? "");
}
