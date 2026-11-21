// Thin client for agent/api.py. Every write goes through the backend, which
// enforces the gates, policy and audit log. The UI never decides a stage change.
import type { AuditEntry, CaseRecord, CaseState, DecisionRecord, GateDecision } from "./types";

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
      headers: { Accept: "application/json", ...(init?.headers ?? {}) },
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

export const api = {
  listCases: () => request<CaseState[]>("/cases"),
  getCase: (id: string) => request<CaseState>(`/cases/${encodeURIComponent(id)}`),
  getRecord: (id: string) => request<CaseRecord>(`/cases/${encodeURIComponent(id)}/record`),
  getAudit: (id: string) => request<AuditEntry[]>(`/cases/${encodeURIComponent(id)}/audit`),
  runAgent: (id: string) =>
    request<CaseState>(`/cases/${encodeURIComponent(id)}/run`, { method: "POST" }),

  submitDecision: (caseId: string, doctorId: string, d: GateDecision, note?: string) => {
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

  /**
   * MRI intake. Proposed endpoint (not in agent/api.py yet):
   *   POST /cases  multipart/form-data  { case_id, file }  -> CaseState
   * It should create the case, run the classifier and attach its output
   * (workflow.attach_classification), leaving the case at confirm_bucket.
   */
  createCase: (caseId: string, file: File) => {
    const form = new FormData();
    form.append("case_id", caseId);
    form.append("file", file);
    return request<CaseState>("/cases", { method: "POST", body: form });
  },
};
