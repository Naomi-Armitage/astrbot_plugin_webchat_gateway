/** Parse Edge Drop's plain-text export in the importing browser's timezone. */
export interface EdgeDropImportMessage {
  text: string;
  created_at: number;
}

export const MAX_EDGE_DROP_IMPORT_MESSAGES = 1000;
export const MAX_EDGE_DROP_TEXT_CHARS = 16_000;

const ENTRY_RE = /^\[(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})\](?:[ \t](.*))?$/;

export function looksLikeEdgeDropExport(source: string): boolean {
  const first = source.replace(/^\uFEFF/, "").split(/\r?\n/).find((line) => line.trim());
  return first !== undefined && ENTRY_RE.test(first);
}

export function parseEdgeDropExport(source: string): EdgeDropImportMessage[] {
  const lines = source.replace(/^\uFEFF/, "").split(/\r?\n/);
  const messages: EdgeDropImportMessage[] = [];
  let current: { created_at: number; body: string[] } | null = null;

  const finish = (): void => {
    if (!current) return;
    const text = current.body.join("\n").trimEnd();
    if (!text.trim()) throw new Error("存在空的 Drop 消息");
    if (text.length > MAX_EDGE_DROP_TEXT_CHARS) throw new Error("单条 Drop 消息超过 16,000 字符");
    messages.push({ text, created_at: current.created_at });
    if (messages.length > MAX_EDGE_DROP_IMPORT_MESSAGES) throw new Error("一次最多导入 1,000 条 Drop 消息");
  };

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index]!;
    const match = ENTRY_RE.exec(line);
    if (match) {
      finish();
      const [year, month, day, hour, minute, second] = match.slice(1, 7).map(Number) as
        [number, number, number, number, number, number];
      const date = new Date(year, month - 1, day, hour, minute, second);
      if (Number.isNaN(date.getTime()) || date.getFullYear() !== year || date.getMonth() !== month - 1
        || date.getDate() !== day || date.getHours() !== hour || date.getMinutes() !== minute
        || date.getSeconds() !== second) {
        throw new Error(`第 ${index + 1} 行时间无效`);
      }
      current = { created_at: Math.floor(date.getTime() / 1000), body: [match[7] ?? ""] };
    } else if (current) {
      current.body.push(line);
    } else if (line.trim()) {
      throw new Error(`第 ${index + 1} 行不是 Edge Drop 时间戳记录`);
    }
  }
  finish();
  if (!messages.length) throw new Error("文件中没有可导入的 Drop 消息");
  return messages;
}
