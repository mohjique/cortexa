import { useState } from "react";
import type { CaseRecord, EligibilityResult, NoMatchPlan, PreOpScreen, ShortlistTrial, TestNeeded } from "../types";
import { RecruitChip, StatusPill, fmtTime, siteText } from "./common";
import { useSubmit } from "./GateForms";
import TrialMap from "./TrialMap";
import { MutationBox } from "./CaseView";

function CriteriaTable({ rows }: { rows: EligibilityResult[] }) {
  return (
    <table className="crit">
      <thead>
        <tr>
          <th>Status</th>
          <th>Criterion (trial text)</th>
          <th>Evidence</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((e, i) => (
          <tr key={i}>
            <td>
              <StatusPill status={e.status} />
              {e.criterion_type && <div className="faint" style={{ fontSize: 10, marginTop: 2 }}>{e.criterion_type}</div>}
            </td>
            <td>"{e.criterion_text}"</td>
            <td>
              <code>{e.evidence_field}</code>
              <div className="muted">{e.reason}</div>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

const FIELD_LABEL: Record<string, string> = {
  idh: "IDH status",
  codeletion_1p19q: "1p/19q codeletion",
  mgmt: "MGMT methylation",
  tmb_clia: "CLIA TMB result",
  grade: "grade",
};
const fieldLabel = (f: string) => FIELD_LABEL[f] ?? f.replace(/_/g, " ");

type Choice = { keep: boolean | null; reason: string };

export function ShortlistReview({ record, canReview, onDone }: { record: CaseRecord; canReview: boolean; onDone: () => Promise<void> }) {
  const sl = record.shortlist!;
  const { submit, busy, error } = useSubmit(record.state.case_id, onDone);
  const [choices, setChoices] = useState<Record<string, Choice>>({});
  const [note, setNote] = useState("");
  const get = (id: string): Choice => choices[id] ?? { keep: null, reason: "" };
  const setC = (id: string, c: Partial<Choice>) => setChoices((x) => ({ ...x, [id]: { ...get(id), ...c } }));
  const undecided = sl.trials.filter((t) => get(t.trial_id).keep === null || (get(t.trial_id).keep === false && !get(t.trial_id).reason.trim()));
  const mutation = record.mutation;
  // Backend order: eligible, then needs_test, then needs_review, nearest first within each.
  const eligible = sl.trials.filter((t) => (t.group ?? "eligible") === "eligible");
  const needsTest = sl.trials.filter((t) => t.group === "needs_test");
  const needsReview = sl.trials.filter((t) => t.group === "needs_review");

  return (
    <div>
      <div className="box" style={{ marginTop: 0 }}>
        <h4>Trial shortlist · nearest first, no distance cutoff</h4>
        Every eligible trial is shown, however far away. The patient and care team decide whether the travel is worth it.
        Statuses searched: {sl.statuses_searched.join(", ")}. Distances are straight-line from the ZIP {sl.origin.zip} centroid;
        drive times are estimates.
      </div>
      <div style={{ height: 12 }} />
      <TrialMap shortlist={sl} />
      {mutation && <MutationBox m={mutation} />}
      <div style={{ height: 10 }} />
      {eligible.length > 0 && <h4 style={{ margin: "14px 0 6px" }}>Eligible on what we know ({eligible.length})</h4>}
      {eligible.map((t) => (
        <TrialCard key={t.trial_id} t={t} canReview={canReview} choice={get(t.trial_id)} onChange={(c) => setC(t.trial_id, c)} />
      ))}
      {needsTest.length > 0 && (
        <>
          <h4 style={{ margin: "14px 0 6px" }}>Needs a test before it can match ({needsTest.length})</h4>
          <div className="faint" style={{ fontSize: 12, marginBottom: 6 }}>
            Not a match yet. A key result is unknown; the unknown row says which test decides it.
          </div>
        </>
      )}
      {needsTest.map((t) => (
        <TrialCard key={t.trial_id} t={t} canReview={canReview} choice={get(t.trial_id)} onChange={(c) => setC(t.trial_id, c)} />
      ))}
      {needsReview.length > 0 && (
        <>
          <h4 style={{ margin: "14px 0 6px" }}>Needs doctor review of the criteria ({needsReview.length})</h4>
          <div className="faint" style={{ fontSize: 12, marginBottom: 6 }}>
            Not a match yet. No clinical criterion could be checked automatically; nothing ruled these out. Read the criteria to decide.
          </div>
        </>
      )}
      {needsReview.map((t) => (
        <TrialCard key={t.trial_id} t={t} canReview={canReview} choice={get(t.trial_id)} onChange={(c) => setC(t.trial_id, c)} />
      ))}
      {canReview && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (undecided.length) return;
            const keep = sl.trials.filter((t) => get(t.trial_id).keep).map((t) => t.trial_id);
            const reject = sl.trials
              .filter((t) => get(t.trial_id).keep === false)
              .map((t) => ({ trial_id: t.trial_id, reason: get(t.trial_id).reason.trim() }));
            submit({ gate: "review_shortlist", decision: { keep, reject } }, note);
          }}
        >
          <div className="box gate">
            <h4>Record shortlist review</h4>
            Keep or reject every trial. A rejection needs a reason.
            <label className="field">
              <span>Note (optional, no patient identifiers)</span>
              <textarea value={note} onChange={(e) => setNote(e.target.value)} />
            </label>
            <div className="row-actions">
              <button className="btn" type="submit" disabled={busy || undecided.length > 0}>
                {busy ? "Recording…" : "Record review"}
              </button>
              {undecided.length > 0 && <span className="faint">{undecided.length} trial(s) still need a decision</span>}
            </div>
            {error && <div className="err-text">{error}</div>}
          </div>
        </form>
      )}
    </div>
  );
}

function TrialCard({ t, canReview, choice, onChange }: { t: ShortlistTrial; canReview: boolean; choice: Choice; onChange: (c: Partial<Choice>) => void }) {
  const decided = canReview ? choice.keep : t.doctor_decision === "pending" ? null : t.doctor_decision === "keep";
  return (
    <div data-tour="trial-card" className={`trial ${decided === true ? "kept" : decided === false ? "rejected" : ""}`}>
      <div className="thead">
        <b>{t.trial_id}</b>
        <RecruitChip status={t.recruitment_status} label={t.status_label} />
        {t.remote_participation && <span className="chip doc">Remote participation</span>}
        {t.group === "needs_test" && (
          <span className="chip warn">Needs a test: {(t.needs_test_fields ?? []).map(fieldLabel).join(", ")}</span>
        )}
        {!canReview && t.doctor_decision !== "pending" && (
          <span className={`chip ${t.doctor_decision === "keep" ? "doc" : ""}`}>Doctor: {t.doctor_decision}</span>
        )}
      </div>
      <div className="ttitle">{t.title}</div>
      <div className="demo" style={{ marginTop: 4 }}>
        Nearest site: {siteText(t.nearest_site, t.remote_participation)}
      </div>
      <div style={{ marginTop: 6, fontSize: 12 }}>
        <StatusPill status="met" /> {t.summary.met} &nbsp; <StatusPill status="unknown" /> {t.summary.unknown} &nbsp;
        <StatusPill status="not_met" /> {t.summary.not_met}
        {t.summary.unknown > 0 && <span className="faint"> · unknown criteria need doctor review</span>}
      </div>
      <details>
        <summary>Show {t.eligibility.length} criteria with evidence</summary>
        <CriteriaTable rows={t.eligibility} />
      </details>
      {canReview && (
        <div className="choice">
          <label className="radio">
            <input type="radio" name={`k-${t.trial_id}`} checked={choice.keep === true} onChange={() => onChange({ keep: true })} /> Keep
          </label>
          <label className="radio">
            <input type="radio" name={`k-${t.trial_id}`} checked={choice.keep === false} onChange={() => onChange({ keep: false })} /> Reject
          </label>
          {choice.keep === false && (
            <input
              style={{ flex: 1, minWidth: 180 }}
              placeholder="Reason (required)"
              value={choice.reason}
              onChange={(e) => onChange({ reason: e.target.value })}
            />
          )}
        </div>
      )}
    </div>
  );
}

export function NoMatch({ plan }: { plan: NoMatchPlan }) {
  return (
    <div>
      <div className="box" style={{ marginTop: 0 }}>
        <h4>No-match plan</h4>
        No eligible trial in the searched statuses ({plan.searched.statuses.join(", ")}). There is no distance cutoff, so there is
        no wider search to try.
      </div>
      <h4 style={{ margin: "14px 0 6px" }}>Near misses ({plan.near_misses.length})</h4>
      {!plan.near_misses.length && <div className="empty">None: no trial fails just one rule or waits on a pending result.</div>}
      {plan.near_misses.map((n) => (
        <div className="trial" key={n.trial_id}>
          <div className="thead">
            <b>{n.trial_id}</b>
            <span className={`chip ${n.near_miss_type === "pending_result" ? "warn" : "danger"}`}>
              {n.near_miss_type === "pending_result" ? "Waiting on a pending result" : "Fails one rule"}
            </span>
          </div>
          <div className="ttitle">{n.title}</div>
          <div className="demo" style={{ marginTop: 4 }}>
            Nearest site: {siteText(n.nearest_site)}
          </div>
          <CriteriaTable rows={n.blocking} />
        </div>
      ))}
      {plan.alert && (
        <div className="box">
          <h4>Trial alert set</h4>
          The care team is notified in the app when a new trial opens for {plan.alert.query.bucket.replace("_", " ")}
          {Object.keys(plan.alert.query.markers).length > 0 &&
            ` (${Object.entries(plan.alert.query.markers)
              .map(([k, v]) => `${k}: ${v}`)
              .join(", ")})`}
          . Patients are never contacted by the app. Created {fmtTime(plan.alert.created_at)}.
        </div>
      )}
      {plan.tumor_board_log && (
        <div className="box">
          <h4>Tumor board log</h4>"{plan.tumor_board_log.entry}" · logged {fmtTime(plan.tumor_board_log.ts)}
        </div>
      )}
    </div>
  );
}

function formatRequirement(field: TestNeeded["field"], requires: string): string {
  if (field !== "grade") return requires;
  const [lo, hi] = requires.split("-");
  return lo === hi ? `grade ${lo}` : `grade ${lo}-${hi}`;
}

/** [requirement, trials] pairs, most trials first. */
function requirementCounts(t: TestNeeded): [string, number][] {
  const counts = new Map<string, number>();
  for (const e of t.evidence ?? []) {
    if (e.requires) counts.set(e.requires, (counts.get(e.requires) ?? 0) + 1);
  }
  return [...counts.entries()].sort((a, b) => b[1] - a[1]);
}

export function PreOp({ preop }: { preop: PreOpScreen }) {
  return (
    <div className="box">
      <h4>Pre-op screen · {preop.label}</h4>
      <div className="muted">{preop.trials_considered} trial(s) considered.</div>
      {preop.tests_needed.length > 0 ? (
        <table className="crit">
          <thead>
            <tr>
              <th>Biopsy must answer</th>
              <th>Now</th>
              <th>Decides</th>
            </tr>
          </thead>
          <tbody>
            {preop.tests_needed.map((t) => {
              const evidence = t.evidence ?? [];
              const reqs = requirementCounts(t);
              const mentions = t.mentions_trials ?? 0;
              return (
                <tr key={t.field}>
                  <td>{t.label}</td>
                  <td>{t.current_value}</td>
                  <td>
                    {t.decides_trials} trial(s)
                    {reqs.length > 0 && (
                      <div className="muted">{reqs.map(([r, n]) => `${n} need ${formatRequirement(t.field, r)}`).join(", ")}</div>
                    )}
                    {mentions > 0 && (
                      <div>
                        <span className="chip warn">Mentioned by {mentions}: check criteria</span>
                      </div>
                    )}
                    {evidence.length > 0 ? (
                      <details>
                        <summary className="faint">Criteria cited</summary>
                        {evidence.map((e) => (
                          <div className="crit" key={`${e.trial_id}-${e.requires ?? "mention"}`}>
                            <span>
                              {e.trial_id} ·{" "}
                              {e.requires ? `needs ${formatRequirement(t.field, e.requires)}` : "mentions, check criteria"}
                            </span>
                            "{e.criterion_text}"
                          </div>
                        ))}
                      </details>
                    ) : (
                      <div className="faint">{t.trial_ids.join(", ")}</div>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      ) : (
        <div className="faint" style={{ marginTop: 4 }}>No tests listed.</div>
      )}
      {preop.pre_surgery_trials.length > 0 && (
        <div className="box urgent">
          <h4>Enroll before surgery</h4>
          These trials only accept patients before resection. Once surgery happens the patient can no longer join.
          {preop.pre_surgery_trials.map((t) => (
            <div className="ptrial" key={t.trial_id}>
              <div>
                <b>{t.trial_id}</b> <RecruitChip status={t.recruitment_status} />
                {t.matched_on === "title_hint" && <span className="chip warn">Possible: check criteria</span>}
                {t.doctor_action && t.doctor_action !== "pending" && (
                  <span className={`chip ${t.doctor_action === "pursue_before_surgery" ? "doc" : ""}`}>
                    Doctor: {t.doctor_action === "pursue_before_surgery" ? "pursue before surgery" : "not pursuing"}
                  </span>
                )}
              </div>
              <div className="ptitle">{t.title}</div>
              <div className="crit">
                <span>{t.matched_on === "title_hint" ? "Matched on title only" : "Criterion"}</span>"{t.criterion_text}"
              </div>
              {t.doctor_reason && <div className="muted">Reason: {t.doctor_reason}</div>}
              <div className="demo">Nearest site: {siteText(t.nearest_site)}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
