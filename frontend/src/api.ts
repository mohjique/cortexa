// Thin client for agent/api.py. Every write goes through the backend, which
// enforces the gates, policy and audit log. The UI never decides a stage change.
import type { AuditEntry, CaseRecord, CaseState, DecisionRecord, GateDecision } from "./types";
import { WORKSPACE_HEADER, workspaceId } from "./workspace";
import { EXAMPLE_AUDIT, EXAMPLE_RECORD, TOUR_CASE_ID, TOUR_SOURCE_PATIENT } from "./tutorial/exampleCase";

// Production: VITE_API_URL points at the deployed FastAPI server.
// Local dev: unset, so requests go to /api and Vite proxies them to :8000.
export const API_BASE = (import.meta.env.VITE_API_URL as string | undefined)?.replace(/\/+$/, "") || "/api";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

function formatDetail(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    // FastAPI / pydantic validation errors
    return detail
      .map((d) => {
        const loc = Array.isArray(d?.loc) ? d.loc.filter((x: unknown) => x !== "body").join(".") : "";
        return loc ? `${loc}: ${d?.msg ?? JSON.stringify(d)}` : (d?.msg ?? JSON.stringify(d));
      })
      .join("\n");
  }
  return JSON.stringify(detail);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      ...init,
      // Every call carries this browser's workspace (workspace.ts); case routes 400 without it.
      headers: { Accept: "application/json", [WORKSPACE_HEADER]: workspaceId(), ...(init?.headers ?? {}) },
    });
  } catch {
    throw new ApiError(0, `Can't reach the Cortexa backend at ${API_BASE}.`);
  }
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body?.detail !== undefined) msg = formatDetail(body.detail);
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, msg);
  }
  return (await res.json()) as T;
}

export type Health = { status: string; classifier: string; trial_source?: "demo" | "live" };

// The guided tour's example case (tutorial/exampleCase.ts) is served from the
// bundle and never reaches the backend: reads return the fixture, and every
// write for it is refused here, so nothing on it can be approved or changed.
const isExample = (id: string) => id === TOUR_CASE_ID;
const refuseExample = () =>
  Promise.reject(new ApiError(0, "This is the tour's example case. It can't be changed; open a real case instead."));

export const api = {
  /** Liveness, classifier, and where trials come from (`demo` or `live`). */
  health: () => request<Health>("/health"),
  listCases: () => request<CaseState[]>("/cases"),
  getCase: (id: string) =>
    isExample(id) ? Promise.resolve(EXAMPLE_RECORD.state as CaseState) : request<CaseState>(`/cases/${encodeURIComponent(id)}`),
  getRecord: (id: string) =>
    isExample(id) ? Promise.resolve(EXAMPLE_RECORD as CaseRecord) : request<CaseRecord>(`/cases/${encodeURIComponent(id)}/record`),
  getAudit: (id: string) =>
    isExample(id) ? Promise.resolve(EXAMPLE_AUDIT as AuditEntry[]) : request<AuditEntry[]>(`/cases/${encodeURIComponent(id)}/audit`),
  runAgent: (id: string) =>
    isExample(id) ? refuseExample() : request<CaseState>(`/cases/${encodeURIComponent(id)}/run`, { method: "POST" }),

  submitDecision: (caseId: string, doctorId: string, d: GateDecision, note?: string) => {
    if (isExample(caseId)) return refuseExample();
    const body: DecisionRecord = {
      ...d,
      case_id: caseId,
      doctor_id: doctorId,
      ts: new Date().toISOString(),
      note: note?.trim() ? note.trim() : null,
    } as DecisionRecord;
    return request<CaseState>(`/cases/${encodeURIComponent(caseId)}/decisions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  },

  /** Doctor asks for a fresh pre-op screen. 409 unless the case waits on preop_review. */
  rescreenPreop: (caseId: string, doctorId: string, reason?: string) =>
    isExample(caseId) ? refuseExample() : request<CaseState>(`/cases/${encodeURIComponent(caseId)}/preop/rescreen`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ doctor_id: doctorId, reason: reason?.trim() ? reason.trim() : null }),
    }),

  /** Delete this browser's cases and audit log and start a fresh demo workspace. */
  resetWorkspace: () =>
    request<{ status: string; removed_cases: number }>("/workspace/reset", { method: "POST" }),

  // The two case image URLs go in <img src>, which can't send a header, so they
  // carry the workspace as ?ws= instead. Only these two routes accept it.

  /** Grad-CAM overlay PNG. 404 when there is none (e.g. the stub classifier). */
  heatmapUrl: (caseId: string) =>
    `${API_BASE}/cases/${encodeURIComponent(caseId)}/heatmap?ws=${encodeURIComponent(workspaceId())}`,

  /** The case's MRI as uploaded (PNG). 404 when it wasn't kept. The example case shows its demo MRI. */
  scanUrl: (caseId: string) =>
    isExample(caseId)
      ? api.demoScanUrl(TOUR_SOURCE_PATIENT)
      : `${API_BASE}/cases/${encodeURIComponent(caseId)}/image?ws=${encodeURIComponent(workspaceId())}`,

  /** The committed demo MRIs: case IDs only, no label or model call. */
  listDemoScans: () => request<{ case_id: string }[]>("/demo-scans"),
  demoScanUrl: (caseId: string) => `${API_BASE}/demo-scans/${encodeURIComponent(caseId)}`,

  /** Open a demo MRI as a case: fetch it and upload it like any other image. */
  createCaseFromDemo: async (caseId: string): Promise<CaseState> => {
    let res: Response;
    try {
      // The demo gallery is shared and needs no workspace; the header is sent anyway, like every call.
      res = await fetch(`${API_BASE}/demo-scans/${encodeURIComponent(caseId)}`, {
        headers: { [WORKSPACE_HEADER]: workspaceId() },
      });
    } catch {
      throw new ApiError(0, `Can't reach the Cortexa backend at ${API_BASE}.`);
    }
    if (!res.ok) throw new ApiError(res.status, `demo scan ${caseId} not found`);
    const blob = await res.blob();
    return api.createCase(caseId, new File([blob], `${caseId}.jpg`, { type: blob.type || "image/jpeg" }));
  },

  /**
   * MRI intake (docs/interfaces.md, "Upload endpoint"):
   *   POST /cases?case_id=<id>&filename=<name>, raw image bytes as the body -> CaseState (201)
   * The backend classifies, creates the case and moves it to confirm_bucket.
   * 422 unreadable image (no case created), 409 case exists, 413 over 20 MB.
   */
  createCase: (caseId: string, file: File) => {
    const q = new URLSearchParams({ case_id: caseId, filename: file.name });
    return request<CaseState>(`/cases?${q}`, {
      method: "POST",
      headers: { "Content-Type": file.type || "application/octet-stream" },
      body: file,
    });
  },
};
