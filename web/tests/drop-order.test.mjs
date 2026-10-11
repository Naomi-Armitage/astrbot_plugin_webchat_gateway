import test from "node:test";
import assert from "node:assert/strict";

import { compareDropNewestFirst, isOlderDropMessage } from "../src/chat_client/drop-order.ts";

test("backdated imports interleave with local Drop messages", () => {
  const localOld = { id: 1, created_at: 100 };
  const localNew = { id: 2, created_at: 300 };
  const imported = { id: 3, created_at: 200 };
  const importedSameSecond = { id: 4, created_at: 200 };
  assert.deepEqual(
    [localOld, localNew, imported, importedSameSecond]
      .sort(compareDropNewestFirst).map((item) => item.id),
    [2, 4, 3, 1],
  );
  assert.equal(isOlderDropMessage(imported, localNew), true);
  assert.equal(isOlderDropMessage(importedSameSecond, imported), false);
  assert.equal(isOlderDropMessage(imported, importedSameSecond), true);
});
