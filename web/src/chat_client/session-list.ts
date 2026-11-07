/** Preserve local sessions that still contain a message awaiting server sync. */
export function mergePendingLocalSessions<T>(
  listedSessions: Record<string, T>,
  currentSessions: Record<string, T>,
  pendingSessionIds: ReadonlySet<string>,
  deletedSessionIds: ReadonlySet<string>,
): Record<string, T> {
  const merged = { ...listedSessions };
  for (const sessionId of pendingSessionIds) {
    if (merged[sessionId] || deletedSessionIds.has(sessionId)) continue;
    const local = currentSessions[sessionId];
    if (local) merged[sessionId] = local;
  }
  return merged;
}
