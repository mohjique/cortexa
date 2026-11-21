import doctorIcon from "../assets/doctor.jpg";
import agentIcon from "../assets/agent.jpg";
import labIcon from "../assets/lab.jpg";
import noneIcon from "../assets/none.jpg";
import { ApiError } from "../api";
import type { EligibilityStatus, NearestSite } from "../types";

const ICON = { doctor: doctorIcon, agent: agentIcon, lab: labIcon, none: noneIcon };
const NAME = { doctor: "Doctor", agent: "Agent", lab: "Pathology lab", none: "No owner" };

export function Owner({ owner }: { owner: "doctor" | "agent" | "lab" | null }) {
  const k = owner ?? "none";
  return (
    <div className={`owner ${k}`}>
      <img className="av" src={ICON[k]} alt="" />
      <span>
        <small>Owner</small>
        {NAME[k]}
      </span>
    </div>
  );
}

const WAIT_CLASS: Record<string, string> = { doctor: "doc", agent: "ai", pathology: "warn" };
export function WaitChip({ who, label }: { who: string; label: string }) {
  return <span className={`chip ${WAIT_CLASS[who] ?? ""}`}>{label}</span>;
}

export function ErrorBanner({ error }: { error: Error }) {
  const offline = error instanceof ApiError && error.status === 0;
  return (
    <div className="banner err">
      {error.message}
      {offline && " Check that the backend is running and that its CORTEXA_CORS_ORIGINS includes this site."}
    </div>
  );
}

export function StatusPill({ status }: { status: EligibilityStatus }) {
  const label = { met: "met", not_met: "not met", unknown: "unknown" }[status];
  return <span className={`st ${status}`}>{label}</span>;
}

/** Label for a recruitment status chip. Uses the backend's policy label when it has one. */
export function RecruitChip({ status, label }: { status: string; label?: string }) {
  const text = label ?? (status === "RECRUITING" ? "Recruiting" : status.replace(/_/g, " ").toLowerCase());
  return <span className={`chip ${status === "RECRUITING" ? "doc" : "warn"}`}>{text}</span>;
}

export function siteText(site: NearestSite | null | undefined, remote = false): string {
  if (!site) return remote ? "Remote participation (no site visit needed)" : "No site location listed";
  const where = [site.facility, [site.city, site.state].filter(Boolean).join(", ")].filter(Boolean).join(" · ");
  const drive = site.drive_minutes != null ? ` · about ${site.drive_minutes} min drive` : " · drive time unavailable";
  return `${where} · ${site.distance_miles.toFixed(1)} mi${drive}`;
}

export function fmtTime(ts: string) {
  return new Date(ts).toLocaleString([], { dateStyle: "short", timeStyle: "medium" });
}
