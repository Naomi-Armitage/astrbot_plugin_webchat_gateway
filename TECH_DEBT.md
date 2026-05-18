# Tech Debt

Known limitations of the current implementation that we deliberately
shipped rather than fix in-line. Each item links the symptom to a
proposed long-term fix. None of these block normal operation; they
are the kind of edge case that an attentive operator would notice
under specific failure modes.

---

## 1. Attachment persistence is bolted onto two unrelated layers

**Symptom**: in a few corner cases, attachment files end up either
orphaned (committed=1 in `webchat_files` but no message referencing
them) or referenced by something that's been pruned (CM
`ImageURLPart` pointing at a deleted file_id).

The two cases we currently accept:

- **Stream-started-then-failed**: `emit_stream_started` already
  emitted `message_added(user)` with the file_ids to peer devices.
  Then the LLM stream fails with empty_reply / llm_timeout zero-
  chunks. If we released the files in `close_failed`, every peer's
  `<img>` would 404 and show a broken bubble. So we keep the files
  committed=1 with no CM record. Cleanup paths: user-initiated
  `clear_history` or the 90-day session cascade.
- **Non-stream `/chat` after `record_chat_pair` failure**: LLM
  succeeded, attachment was committed, then the CM persist for the
  pair raised an exception. CM has no `ImageURLPart`, but the file
  rows are committed=1. Currently `record_chat_pair` is documented
  to "never raise" — but if it ever does, we leak the file.

**Root cause**: attachment lifecycle is currently tracked across
three independent layers:

1. `webchat_files` table (committed flag, owns the storage_key)
2. `webchat_updates` events (`message_added.attachments[]`, 14-day
   retention)
3. AstrBot CM `ImageURLPart` segments (long-term, retention is CM's)

There's no single source of truth for "this file_id is referenced by
that turn". The CM ImageURLPart is the closest thing to long-term
reference but it's piggy-backed on CM's data model and we can't add
a "user-only turn" without polluting CM's user/assistant pair
semantic.

**Proposed long-term fix**: a dedicated `webchat_message_attachments`
table that maps `(message_event_id | turn_id) → file_id[]`, owned by
the gateway, with its own retention policy matched to the
`webchat_updates` 14-day window. Then:

- The "stream-started then failed" case writes the turn_id even
  without an assistant reply; the user bubble survives 14d at the
  event log layer, the file mapping survives the same 14d, and the
  file's `committed=1` lifecycle is anchored to the turn rather than
  to CM.
- After 14d, the turn drops out of events AND the file mapping drops
  out, releasing the file via the orphan sweep without depending on
  user-driven `clear_history`.
- `record_chat_pair` becomes "write CM, write event, write mapping
  atomically" with a single transactional anchor.

**Why not now**: invasive — requires a new table (v5 → v6 migration),
new storage methods, rework of `emit_stream_started` /
`record_chat_pair` / orphan sweep / clear_history. v0.3.0 ships the
file-storage feature; this tightens its lifecycle once the basic
shape has been operationally validated.

---

## 2. Cookie logout invalidation is per-process

**Symptom**: in multi-worker deployments, clicking "logout" only
invalidates cookies on the worker that received the request. Other
workers continue accepting the old cookie until natural expiry
(default 24h) or admin `regenerate_token`.

**Current workaround**: documented in README §"已知限制". Operators
who run multi-worker can mitigate via sticky session, or use
`regenerate_token` for hard kicks (rotates `token_hash`, which is
folded into the cookie HMAC, so all workers reject simultaneously).
Cookie TTL is currently hard-coded at 24h (`core/file_cookie.py:
DEFAULT_TTL_SECONDS`); exposing it as config so ops could shorten
the worst-case staleness window is a sub-issue worth tracking but
not blocking — the `regenerate_token` workaround already provides
the immediate-invalidation escape hatch.

**Proposed long-term fix**: persist the logout threshold in a
shared store (DB row on the token, or Redis if available) so all
workers consult the same `last_logout_at` value. Schema cost: one
column on `tokens` (e.g. `cookie_invalidated_before`).

**Why not now**: single-process is the documented deployment model
for the plugin. The in-memory tracker is a security improvement
over "no server-side invalidation at all" without committing to
the shared-storage shape. Will revisit when multi-worker becomes a
supported deployment.

---

## 3. CM clear failure during prune is best-effort retry

**Symptom**: during the daily prune, AstrBot CM's
`update_conversation(history=[])` could in theory fail for a
session about to be physically pruned. The current implementation
collects the failure and excludes those sessions from this
iteration's `prune_chat_sync` so they retry next iteration —
correct behaviour for any transient CM issue (DB lock, disk space).

A persistent CM failure (e.g., CM data corrupted, conversation
permanently broken) would cause the session_meta to be retained
indefinitely, blocking the 90-day cleanup. The session would never
fully drain even though the user soft-deleted it 90+ days ago.

**Proposed long-term fix**: after N consecutive CM clear failures
(e.g., 3 days of retry), audit-log a `cm_clear_permanently_failed`
event and skip the CM step on subsequent prunes (still deleting
session_meta + leaving CM stale). Operators get a clear signal in
the audit log to investigate the CM data.

**Why not now**: requires retry-count state per session. Acceptable
to leave the "retry forever" behaviour for now since CM failures
should be operationally rare and an indefinitely-retained
session_meta row is harmless beyond consuming a few KB of DB space.

---

## 4. `prune_chat_sync` still bundles events + session_meta

**Symptom**: the storage layer's `prune_chat_sync(events_before_ts,
deleted_meta_before_ts, exclude_sessions)` does two unrelated DELETEs
in one transaction: events past retention, and soft-deleted
session_meta. main.py already orchestrates the rest of the prune
(file listing, file_store delete, CM clear, exclude_sessions
filtering) so the "what stays / what goes" decisions live in the
caller. The storage method is then doing two things that don't
strictly need to be in the same transaction.

**Why this is fine today**: events and session_meta deletes have no
mutual dependency. Bundling them keeps the method count low and the
failure semantic identical (both succeed or both roll back per
transaction — and a partial commit isn't observable since the loop
just logs + retries next iteration).

**Proposed long-term fix** (Option 3 from the v0.3.0 follow-up
review): split into two methods —

- `prune_events(events_before_ts)` — just the events table + the
  `_pruned_marker` dance.
- `delete_session_metas(allow_list: list[tuple[str, str]])` —
  explicit positive list of (token, session) pairs to physically
  delete. The `NOT EXISTS (file)` guard moves into the caller (via a
  separate `list_sessions_with_remaining_files` query).

After the split, the storage layer becomes pure SQL accessors and
ALL "what should be deleted" judgement lives in main.py. The
`exclude_sessions` parameter and the chained `AND NOT (...)` SQL
disappear — replaced by an explicit positive list.

**Why not now (YAGNI)**: the current `exclude_sessions` + `NOT
EXISTS (file)` machinery covers every skip condition we've actually
needed. The split would pay off if we add a third skip condition
(e.g., "session is locked by a long-lived stream") or want to expose
the events prune as a separate admin endpoint. Until then, splitting
just trades 3-line SQL chains for a new method + a new round-trip,
with no net safety win.

**Trigger to revisit**: any new "skip session_meta DELETE" condition
beyond CM-clear-failure, OR multi-tenant deployment where event
retention and meta cleanup get different schedules.
