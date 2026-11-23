// This browser's demo workspace (issue 48). A random ID, made on first load and
// kept in localStorage, sent on every API call as the X-Cortexa-Workspace header
// (and as ?ws= on the two case image URLs, since an <img> can't send a header).
// The backend keeps each workspace's cases and audit log apart.
//
// Isolation, not authentication: anyone holding the ID can open the workspace.
// That is acceptable only because every case is synthetic (safety rule 9).
// The doctor ID (hooks.ts) is attribution on decisions, not scoping.

export const WORKSPACE_HEADER = "X-Cortexa-Workspace";
const KEY = "cortexa.workspace_id";

function randomId(): string {
  try {
    if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  } catch {
    /* not a secure context: fall through */
  }
  const b = new Uint8Array(16);
  crypto.getRandomValues(b);
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}

let cached: string | null = null;

/** The workspace ID. Falls back to an in-memory ID for this tab if storage is blocked. */
export function workspaceId(): string {
  if (cached) return cached;
  try {
    const stored = localStorage.getItem(KEY);
    if (stored && /^[A-Za-z0-9_-]{8,64}$/.test(stored)) {
      cached = stored;
      return cached;
    }
  } catch {
    /* storage blocked */
  }
  cached = randomId();
  try {
    localStorage.setItem(KEY, cached);
  } catch {
    /* storage blocked: keep it in memory for this tab */
  }
  return cached;
}
