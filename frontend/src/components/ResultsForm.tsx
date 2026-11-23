// enter_results gate: the doctor types the pathology / lab results.
// Nothing is defaulted to a clinical value. Every marker starts blank and must
// be chosen, including "pending" (ordered, not back) or "unknown" (not tested).
import { useState } from "react";
import { BUCKET_LABEL } from "../pathway";
import type { CaseRecord, DoctorResults, Grade, PituitaryResults, PriorTreatment, Sex, TreatmentType } from "../types";
import { useSubmit } from "./GateForms";

// Synthetic demo records (clinical/patients/demo-*.json), only their doctor_decisions block.
const DEMO = import.meta.glob("../../../clinical/patients/demo-*.json", { eager: true, import: "doctor_decisions" }) as Record<
  string,
  { enter_results?: DoctorResults } | undefined
>;
function demoResults(caseId: string): DoctorResults | null {
  const hit = Object.entries(DEMO).find(([p]) => p.endsWith(`/${caseId}.json`));
  return hit?.[1]?.enter_results ?? null;
}

type Tri = "" | "yes" | "no";
interface TRow {
  type: TreatmentType;
  weeks_ago: string;
  detail: string;
  extent: string;
  cycles: string;
}
interface F {
  diagnosis: string;
  grade: string;
  idh: string;
  codeletion_1p19q: string;
  mgmt: string;
  tmb_clia: string;
  recurrent: Tri;
  recurrence_number: string;
  treatments: TRow[];
  comorbidities: string;
  comorbNone: boolean;
  phone: Tri;
  age: string;
  sex: "" | Sex; // "" = not recorded
  zip: string;
  sequencing: string;
  pit: { secreting_hormone: string; size: string; cavernous_sinus_invasion: string; surgical: Tri; levels: { k: string; v: string }[] };
}

const blank = (): F => ({
  diagnosis: "",
  grade: "",
  idh: "",
  codeletion_1p19q: "",
  mgmt: "",
  tmb_clia: "",
  recurrent: "",
  recurrence_number: "",
  treatments: [],
  comorbidities: "",
  comorbNone: false,
  phone: "",
  age: "",
  sex: "",
  zip: "",
  sequencing: "",
  pit: { secreting_hormone: "", size: "", cavernous_sinus_invasion: "", surgical: "", levels: [] },
});

const tri = (b: boolean | null | undefined): Tri => (b == null ? "" : b ? "yes" : "no");
const s = (v: unknown) => (v == null ? "" : String(v));

function fromResults(r: DoctorResults): F {
  return {
    diagnosis: s(r.diagnosis),
    grade: r.grade == null ? "" : String(r.grade),
    idh: r.idh,
    codeletion_1p19q: r.codeletion_1p19q,
    mgmt: r.mgmt,
    tmb_clia: s(r.tmb_clia),
    recurrent: tri(r.recurrent),
    recurrence_number: s(r.recurrence_number),
    treatments: (r.prior_treatments ?? []).map((t) => ({
      type: t.type,
      weeks_ago: s(t.weeks_ago),
      detail: s(t.detail),
      extent: s(t.extent),
      cycles: s(t.cycles),
    })),
    comorbidities: (r.comorbidities ?? []).join(", "),
    comorbNone: Array.isArray(r.comorbidities) && r.comorbidities.length === 0,
    phone: tri(r.can_use_phone_or_tablet),
    age: s(r.age),
    sex: r.sex ?? "",
    zip: r.zip,
    sequencing: r.sequencing ?? (r.broad_sequencing_available ? "" : "none"),
    pit: {
      secreting_hormone: s(r.pituitary?.secreting_hormone),
      size: s(r.pituitary?.size),
      cavernous_sinus_invasion: s(r.pituitary?.cavernous_sinus_invasion),
      surgical: tri(r.pituitary?.surgical_candidate),
      levels: Object.entries(r.pituitary?.hormone_levels ?? {}).map(([k, v]) => ({ k, v: String(v) })),
    },
  };
}

const TREATMENTS: TreatmentType[] = [
  "surgery",
  "radiation",
  "temozolomide",
  "bevacizumab",
  "idh_inhibitor",
  "checkpoint_inhibitor",
  "pituitary_radiation",
  "other",
];

function Sel({ label, value, onChange, options, disabled, hint }: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: [string, string][];
  disabled?: boolean;
  hint?: string;
}) {
  return (
    <label className="field">
      <span>{label}</span>
      <select value={value} onChange={(e) => onChange(e.target.value)} disabled={disabled}>
        <option value="">— select —</option>
        {options.map(([v, l]) => (
          <option key={v} value={v}>
            {l}
          </option>
        ))}
      </select>
      {hint && <small>{hint}</small>}
    </label>
  );
}

const MARKER = (a: string, b: string): [string, string][] => [
  [a, a],
  [b, b],
  ["pending", "pending (ordered, not back)"],
  ["unknown", "unknown (not tested)"],
];

export default function ResultsForm({ record, onDone }: { record: CaseRecord; onDone: () => Promise<void> }) {
  const st = record.state;
  const bucket = st.confirmed_bucket!;
  const isPit = bucket === "pituitary";
  const medsOnly = isPit && st.treatment_path === "medication_only";
  const { submit, busy, error } = useSubmit(st.case_id, onDone);
  const [f, setF] = useState<F>(() => {
    const b = blank();
    return medsOnly ? { ...b, idh: "unknown", codeletion_1p19q: "unknown", mgmt: "unknown" } : b;
  });
  const [note, setNote] = useState("");
  const [localErr, setLocalErr] = useState<string | null>(null);
  const demo = demoResults(st.case_id);
  const set = <K extends keyof F>(k: K, v: F[K]) => setF((x) => ({ ...x, [k]: v }));
  const setPit = (k: keyof F["pit"], v: string) => setF((x) => ({ ...x, pit: { ...x.pit, [k]: v } }));

  const build = (): DoctorResults | string => {
    const missing: string[] = [];
    if (!isPit && !f.grade) missing.push("grade");
    if (!f.idh) missing.push("IDH");
    if (!f.codeletion_1p19q) missing.push("1p/19q");
    if (!f.mgmt) missing.push("MGMT");
    if (!f.recurrent) missing.push("recurrent");
    if (!f.age) missing.push("age");
    if (!f.sequencing) missing.push("sequencing");
    if (isPit && (!f.pit.secreting_hormone || !f.pit.size || !f.pit.cavernous_sinus_invasion)) missing.push("pituitary fields");
    if (missing.length) return `Still needed: ${missing.join(", ")}. Choose "unknown" or "pending" if there is no result.`;
    if (!/^\d{5}$/.test(f.zip)) return "ZIP must be 5 digits. Only the ZIP is used for distance, never an address.";
    const age = Number(f.age);
    if (!Number.isInteger(age) || age < 0 || age > 120) return "Age must be a whole number from 0 to 120.";

    const treatments: PriorTreatment[] = f.treatments.map((t) => ({
      type: t.type,
      ...(t.detail.trim() && { detail: t.detail.trim() }),
      ...(t.weeks_ago !== "" && { weeks_ago: Number(t.weeks_ago) }),
      ...(t.type === "surgery" && t.extent && { extent: t.extent as PriorTreatment["extent"] }),
      ...(t.type === "temozolomide" && t.cycles !== "" && { cycles: Number(t.cycles) }),
    }));

    const levels: Record<string, number> = {};
    for (const { k, v } of f.pit.levels) if (k.trim() && v !== "") levels[k.trim()] = Number(v);

    const pituitary: PituitaryResults | null = isPit
      ? {
          treatment_path: st.treatment_path === "medication_only" ? "medication_only" : "surgery",
          secreting_hormone: f.pit.secreting_hormone as PituitaryResults["secreting_hormone"],
          size: f.pit.size as PituitaryResults["size"],
          cavernous_sinus_invasion: f.pit.cavernous_sinus_invasion as PituitaryResults["cavernous_sinus_invasion"],
          surgical_candidate: f.pit.surgical === "" ? null : f.pit.surgical === "yes",
          hormone_levels: levels,
        }
      : null;

    const recurrent = f.recurrent === "yes";
    return {
      case_id: st.case_id,
      confirmed_bucket: bucket,
      diagnosis: f.diagnosis.trim() || null,
      grade: isPit ? null : ((/^\d$/.test(f.grade) ? Number(f.grade) : f.grade) as Grade),
      idh: f.idh as DoctorResults["idh"],
      codeletion_1p19q: f.codeletion_1p19q as DoctorResults["codeletion_1p19q"],
      mgmt: f.mgmt as DoctorResults["mgmt"],
      tmb_clia: f.tmb_clia === "" ? null : Number(f.tmb_clia),
      recurrent,
      recurrence_number: f.recurrence_number === "" ? (recurrent ? null : 0) : Number(f.recurrence_number),
      prior_treatments: treatments,
      comorbidities: f.comorbNone
        ? []
        : f.comorbidities.trim()
          ? f.comorbidities.split(",").map((x) => x.trim()).filter(Boolean)
          : null,
      can_use_phone_or_tablet: f.phone === "" ? null : f.phone === "yes",
      age,
      sex: f.sex === "" ? null : f.sex,
      zip: f.zip,
      broad_sequencing_available: f.sequencing === "exome" || f.sequencing === "genome",
      sequencing: f.sequencing as DoctorResults["sequencing"],
      pituitary,
    };
  };

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        const r = build();
        if (typeof r === "string") return setLocalErr(r);
        setLocalErr(null);
        submit({ gate: "enter_results", decision: r }, note);
      }}
    >
      <div className="box gate" style={{ marginTop: 0 }}>
        <h4>Enter results · {BUCKET_LABEL[bucket]}</h4>
        {medsOnly
          ? "Medication-only pituitary case: no pathology, so glioma markers are fixed to unknown."
          : "Enter what the pathology report says. Leave nothing to guesswork: choose pending or unknown when there is no result."}
        {demo && (
          <div className="row-actions">
            <button type="button" className="btn ghost small" onClick={() => setF(fromResults(demo))}>
              Fill from synthetic demo record
            </button>
            <span className="faint" style={{ fontSize: 12 }}>
              Review every field before recording.
            </span>
          </div>
        )}
      </div>

      <fieldset>
        <legend>Pathology</legend>
        <label className="field">
          <span>Diagnosis (as written on the report)</span>
          <input value={f.diagnosis} onChange={(e) => set("diagnosis", e.target.value)} placeholder="e.g. Glioblastoma, IDH-wildtype" />
        </label>
        <div className="grid2">
          {!isPit && (
            <Sel
              label="WHO grade"
              value={f.grade}
              onChange={(v) => set("grade", v)}
              options={[["1", "1"], ["2", "2"], ["3", "3"], ["4", "4"], ["pending", "pending"], ["unknown", "unknown"]]}
            />
          )}
          <Sel label="IDH" value={f.idh} onChange={(v) => set("idh", v)} options={MARKER("mutant", "wildtype")} disabled={medsOnly} />
          <Sel
            label="1p/19q"
            value={f.codeletion_1p19q}
            onChange={(v) => set("codeletion_1p19q", v)}
            options={MARKER("codeleted", "intact")}
            disabled={medsOnly}
          />
          <Sel label="MGMT promoter" value={f.mgmt} onChange={(v) => set("mgmt", v)} options={MARKER("methylated", "unmethylated")} disabled={medsOnly} />
          <label className="field">
            <span>TMB, CLIA lab (mut/Mb)</span>
            <input type="number" min={0} step="any" value={f.tmb_clia} onChange={(e) => set("tmb_clia", e.target.value)} placeholder="not recorded" />
            <small>CLIA lab result only. Never from the mutation check.</small>
          </label>
          <Sel
            label="Sequencing available"
            value={f.sequencing}
            onChange={(v) => set("sequencing", v)}
            options={[["exome", "exome"], ["genome", "genome"], ["panel", "gene panel"], ["none", "none"]]}
            hint="Exome or genome enables the mutation check for recurrent glioma."
          />
        </div>
      </fieldset>

      {isPit && (
        <fieldset>
          <legend>Pituitary</legend>
          <div className="grid2">
            <Sel
              label="Secreting hormone"
              value={f.pit.secreting_hormone}
              onChange={(v) => setPit("secreting_hormone", v)}
              options={["prolactin", "growth_hormone", "acth", "tsh", "none", "pending", "unknown"].map((x) => [x, x.replace("_", " ")])}
            />
            <Sel label="Size" value={f.pit.size} onChange={(v) => setPit("size", v)} options={[["micro", "micro"], ["macro", "macro"], ["unknown", "unknown"]]} />
            <Sel
              label="Cavernous sinus invasion"
              value={f.pit.cavernous_sinus_invasion}
              onChange={(v) => setPit("cavernous_sinus_invasion", v)}
              options={[["yes", "yes"], ["no", "no"], ["unknown", "unknown"]]}
            />
            <Sel
              label="Surgical candidate"
              value={f.pit.surgical}
              onChange={(v) => setPit("surgical", v)}
              options={[["yes", "yes"], ["no", "no"]]}
              hint="Blank = not assessed"
            />
          </div>
          <div className="field">
            <span>Hormone levels (key as hormone_unit, e.g. acth_pg_ml)</span>
            {f.pit.levels.map((l, i) => (
              <div key={i} className="row-actions" style={{ marginTop: 4 }}>
                <input
                  value={l.k}
                  placeholder="acth_pg_ml"
                  onChange={(e) => setF((x) => ({ ...x, pit: { ...x.pit, levels: x.pit.levels.map((y, j) => (j === i ? { ...y, k: e.target.value } : y)) } }))}
                />
                <input
                  type="number"
                  step="any"
                  value={l.v}
                  onChange={(e) => setF((x) => ({ ...x, pit: { ...x.pit, levels: x.pit.levels.map((y, j) => (j === i ? { ...y, v: e.target.value } : y)) } }))}
                />
                <button type="button" className="btn ghost small" onClick={() => setF((x) => ({ ...x, pit: { ...x.pit, levels: x.pit.levels.filter((_, j) => j !== i) } }))}>
                  Remove
                </button>
              </div>
            ))}
            <div>
              <button type="button" className="btn ghost small" style={{ marginTop: 4 }} onClick={() => setF((x) => ({ ...x, pit: { ...x.pit, levels: [...x.pit.levels, { k: "", v: "" }] } }))}>
                Add hormone level
              </button>
            </div>
          </div>
        </fieldset>
      )}

      <fieldset>
        <legend>History</legend>
        <div className="grid2">
          <Sel label="Recurrent tumor" value={f.recurrent} onChange={(v) => set("recurrent", v as Tri)} options={[["no", "no (new diagnosis)"], ["yes", "yes"]]} />
          {f.recurrent === "yes" && (
            <label className="field">
              <span>Recurrence number</span>
              <input type="number" min={1} value={f.recurrence_number} onChange={(e) => set("recurrence_number", e.target.value)} placeholder="not recorded" />
            </label>
          )}
        </div>
        <div className="field">
          <span>Prior treatments</span>
          {f.treatments.map((t, i) => {
            const upd = (p: Partial<TRow>) => setF((x) => ({ ...x, treatments: x.treatments.map((y, j) => (j === i ? { ...y, ...p } : y)) }));
            return (
              <div key={i} className="row-actions" style={{ marginTop: 4 }}>
                <select value={t.type} onChange={(e) => upd({ type: e.target.value as TreatmentType, extent: "", cycles: "" })}>
                  {TREATMENTS.map((x) => (
                    <option key={x} value={x}>
                      {x.replace(/_/g, " ")}
                    </option>
                  ))}
                </select>
                <input type="number" min={0} style={{ width: 90 }} placeholder="weeks ago" value={t.weeks_ago} onChange={(e) => upd({ weeks_ago: e.target.value })} />
                {t.type === "surgery" && (
                  <select value={t.extent} onChange={(e) => upd({ extent: e.target.value })}>
                    <option value="">extent?</option>
                    <option value="gross_total">gross total</option>
                    <option value="subtotal">subtotal</option>
                    <option value="biopsy_only">biopsy only</option>
                  </select>
                )}
                {t.type === "temozolomide" && (
                  <input type="number" min={0} style={{ width: 80 }} placeholder="cycles" value={t.cycles} onChange={(e) => upd({ cycles: e.target.value })} />
                )}
                <input style={{ flex: 1, minWidth: 120 }} placeholder="detail" value={t.detail} onChange={(e) => upd({ detail: e.target.value })} />
                <button type="button" className="btn ghost small" onClick={() => setF((x) => ({ ...x, treatments: x.treatments.filter((_, j) => j !== i) }))}>
                  Remove
                </button>
              </div>
            );
          })}
          <div>
            <button
              type="button"
              className="btn ghost small"
              style={{ marginTop: 4 }}
              onClick={() => setF((x) => ({ ...x, treatments: [...x.treatments, { type: "surgery", weeks_ago: "", detail: "", extent: "", cycles: "" }] }))}
            >
              Add treatment
            </button>
          </div>
        </div>
        <label className="field">
          <span>Comorbidities (comma-separated)</span>
          <input
            value={f.comorbidities}
            disabled={f.comorbNone}
            onChange={(e) => set("comorbidities", e.target.value)}
            placeholder="blank = not recorded"
          />
        </label>
        <label className="radio">
          <input type="checkbox" checked={f.comorbNone} onChange={(e) => set("comorbNone", e.target.checked)} /> No comorbidities
        </label>
      </fieldset>

      <fieldset>
        <legend>Patient (synthetic)</legend>
        <div className="grid2">
          <label className="field">
            <span>Age</span>
            <input type="number" min={0} max={120} value={f.age} onChange={(e) => set("age", e.target.value)} />
          </label>
          <label className="field">
            <span>Sex</span>
            <select value={f.sex} onChange={(e) => set("sex", e.target.value as F["sex"])}>
              <option value="">not recorded</option>
              <option value="female">female</option>
              <option value="male">male</option>
            </select>
            <small>Checked only against a trial&apos;s registry sex eligibility. Not recorded = unknown, never guessed.</small>
          </label>
          <label className="field">
            <span>ZIP code</span>
            <input inputMode="numeric" maxLength={5} value={f.zip} onChange={(e) => set("zip", e.target.value.replace(/\D/g, ""))} />
            <small>ZIP only, never a street address.</small>
          </label>
          <Sel
            label="Can use phone or tablet"
            value={f.phone}
            onChange={(v) => set("phone", v as Tri)}
            options={[["yes", "yes"], ["no", "no"]]}
            hint="Blank = not recorded"
          />
        </div>
      </fieldset>

      <label className="field">
        <span>Note (optional, no patient identifiers)</span>
        <textarea value={note} onChange={(e) => setNote(e.target.value)} />
      </label>
      <div className="row-actions">
        <button className="btn" type="submit" disabled={busy}>
          {busy ? "Recording…" : "Record results"}
        </button>
      </div>
      {(localErr || error) && <div className="err-text">{localErr ?? error}</div>}
    </form>
  );
}
