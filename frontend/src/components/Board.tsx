import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { usePoll } from "../hooks";
import { BUCKET_LABEL, COLUMNS, STAGE_LABEL, waitingOn } from "../pathway";
import type { CaseState } from "../types";
import { Owner, WaitChip, ErrorBanner } from "./common";

export default function Board() {
  const { data, error, loading } = usePoll(() => api.listCases(), [], 10000);
  const nav = useNavigate();
  const cases = data ?? [];

  return (
    <>
      {error && <ErrorBanner error={error} />}
      <section id="kanban">
        {COLUMNS.map((c) => {
          const items = cases.filter((s) => c.stages.includes(s.stage));
          const counts: Record<string, number> = {};
          items.forEach((s) => {
            const w = waitingOn(s.stage, s.waiting_on);
            counts[w] = (counts[w] ?? 0) + 1;
          });
          return (
            <div className="col" key={c.key}>
              <div className="colhead">
                <h3>
                  {c.title}
                  <span className="n">{items.length}</span>
                </h3>
                <Owner owner={c.owner} />
                <div className="waitlabel">Waiting on</div>
                <div className="waittext">{c.wait}</div>
                <div className="chips">
                  {(["doctor", "pathology", "agent", "none"] as const)
                    .filter((w) => counts[w])
                    .map((w) =>
                      w === "none" ? (
                        <span key={w} className="chip">
                          Closed · {counts[w]}
                        </span>
                      ) : (
                        <WaitChip key={w} who={w} label={`${w[0].toUpperCase() + w.slice(1)} · ${counts[w]}`} />
                      ),
                    )}
                  {!items.length && <span className="chip">None waiting</span>}
                </div>
              </div>
              {items.map((s) => (
                <Card key={s.case_id} s={s} onOpen={() => nav(`/cases/${encodeURIComponent(s.case_id)}`)} />
              ))}
            </div>
          );
        })}
        {!loading && !error && !cases.length && (
          <div className="empty" style={{ alignSelf: "center" }}>
            No cases yet. Upload an MRI from <b>New case</b>.
          </div>
        )}
      </section>
    </>
  );
}

function Card({ s, onOpen }: { s: CaseState; onOpen: () => void }) {
  const bucket = s.confirmed_bucket ?? s.suspected_bucket;
  const overridden = s.confirmed_bucket && s.suspected_bucket && s.confirmed_bucket !== s.suspected_bucket;
  const who = waitingOn(s.stage, s.waiting_on);
  return (
    <div
      className={`card ${bucket === "no_tumor" ? "no-tumor" : bucket ? "tumor" : ""}`}
      onClick={onOpen}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onOpen()}
      role="button"
      tabIndex={0}
    >
      <div className="top">
        <span className="id">{s.case_id}</span>
        <span className="demo">{new Date(s.updated_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
      </div>
      <div className="bucket">
        Suspected: <b>{s.suspected_bucket ? BUCKET_LABEL[s.suspected_bucket] : "pending"}</b>
        {s.confirmed_bucket && (
          <>
            <br />
            Confirmed by doctor: <b>{BUCKET_LABEL[s.confirmed_bucket]}</b>
          </>
        )}
      </div>
      <div className="next">{STAGE_LABEL[s.stage]}</div>
      <div className="chips">
        {who !== "none" && <WaitChip who={who} label={`Waiting on ${who}`} />}
        {overridden && <span className="chip danger">Doctor override</span>}
        {s.treatment_path === "medication_only" && <span className="chip">Meds only</span>}
      </div>
    </div>
  );
}
