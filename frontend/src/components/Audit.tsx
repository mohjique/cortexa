import { useEffect } from "react";
import { api } from "../api";
import { usePoll } from "../hooks";
import { fmtTime } from "./common";

/** Append-only audit log for one case, newest first. */
export default function Audit({ caseId, refreshKey }: { caseId: string; refreshKey: number }) {
  const { data, error, reload } = usePoll(() => api.getAudit(caseId), [caseId], 0);
  useEffect(() => {
    if (refreshKey) reload();
  }, [refreshKey, reload]);

  if (error) return <div className="err-text">{error.message}</div>;
  if (!data) return <div className="empty">Loading…</div>;
  const rows = [...data].reverse();
  return (
    <table className="audit" data-tour="audit">
      <thead>
        <tr>
          <th>Time</th>
          <th>Actor</th>
          <th>Action</th>
          <th>Detail</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((a, i) => (
          <tr key={i}>
            <td style={{ whiteSpace: "nowrap" }}>{fmtTime(a.ts)}</td>
            <td>
              <span className={`chip ${a.actor === "doctor" ? "doc" : a.actor === "agent" ? "ai" : ""}`}>{a.actor}</span>
            </td>
            <td>
              <code>{a.action}</code>
            </td>
            <td>{Object.keys(a.detail).length > 0 && <pre>{JSON.stringify(a.detail, null, 1)}</pre>}</td>
          </tr>
        ))}
        {!rows.length && (
          <tr>
            <td colSpan={4} className="empty">
              No entries.
            </td>
          </tr>
        )}
      </tbody>
    </table>
  );
}
