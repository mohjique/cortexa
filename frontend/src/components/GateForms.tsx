// Doctor approval gates. Each form sends one decision record to the backend,
// which checks the gate against the stage, audits it and moves the case on.
import { useState } from "react";
import { api } from "../api";
import { useDoctorId } from "../hooks";
import { BUCKET_LABEL } from "../pathway";
import { BUCKETS, type Bucket, type CaseRecord, type GateDecision } from "../types";

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
              setError((e as Error).message);
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
