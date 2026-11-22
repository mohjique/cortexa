import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { usePoll } from "../hooks";
import { BUCKET_LABEL, STAGE_LABEL, nodeById, routeFor, waitingOn } from "../pathway";
import type { CaseRecord, Gate, MutationCheckOutput, Stage } from "../types";
import Tree from "./Tree";
import Audit from "./Audit";
import { ErrorBanner, WaitChip } from "./common";
import {
  AgentRetry,
  ConfirmBucketForm,
  HormoneLabsForm,
  PreopReviewForm,
  SurgeryOrMedsForm,
  WatchOrTreatForm,
} from "./GateForms";
import ResultsForm from "./ResultsForm";
import { NoMatch, PreOp, ShortlistReview } from "./Trials";

// Mirrors GATE_AT_STAGE in agent/workflow.py. The backend still checks it.
const GATE_AT_STAGE: Partial<Record<Stage, Gate>> = {
  confirm_bucket: "confirm_bucket",
  watch_or_treat: "watch_or_treat",
  preop_screen: "preop_review", // only once the screen has run (waiting_on: doctor)
  hormone_labs: "hormone_labs",
  surgery_or_meds: "surgery_or_meds",
  awaiting_pathology: "enter_results",
  enter_results: "enter_results",
  review_shortlist: "review_shortlist",
};
const AGENT_STAGES: Stage[] = ["intake", "preop_screen", "mutation_check", "matching"];

type Tab = "pathway" | "results" | "trials" | "audit";

/** The doctor gate the case waits on, if any. preop_screen is an agent stage until the screen is built. */
function gateFor(record: CaseRecord): Gate | undefined {
  const { stage, waiting_on } = record.state;
  if (stage === "preop_screen") return waiting_on === "doctor" && record.preop ? "preop_review" : undefined;
  return GATE_AT_STAGE[stage];
}

export default function CaseView() {
  const { caseId } = useParams();
  const nav = useNavigate();
  const list = usePoll(() => api.listCases(), [], 15000);

  // /cases with no id: open the most recently updated case.
  useEffect(() => {
    if (!caseId && list.data?.length) nav(`/cases/${encodeURIComponent(list.data[0].case_id)}`, { replace: true });
  }, [caseId, list.data, nav]);

  const rec = usePoll(() => (caseId ? api.getRecord(caseId) : Promise.resolve(null)), [caseId], 8000);
  const [auditKey, setAuditKey] = useState(0);

  const refresh = async () => {
    await Promise.all([rec.reload(), list.reload()]);
    setAuditKey((k) => k + 1);
  };

  const record = rec.data;
  const gate = record ? gateFor(record) : undefined;
  const hasTrials = !!(record?.shortlist || record?.no_match || record?.preop);
  const tabs: [Tab, string][] = [
    ["pathway", "Pathway"],
    ...(gate === "enter_results" ? ([["results", "Enter results"]] as [Tab, string][]) : []),
    ...(hasTrials ? ([["trials", record?.shortlist ? "Trial shortlist" : record?.no_match ? "No-match plan" : "Pre-op screen"]] as [Tab, string][]) : []),
    ["audit", "Audit log"],
  ];
  const defaultTab: Tab =
    gate === "enter_results" ? "results" : gate === "review_shortlist" || record?.state.stage === "no_match" ? "trials" : "pathway";
  const [tab, setTab] = useState<Tab>("pathway");
  useEffect(() => setTab(defaultTab), [caseId, record?.state.stage]); // eslint-disable-line react-hooks/exhaustive-deps
  const shownTab = tabs.some(([k]) => k === tab) ? tab : "pathway";

  const route = useMemo(() => (record ? routeFor(record) : null), [record]);

  return (
    <>
      {(list.error || rec.error) && <ErrorBanner error={(rec.error ?? list.error)!} />}
      <section id="pathway">
        <div className="list">
          {(list.data ?? []).map((s) => (
            <Link
              key={s.case_id}
              to={`/cases/${encodeURIComponent(s.case_id)}`}
              className={`row ${s.case_id === caseId ? "on" : ""}`}
              style={{ display: "block", color: "inherit", textDecoration: "none" }}
            >
              {s.case_id} ·{" "}
              {s.confirmed_bucket
                ? BUCKET_LABEL[s.confirmed_bucket]
                : s.suspected_bucket
                  ? `Suspected ${BUCKET_LABEL[s.suspected_bucket].toLowerCase()}`
                  : "Pending"}
              <small>{STAGE_LABEL[s.stage]}</small>
            </Link>
          ))}
          {list.data && !list.data.length && <div className="empty" style={{ padding: 12 }}>No cases yet.</div>}
        </div>

        <div className="center">
          <div className="ctabs" role="tablist">
            {tabs.map(([k, label]) => (
              <button key={k} role="tab" aria-selected={shownTab === k} className={shownTab === k ? "on" : ""} onClick={() => setTab(k)}>
                {label}
              </button>
            ))}
          </div>
          <div className="cbody">
            {shownTab === "pathway" && (
              <div className="canvas">
                <Tree route={route} />
              </div>
            )}
            {shownTab === "results" && record && <ResultsForm record={record} onDone={refresh} />}
            {shownTab === "trials" && record && (
              <>
                {record.shortlist && (
                  <ShortlistReview record={record} canReview={gate === "review_shortlist"} onDone={refresh} />
                )}
                {record.no_match && <NoMatch plan={record.no_match} />}
                {record.preop && <PreOp preop={record.preop} />}
              </>
            )}
            {shownTab === "audit" && caseId && <Audit caseId={caseId} refreshKey={auditKey} />}
          </div>
        </div>

        <div className="detail">
          {!caseId && <div className="empty">Select a case.</div>}
          {caseId && !record && !rec.error && <div className="empty">Loading…</div>}
          {record && route && (
            <Detail record={record} gate={gate} cur={route.cur} onDone={refresh} openTab={setTab} />
          )}
        </div>
      </section>
    </>
  );
}

function Detail({
  record,
  gate,
  cur,
  onDone,
  openTab,
}: {
  record: CaseRecord;
  gate: Gate | undefined;
  cur: string;
  onDone: () => Promise<void>;
  openTab: (t: Tab) => void;
}) {
  const s = record.state;
  const who = waitingOn(s.stage, s.waiting_on);
  const c = record.classifier;
  const overrode = s.confirmed_bucket && s.suspected_bucket && s.confirmed_bucket !== s.suspected_bucket;

  return (
    <>
      <h2>{s.case_id}</h2>
      <div className="sub">
        Synthetic case · opened {new Date(record.created_at).toLocaleDateString()} · updated{" "}
        {new Date(s.updated_at).toLocaleTimeString()}
      </div>
      <div className="chips">
        {who !== "none" && <WaitChip who={who} label={`Waiting on ${who}`} />}
        {c?.flagged_uncertain && <span className="chip danger">Low confidence: look harder</span>}
        {overrode && <span className="chip danger">Doctor override</span>}
      </div>
      <div className="kv">
        <div>Suspected</div>
        <div>
          {s.suspected_bucket ? <b>{BUCKET_LABEL[s.suspected_bucket]}</b> : "pending"}
          {c && <> ({Math.round(c.confidence * 100)}% confidence)</>}
        </div>
        {s.confirmed_bucket && (
          <>
            <div>Confirmed</div>
            <div>
              <b>{BUCKET_LABEL[s.confirmed_bucket]}</b> {overrode ? "(doctor override)" : "(by doctor)"}
            </div>
          </>
        )}
        {s.treatment_path && (
          <>
            <div>Path</div>
            <div>{s.treatment_path.replace("_", " ")}</div>
          </>
        )}
        <div>Current step</div>
        <div>{nodeById[cur]?.t ?? s.stage}</div>
      </div>

      {gate && (
        <div className="box gate">
          <h4>Doctor action needed</h4>
          <div>{gate === "preop_review" ? "Review the pre-op screen and answer each enroll-before-surgery trial" : STAGE_LABEL[s.stage]}</div>
          {gate === "confirm_bucket" && <ConfirmBucketForm record={record} onDone={onDone} />}
          {gate === "watch_or_treat" && <WatchOrTreatForm record={record} onDone={onDone} />}
          {gate === "preop_review" && (
            <>
              <div className="row-actions">
                <a href="#" onClick={(e) => (e.preventDefault(), openTab("trials"))}>
                  See the full pre-op screen
                </a>
              </div>
              <PreopReviewForm key={record.preop!.screen_id} record={record} onDone={onDone} />
            </>
          )}
          {gate === "hormone_labs" && <HormoneLabsForm record={record} onDone={onDone} />}
          {gate === "surgery_or_meds" && <SurgeryOrMedsForm record={record} onDone={onDone} />}
          {gate === "enter_results" && (
            <div className="row-actions">
              {s.stage === "awaiting_pathology" && <div className="muted">Enter results when the pathology report is back.</div>}
              <button className="btn" onClick={() => openTab("results")}>
                Open results form
              </button>
            </div>
          )}
          {gate === "review_shortlist" && (
            <div className="row-actions">
              <button className="btn" onClick={() => openTab("trials")}>
                Review shortlist
              </button>
            </div>
          )}
        </div>
      )}

      {!gate && AGENT_STAGES.includes(s.stage) && (
        <div className="box">
          <h4>Agent working</h4>
          {STAGE_LABEL[s.stage]}. If this doesn't move on, the last agent step may have failed (see the audit log).
          {s.stage !== "intake" && <AgentRetry caseId={s.case_id} onDone={onDone} />}
        </div>
      )}

      {s.stage === "no_match" && (
        <div className="box">
          <h4>No current match</h4>
          No eligible trial in the searched statuses. Near misses and the trial alert are in the{" "}
          <a href="#" onClick={(e) => (e.preventDefault(), openTab("trials"))}>
            No-match plan
          </a>{" "}
          tab.
        </div>
      )}
      {s.stage === "stopped" && (
        <div className="box">
          <h4>Closed</h4>
          {record.shortlist ? "Shortlist reviewed. No further agent steps." : "No trial search for this case."}
        </div>
      )}
      {s.stage === "rescan" && (
        <div className="box">
          <h4>Rescan scheduled</h4>
          {rescanText(record)}
        </div>
      )}

      {c && (
        <div className="box">
          <h4>Classifier (suspected only)</h4>
          {(Object.entries(c.probabilities) as [keyof typeof BUCKET_LABEL, number][])
            .sort((a, b) => b[1] - a[1])
            .map(([b, p]) => (
              <div className="prob" key={b}>
                <span>{BUCKET_LABEL[b]}</span>
                <span className="bar">
                  <i style={{ width: `${Math.round(p * 100)}%` }} />
                </span>
                <span>{Math.round(p * 100)}%</span>
              </div>
            ))}
          {c.heatmap_path && <Heatmap caseId={s.case_id} />}
          {c.reasoning && (
            <div style={{ marginTop: 10 }}>
              <div style={{ fontSize: 12 }}>
                <b>Reasoning: suspected {BUCKET_LABEL[c.suspected_bucket].toLowerCase()}</b>{" "}
                <span className="chip warn">AI draft · not a diagnosis</span>
              </div>
              <div className="muted" style={{ marginTop: 4, fontSize: 13, whiteSpace: "pre-wrap" }}>
                {c.reasoning}
              </div>
              <div className="faint" style={{ fontSize: 11 }}>
                Written by MedGemma from the image, the classifier's call and this bucket's pathway. The doctor
                confirms or corrects the bucket.
              </div>
            </div>
          )}
          {c.findings_summary && (
            <div style={{ marginTop: 10 }}>
              <div style={{ fontSize: 12 }}>
                <b>Findings summary</b> <span className="chip warn">AI draft · not a diagnosis</span>
              </div>
              <div className="muted" style={{ marginTop: 4, fontSize: 13, whiteSpace: "pre-wrap" }}>
                {c.findings_summary}
              </div>
              <div className="faint" style={{ fontSize: 11 }}>
                Written by MedGemma from the image. The doctor confirms the bucket.
              </div>
            </div>
          )}
        </div>
      )}

      {record.results && <ResultsSummary record={record} />}
      {record.mutation && <MutationBox m={record.mutation} />}
    </>
  );
}

/** Grad-CAM overlay served by GET /cases/{id}/heatmap. Hidden on 404 (no heatmap, e.g. the stub). */
function Heatmap({ caseId }: { caseId: string }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [caseId]);
  if (failed) return null;
  return (
    <>
      <img
        src={api.heatmapUrl(caseId)}
        alt="Grad-CAM heatmap: where the classifier looked"
        onError={() => setFailed(true)}
        style={{ width: "100%", marginTop: 8, borderRadius: 6 }}
      />
      <div className="faint" style={{ fontSize: 11 }}>Grad-CAM: where the model looked. Not a tumor outline.</div>
    </>
  );
}

function rescanText(record: CaseRecord) {
  const d = [...record.decisions].reverse().find((x) => x.gate === "watch_or_treat");
  const months = d && d.gate === "watch_or_treat" ? d.decision.rescan_in_months : null;
  return months ? `Doctor chose watch. Rescan in ${months} months. No trial search.` : "Doctor chose watch. No trial search.";
}

function ResultsSummary({ record }: { record: CaseRecord }) {
  const r = record.results!;
  const rows: [string, string][] = [
    ["Diagnosis", r.diagnosis ?? "not recorded"],
    ["Grade", r.grade == null ? "n/a" : String(r.grade)],
    ["IDH", r.idh],
    ["1p/19q", r.codeletion_1p19q],
    ["MGMT", r.mgmt],
    ["Recurrent", r.recurrent ? (r.recurrence_number ? `yes (recurrence ${r.recurrence_number})` : "yes") : "no"],
    ["Age / ZIP", `${r.age} · ${r.zip}`],
    ["Sequencing", r.sequencing ?? (r.broad_sequencing_available ? "broad" : "none recorded")],
  ];
  return (
    <div className="box">
      <h4>Doctor-entered results</h4>
      <div className="kv" style={{ margin: 0 }}>
        {rows.map(([k, v]) => (
          <div key={k} style={{ display: "contents" }}>
            <div>{k}</div>
            <div>{v}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

const SKIP_TEXT = {
  no_broad_sequencing: "Skipped: no exome or genome sequencing.",
  not_recurrent_glioma: "Not run: only for recurrent glioma.",
};

export function MutationBox({ m }: { m: MutationCheckOutput }) {
  return (
    <div className="box">
      <h4>Mutation check (context only)</h4>
      {m.ran ? (
        <>
          <div>
            Temozolomide signature (SBS11): <b>{m.sbs11_call.replace("_", " ")}</b> · {m.total_mutations} mutations ·{" "}
            {Math.round(m.false_alarm_rate * 100)}% false-alarm rate
          </div>
          {m.summary && <div className="muted" style={{ marginTop: 4 }}>{m.summary}</div>}
          <div className="faint" style={{ marginTop: 4, fontSize: 12 }}>
            {m.note} A mutation count is not TMB and never routes a patient to immunotherapy trials.
          </div>
        </>
      ) : (
        <div>{SKIP_TEXT[m.skip_reason]}</div>
      )}
    </div>
  );
}
