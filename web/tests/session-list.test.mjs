import assert from "node:assert/strict";
import test from "node:test";

import { mergePendingLocalSessions } from "../src/chat_client/session-list.ts";

test("keeps a new local session while its optimistic message awaits the server", () => {
  const local = { id: "new-session", history: [{ role: "user", text: "/image cat" }] };
  const merged = mergePendingLocalSessions(
    { existing: { id: "existing", history: [] } },
    { existing: { id: "existing", history: [] }, "new-session": local },
    new Set(["new-session"]),
    new Set(),
  );

  assert.equal(merged["new-session"], local);
  assert.ok(merged.existing);
});

test("drops stale local sessions and never revives a queued deletion", () => {
  const current = {
    stale: { id: "stale" },
    deleting: { id: "deleting" },
  };
  const merged = mergePendingLocalSessions(
    { listed: { id: "listed" } },
    current,
    new Set(["stale", "deleting"]),
    new Set(["deleting"]),
  );

  assert.deepEqual(Object.keys(merged), ["listed", "stale"]);
});

test("keeps the server version when a pending session is already listed", () => {
  const local = { id: "same", title: "local" };
  const server = { id: "same", title: "server" };
  const merged = mergePendingLocalSessions(
    { same: server },
    { same: local },
    new Set(["same"]),
    new Set(),
  );

  assert.equal(merged.same, server);
});
