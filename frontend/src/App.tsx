import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import logo from "./assets/logo.png";
import { useEffect, useState } from "react";
import { API_BASE, api } from "./api";
import { DoctorContext, useDoctorIdState } from "./hooks";
import Board from "./components/Board";
import CaseView from "./components/CaseView";
import Intake from "./components/Intake";
import { TutorialProvider, useTutorial } from "./tutorial/TutorialContext";
import Tour from "./tutorial/Tour";
import Welcome from "./tutorial/Welcome";

export default function App() {
  const [doctorId, setDoctorId] = useDoctorIdState();
  const misconfigured = import.meta.env.PROD && !import.meta.env.VITE_API_URL;

  return (
    <DoctorContext.Provider value={doctorId}>
      <TutorialProvider>
      <div className="app-top">
      <Notice />
      <header>
        <div className="logo">
          <img src={logo} alt="Cortexa brain mark" />
          <div className="wordmark">
            <div className="name">
              Corte<span>xa</span>
            </div>
            <div className="tagline">AGENTIC CARE MANAGEMENT</div>
          </div>
        </div>
        <nav className="tabs">
          <NavLink to="/" end>
            Care board
          </NavLink>
          <NavLink to="/cases">Pathway</NavLink>
          <NavLink to="/new">New case</NavLink>
        </nav>
        <div className="spacer" />
        <div className="legend" data-tour="legend">
          <span>
            <i className="sw" style={{ background: "var(--ai-bg)", border: "1px solid var(--ai-line)" }} />
            AI / agent step
          </span>
          <span>
            <i className="sw" style={{ background: "var(--teal-bg)", border: "1px solid var(--teal)" }} />
            Doctor decision
          </span>
        </div>
        <label className="doctor-id" data-tour="doctor-id">
          Doctor ID
          <input
            value={doctorId}
            onChange={(e) => setDoctorId(e.target.value)}
            placeholder="dr-synthetic-01"
            aria-label="Doctor ID recorded on every decision"
          />
        </label>
        <TourButton />
        <ResetButton />
      </header>
      </div>
      {misconfigured && (
        <div className="banner err">
          VITE_API_URL is not set for this deployment, so the app is calling <code>{API_BASE}</code>. Set it in the
          Vercel project settings and redeploy.
        </div>
      )}
      {!doctorId.trim() && (
        <div className="banner warn">Enter your doctor ID in the header. Every approval is recorded under it.</div>
      )}
      <main>
        <Routes>
          <Route path="/" element={<Board />} />
          <Route path="/cases" element={<CaseView />} />
          <Route path="/cases/:caseId" element={<CaseView />} />
          <Route path="/new" element={<Intake />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
      <Tour />
      <Welcome />
      </TutorialProvider>
    </DoctorContext.Provider>
  );
}

/** Clear this browser's demo workspace (its cases and audit log only) and start fresh. */
function ResetButton() {
  const [busy, setBusy] = useState(false);
  const reset = async () => {
    const ok = window.confirm(
      "Reset the demo? This deletes every case and audit entry in this browser's workspace and starts fresh. " +
        "Other visitors' workspaces are not affected.",
    );
    if (!ok) return;
    setBusy(true);
    try {
      await api.resetWorkspace();
      window.location.assign(import.meta.env.BASE_URL || "/"); // reload: drop every cached case view
    } catch (e) {
      window.alert(`Could not reset the demo: ${(e as Error).message}`);
      setBusy(false);
    }
  };
  return (
    <button className="btn ghost small" onClick={reset} disabled={busy} title="Delete this browser's demo cases and start fresh">
      Reset demo
    </button>
  );
}

function TourButton() {
  const { start, active } = useTutorial();
  return (
    <button className="btn ghost small" data-tour="tour-button" onClick={start} disabled={active} title="Replay the guided tour">
      Tour
    </button>
  );
}

/** What is synthetic and what is real, from the backend's trial source. */
function Notice() {
  const [source, setSource] = useState<"demo" | "live" | null>(null);
  useEffect(() => {
    api.health().then((h) => setSource(h.trial_source ?? null), () => setSource(null));
  }, []);
  const data =
    source === "live"
      ? "Synthetic patients and images; trials are real records from ClinicalTrials.gov."
      : source === "demo"
        ? "Synthetic patients, images and trials."
        : "Synthetic patients and images.";
  return (
    <div className="notice" role="note">
      <strong>Research and demo use only.</strong> {data} Not a medical device. Model output is suspected, never a
      diagnosis; a doctor approves every step.
    </div>
  );
}
