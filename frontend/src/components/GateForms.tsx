// Doctor approval gates. Each form sends one decision record to the backend,
// which checks the gate against the stage, audits it and moves the case on.
import { useState } from "react";
import { ApiError, api } from "../api";
import { useDoctorId } from "../hooks";
import { BUCKET_LABEL } from "../pathway";
import { BUCKETS, type Bucket, type CaseRecord, type GateDecision, type PreSurgeryAction } from "../types";
import { RecruitChip, siteText } from "./common";

export function useSubmit(caseId: string, onDone: () => Promise<void>) {
  const doctorId = useDoctorId();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (d: GateDecision, note?: string) => {
    if (!doctorId) {
      setError("Enter your doctor ID in the header first. Every decision is recorded under it.");
      return false;
    }
    setBusy(true);
    setError(null);
    try {
      await api.submitDecision(caseId, doctorId, d, note);
      await onDone();
      return true;
    } catch (e) {
      setError((e as Error).message);
      return false;
    } finally {
      setBusy(false);
    }
  };
  return { submit, busy, error, doctorId };
}

function Note({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <label className="field">
      <span>Note (optional, no patient identifiers)</span>
      <textarea value={value} onChange={(e) => onChange(e.target.value)} />
    </label>
  );
}

function Submit({ busy, disabled, error, label = "Record decision" }: { busy: boolean; disabled?: boolean; error: string | null; label?: string }) {
  return (
    <>
      <div className="row-actions">
        <button className="btn" type="submit" disabled={busy || disabled}>
          {busy ? "Recording…" : label}
        </button>
      </div>
      {error && <div className="err-text">{error}</div>}
    </>
  );
}

type FormProps = { record: CaseRecord; onDone: () => Promise<void> };

export function ConfirmBucketForm({ record, onDone }: FormProps) {
  const { submit, busy, error } = useSubmit(record.state.case_id, onDone);
  // Never pre-select: the doctor picks a bucket explicitly, even when it matches the model.
  const [bucket, setBucket] = useState<Bucket | null>(null);
  const [note, setNote] = useState("");
  const suspected = record.state.suspected_bucket;

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (bucket) submit({ gate: "confirm_bucket", decision: { bucket, overrode_model: bucket !== suspected } }, note);
      }}
    >
      {record.classifier?.flagged_uncertain && (
        <div className="err-text">The model is unsure about this scan. Review the images closely before confirming.</div>
      )}
      <div style={{ marginTop: 6 }}>
        {BUCKETS.map((b) => (
          <label key={b} className="radio">
            <input type="radio" name="bucket" checked={bucket === b} onChange={() => setBucket(b)} />
            {BUCKET_LABEL[b]}
            {b === suspected && <span className="faint">(model's suspected bucket)</span>}
          </label>
        ))}
      </div>
      {bucket && suspected && bucket !== suspected && (
        <div className="faint" style={{ fontSize: 12, marginTop: 4 }}>
          You are overriding the model ({BUCKET_LABEL[suspected]} → {BUCKET_LABEL[bucket]}).
        </div>
      )}
      <Note value={note} onChange={setNote} />
      <Submit busy={busy} disabled={!bucket} error={error} label="Confirm bucket" />
    </form>
  );
}

export function WatchOrTreatForm({ record, onDone }: FormProps) {
  const { submit, busy, error } = useSubmit(record.state.case_id, onDone);
  const [choice, setChoice] = useState<"watch" | "treat" | null>(null);
  const [months, setMonths] = useState(6);
  const [note, setNote] = useState("");
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (!choice) return;
        submit(
          { gate: "watch_or_treat", decision: choice === "watch" ? { choice, rescan_in_months: months } : { choice } },
          note,
        );
      }}
    >
      <label className="radio">
        <input type="radio" name="wot" checked={choice === "watch"} onChange={() => setChoice("watch")} /> Watch: rescan later,
        no trial search
      </label>
      <label className="radio">
        <input type="radio" name="wot" checked={choice === "treat"} onChange={() => setChoice("treat")} /> Treat: surgery and
        pathology
      </label>
      {choice === "watch" && (
        <label className="field">
          <span>Rescan in (months)</span>
          <input type="number" min={1} max={60} value={months} onChange={(e) => setMonths(Number(e.target.value))} />
        </label>
      )}
      <Note value={note} onChange={setNote} />
      <Submit busy={busy} disabled={!choice || (choice === "watch" && !(months >= 1 && months <= 60))} error={error} />
    </form>
  );
}

type Ack = { action: PreSurgeryAction | null; reason: string };

/**
 * preop_review (glioma). The doctor confirms they reviewed the tests the biopsy
 * must answer and answers every enroll-before-surgery trial. The decision carries
 * the screen_id the doctor saw; if the screen changed since, the backend rejects
 * it (422) and the form reloads with the current screen.
 * Mount with key={screen_id} so choices reset when the screen changes.
 */
export function PreopReviewForm({ record, onDone }: FormProps) {
  const { submit, busy, error } = useSubmit(record.state.case_id, onDone);
  const preop = record.preop!;
  const [tests, setTests] = useState(false);
  const [acks, setAcks] = useState<Record<string, Ack>>({});
  const [note, setNote] = useState("");
  const get = (id: string): Ack => acks[id] ?? { action: null, reason: "" };
  const setA = (id: string, a: Partial<Ack>) => setAcks((x) => ({ ...x, [id]: { ...get(id), ...a } }));
  const open = preop.pre_surgery_trials.filter((t) => {
    const a = get(t.trial_id);
    return a.action === null || (a.action === "not_pursuing" && !a.reason.trim());
  });

  // Rescreen sits outside the <form>, so Enter in its reason box can't submit the review.
  return (
    <>
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          if (!tests || open.length) return;
          const ok = await submit(
            {
              gate: "preop_review",
              decision: {
                screen_id: preop.screen_id,
                tests_reviewed: true,
                pre_surgery_trials: preop.pre_surgery_trials.map((t) => {
                  const a = get(t.trial_id);
                  return { trial_id: t.trial_id, action: a.action!, reason: a.reason.trim() || null };
                }),
              },
            },
            note,
          );
          if (!ok) await onDone(); // a stale screen_id is rejected: load the current screen
        }}
      >
        <label className="radio">
          <input type="checkbox" checked={tests} onChange={(e) => setTests(e.target.checked)} /> I have reviewed what the biopsy
          must answer ({preop.tests_needed.length ? preop.tests_needed.map((t) => t.label).join(", ") : "no tests listed"})
        </label>
        {preop.pre_surgery_trials.length > 0 ? (
          <div style={{ marginTop: 8 }}>
            <div className="muted" style={{ fontSize: 12 }}>
              Enroll-before-surgery trials. Answer each one: once surgery happens the patient can no longer join.
            </div>
            {preop.pre_surgery_trials.map((t) => {
              const a = get(t.trial_id);
              return (
                <div className="ptrial" key={t.trial_id}>
                  <div>
                    <b>{t.trial_id}</b> <RecruitChip status={t.recruitment_status} />
                    {t.matched_on === "title_hint" && <span className="chip warn">Possible: check criteria</span>}
                  </div>
                  <div className="ptitle">{t.title}</div>
                  <div className="demo">Nearest site: {siteText(t.nearest_site)}</div>
                  <label className="radio">
                    <input
                      type="radio"
                      name={`pre-${t.trial_id}`}
                      checked={a.action === "pursue_before_surgery"}
                      onChange={() => setA(t.trial_id, { action: "pursue_before_surgery" })}
                    />{" "}
                    Pursue before surgery
                  </label>
                  <label className="radio">
                    <input
                      type="radio"
                      name={`pre-${t.trial_id}`}
                      checked={a.action === "not_pursuing"}
                      onChange={() => setA(t.trial_id, { action: "not_pursuing" })}
                    />{" "}
                    Not pursuing
                  </label>
                  {a.action !== null && (
                    <input
                      style={{ width: "100%", marginTop: 4 }}
                      placeholder={a.action === "not_pursuing" ? "Reason (required)" : "Reason (optional)"}
                      value={a.reason}
                      onChange={(e) => setA(t.trial_id, { reason: e.target.value })}
                    />
                  )}
                </div>
              );
            })}
          </div>
        ) : (
          <div className="faint" style={{ marginTop: 6, fontSize: 12 }}>
            No enroll-before-surgery trials on this screen.
          </div>
        )}
        <Note value={note} onChange={setNote} />
        <Submit busy={busy} disabled={!tests || open.length > 0} error={error} label="Record pre-op review" />
        {open.length > 0 && <div className="faint" style={{ fontSize: 12 }}>{open.length} trial(s) still need an answer</div>}
      </form>
      <RescreenButton caseId={record.state.case_id} onDone={onDone} />
    </>
  );
}

/** Ask the agent for a fresh pre-op screen. Only valid while the case waits on preop_review. */
function RescreenButton({ caseId, onDone }: { caseId: string; onDone: () => Promise<void> }) {
  const doctorId = useDoctorId();
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <div style={{ marginTop: 10 }}>
      <div className="faint" style={{ fontSize: 12 }}>
        Screen out of date? Ask the agent to run it again. The current screen is dropped and you review the new one.
      </div>
      <div className="row-actions">
        <input style={{ flex: 1, minWidth: 140 }} placeholder="Reason (optional)" value={reason} onChange={(e) => setReason(e.target.value)} />
        <button
          type="button"
          className="btn ghost small"
          disabled={busy}
          onClick={async () => {
            if (!doctorId) {
              setError("Enter your doctor ID in the header first. Every decision is recorded under it.");
              return;
            }
            setBusy(true);
            setError(null);
            try {
              await api.rescreenPreop(caseId, doctorId, reason);
              setReason("");
            } catch (e) {
              const ae = e as ApiError;
              setError(ae.status === 409 ? "This case is no longer waiting on the pre-op review." : ae.message);
            } finally {
              await onDone();
              setBusy(false);
            }
          }}
        >
          {busy ? "Rescreening…" : "Rescreen"}
        </button>
      </div>
      {error && <div className="err-text">{error}</div>}
    </div>
  );
}

export function HormoneLabsForm({ record, onDone }: FormProps) {
  const { submit, busy, error } = useSubmit(record.state.case_id, onDone);
  const [ok, setOk] = useState(false);
  const [note, setNote] = useState("");
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (ok) submit({ gate: "hormone_labs", decision: { labs_reviewed: true } }, note);
      }}
    >
      <label className="radio">
        <input type="checkbox" checked={ok} onChange={(e) => setOk(e.target.checked)} /> I have reviewed the hormone labs and
        endocrinology workup
      </label>
      <Note value={note} onChange={setNote} />
      <Submit busy={busy} disabled={!ok} error={error} label="Mark labs reviewed" />
    </form>
  );
}

export function SurgeryOrMedsForm({ record, onDone }: FormProps) {
  const { submit, busy, error } = useSubmit(record.state.case_id, onDone);
  const [choice, setChoice] = useState<"surgery" | "medication_only" | null>(null);
  const [note, setNote] = useState("");
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (choice) submit({ gate: "surgery_or_meds", decision: { choice } }, note);
      }}
    >
      <label className="radio">
        <input type="radio" name="sm" checked={choice === "surgery"} onChange={() => setChoice("surgery")} /> Surgery, then
        pathology
      </label>
      <label className="radio">
        <input type="radio" name="sm" checked={choice === "medication_only"} onChange={() => setChoice("medication_only")} />{" "}
        Medication only (no pathology; enter results directly)
      </label>
      <Note value={note} onChange={setNote} />
      <Submit busy={busy} disabled={!choice} error={error} />
    </form>
  );
}

export function AgentRetry({ caseId, onDone }: { caseId: string; onDone: () => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <>
      <div className="row-actions">
        <button
          className="btn ghost small"
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            setError(null);
            try {
              await api.runAgent(caseId);
              await onDone();
            } catch (e) {
              const ae = e as ApiError;
              setError(
                ae.status === 503
                  ? "The agent step failed again. The case is unchanged; try again in a moment (details in the audit log)."
                  : ae.message,
              );
            } finally {
              setBusy(false);
            }
          }}
        >
          {busy ? "Retrying…" : "Retry agent step"}
        </button>
      </div>
      {error && <div className="err-text">{error}</div>}
    </>
  );
}
