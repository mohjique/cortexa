import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError, api } from "../api";
import ScanViewer from "./ScanViewer";

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
                ae.status === 409
                  ? `Case ${caseId} already exists. Choose another case ID, or open it from the care board.`
                  : ae.status === 413
                    ? "The image is larger than 20 MB. Upload a smaller PNG or JPEG."
                    : ae.status === 422
                      ? `The image couldn't be classified, so no case was created. ${ae.message}`
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
      <DemoScans />
    </div>
  );
}

/**
 * The 23 demo MRIs. Click one to view it full screen, then open it as a case
 * (it is classified like any upload). Only the case ID is shown: no label or
 * model call, so the doctor reads the scan without an answer in front of them.
 */
function DemoScans() {
  const nav = useNavigate();
  const [scans, setScans] = useState<string[] | null>(null);
  const [existing, setExisting] = useState<Set<string>>(new Set());
  const [open, setOpen] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.listDemoScans().then((s) => setScans(s.map((x) => x.case_id)), () => setScans([]));
    api.listCases().then((cs) => setExisting(new Set(cs.map((c) => c.case_id))), () => {});
  }, []);

  if (!scans || scans.length === 0) return null;

  const openCase = async (id: string) => {
    if (existing.has(id)) return nav(`/cases/${encodeURIComponent(id)}`);
    setBusy(true);
    setError(null);
    try {
      const s = await api.createCaseFromDemo(id);
      nav(`/cases/${encodeURIComponent(s.case_id)}`);
    } catch (err) {
      const ae = err as ApiError;
      if (ae.status === 409) return nav(`/cases/${encodeURIComponent(id)}`);
      setError(ae.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="box">
      <h2 style={{ margin: "0 0 4px", fontSize: 16 }}>Demo scans</h2>
      <div className="muted" style={{ fontSize: 13 }}>
        {scans.length} MRIs from the public dataset, one per synthetic demo patient. Click a scan to read it full screen,
        then open it as a case.
      </div>
      <div className="thumbs gallery">
        {scans.map((id) => (
          <button key={id} className="thumb" onClick={() => setOpen(id)} title={`View ${id}`}>
            <img src={api.demoScanUrl(id)} alt={`Demo scan ${id}`} loading="lazy" />
            <span>
              {id}
              {existing.has(id) && <i className="chip doc">case open</i>}
            </span>
          </button>
        ))}
      </div>
      {open && (
        <ScanViewer
          title={open}
          images={[{ key: "scan", label: "MRI scan", src: api.demoScanUrl(open) }]}
          onClose={() => (setOpen(null), setError(null))}
          footer={
            <>
              <button className="btn" disabled={busy} onClick={() => openCase(open)}>
                {existing.has(open) ? `Open case ${open}` : busy ? "Classifying…" : `Open as case ${open}`}
              </button>
              <span className="muted" style={{ fontSize: 12 }}>
                {existing.has(open)
                  ? "This case already exists."
                  : "Runs the classifier. The result is a suspected bucket for you to confirm or correct."}
              </span>
              {error && <span className="err-text">{error}</span>}
            </>
          }
        />
      )}
    </div>
  );
}
