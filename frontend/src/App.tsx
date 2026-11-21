import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import logo from "./assets/logo.png";
import { API_BASE } from "./api";
import { DoctorContext, useDoctorIdState } from "./hooks";
import Board from "./components/Board";
import CaseView from "./components/CaseView";
import Intake from "./components/Intake";

export default function App() {
  const [doctorId, setDoctorId] = useDoctorIdState();
  const misconfigured = import.meta.env.PROD && !import.meta.env.VITE_API_URL;

  return (
    <DoctorContext.Provider value={doctorId}>
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
        <span className="tag">Synthetic demo data · not for clinical use</span>
        <nav className="tabs">
          <NavLink to="/" end>
            Care board
          </NavLink>
          <NavLink to="/cases">Pathway</NavLink>
          <NavLink to="/new">New case</NavLink>
        </nav>
        <div className="spacer" />
        <div className="legend">
          <span>
            <i className="sw" style={{ background: "var(--ai-bg)", border: "1px solid var(--ai-line)" }} />
            AI / agent step
          </span>
          <span>
            <i className="sw" style={{ background: "var(--teal-bg)", border: "1px solid var(--teal)" }} />
            Doctor decision
          </span>
        </div>
        <label className="doctor-id">
          Doctor ID
          <input
            value={doctorId}
            onChange={(e) => setDoctorId(e.target.value)}
            placeholder="dr-synthetic-01"
            aria-label="Doctor ID recorded on every decision"
          />
        </label>
      </header>
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
    </DoctorContext.Provider>
  );
}
