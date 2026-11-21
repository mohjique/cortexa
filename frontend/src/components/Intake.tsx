import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError, api } from "../api";

const CASE_ID = /^[A-Za-z0-9_-]{1,64}$/; // same rule as agent/store.py

export default function Intake() {
  const nav = useNavigate();
  const [caseId, setCaseId] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [drag, setDrag] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const valid = CASE_ID.test(caseId) && !!file;

  return (
    <div className="page">
      <div className="box">
        <h2 style={{ margin: "0 0 4px" }}>New case: upload MRI</h2>
        <div className="muted" style={{ fontSize: 13 }}>
          The classifier returns a <b>suspected</b> bucket and a confidence score. A doctor confirms or corrects every case
          before anything else happens. Synthetic or public dataset images only. No real patient data.
        </div>
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            if (!valid) return;
            setBusy(true);
            setError(null);
            try {
              const s = await api.createCase(caseId, file!);
              nav(`/cases/${encodeURIComponent(s.case_id)}`);
            } catch (err) {
              const ae = err as ApiError;
              setError(
                ae.status === 404 || ae.status === 405
                  ? "The backend doesn't have an MRI intake endpoint yet (POST /cases). Cases created by the agent still show up on the care board."
                  : ae.message,
              );
            } finally {
              setBusy(false);
            }
          }}
        >
          <label className="field">
            <span>Case ID</span>
            <input value={caseId} onChange={(e) => setCaseId(e.target.value.trim())} placeholder="demo-024" />
            <small>Letters, digits, - or _. Never a patient name or MRN.</small>
          </label>
          <div
            className={`drop ${drag ? "on" : ""}`}
            onDragOver={(e) => (e.preventDefault(), setDrag(true))}
            onDragLeave={() => setDrag(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDrag(false);
              const f = e.dataTransfer.files[0];
              if (f) setFile(f);
            }}
            onClick={() => document.getElementById("mri-file")?.click()}
          >
            {file ? (
              <>
                <b>{file.name}</b> · {(file.size / 1024).toFixed(0)} KB
              </>
            ) : (
              "Drop an MRI image here, or click to choose (PNG or JPEG)"
            )}
            <input
              id="mri-file"
              type="file"
              accept="image/png,image/jpeg"
              hidden
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
          </div>
          <div className="row-actions">
            <button className="btn" type="submit" disabled={!valid || busy}>
              {busy ? "Uploading…" : "Upload and classify"}
            </button>
          </div>
          {error && <div className="err-text">{error}</div>}
        </form>
      </div>
    </div>
  );
}
