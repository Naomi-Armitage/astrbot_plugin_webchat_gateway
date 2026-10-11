import assert from "node:assert/strict";
import test from "node:test";
import { looksLikeEdgeDropExport, parseEdgeDropExport } from "../src/chat_client/edge-drop-import.ts";

test("parses timestamped Edge Drop messages and multiline bodies", () => {
  const source = "\uFEFF[2025-07-07 06:19:24] 你好\r\n[2025-07-08 13:04:05] 第一行\r\n第二行\r\n";
  assert.equal(looksLikeEdgeDropExport(source), true);
  assert.deepEqual(parseEdgeDropExport(source), [
    { text: "你好", created_at: Math.floor(new Date(2025, 6, 7, 6, 19, 24).getTime() / 1000) },
    { text: "第一行\n第二行", created_at: Math.floor(new Date(2025, 6, 8, 13, 4, 5).getTime() / 1000) },
  ]);
});

test("rejects invalid timestamps, junk lines and empty messages", () => {
  assert.equal(looksLikeEdgeDropExport('{"format":"webchat-gateway-conversations"}'), false);
  assert.throws(() => parseEdgeDropExport("[2025-02-30 06:19:24] invalid"), /时间无效/);
  assert.throws(() => parseEdgeDropExport("not a record\n[2025-07-07 06:19:24] 你好"), /不是 Edge Drop/);
  assert.throws(() => parseEdgeDropExport("[2025-07-07 06:19:24]"), /空的 Drop 消息/);
  assert.throws(() => parseEdgeDropExport("[2025-07-07 06:19:24] " + "x".repeat(16_001)), /超过/);
  assert.throws(() => parseEdgeDropExport("[2025-07-07 06:19:24] x\n".repeat(1001)), /1,000 条/);
});
