import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useDoctorId } from "../hooks";
import { useTutorial } from "./TutorialContext";
import type { TourStep } from "./steps";

/**
 * Spotlight overlay for the guided tour. For each step it opens the right page
 * (and case tab), finds the element marked data-tour="<target>", dims
 * everything else and places a caption next to it. If the target never shows
 * up, the step is skipped in the direction the user was moving.
 *
 * The dimmed area blocks clicks, so the tour can't press anything on the page.
 * Only the doctor ID step leaves its field usable.
 */

const PAD = 6; // space between the element and the spotlight edge
const GAP = 12; // space between the spotlight and the caption
const PHONE = 600;

function pathFor(step: TourStep, caseId: string, current: string): string {
  if (step.route === "board") return "/";
  if (step.route === "new") return "/new";
  if (step.route === "case") return `/cases/${encodeURIComponent(caseId)}`;
  return current;
}

type Box = { top: number; left: number; width: number; height: number };
const sameBox = (a: Box | null, b: Box) =>
  !!a && a.top === b.top && a.left === b.left && a.width === b.width && a.height === b.height;

export default function Tour() {
  const t = useTutorial();
  const tRef = useRef(t);
  tRef.current = t;
  const step = t.step;
  const nav = useNavigate();
  const loc = useLocation();
  const doctorId = useDoctorId();
  const [hole, setHole] = useState<Box | null>(null);
  const [vp, setVp] = useState({ w: window.innerWidth, h: window.innerHeight });
  const capRef = useRef<HTMLDivElement>(null);
  const [cap, setCap] = useState({ w: 340, h: 170 });

  const path = step ? pathFor(step, t.caseId, loc.pathname) : null;
  const blockedNext = step?.waitFor === "doctorId" && !doctorId;

  // Open the step's page.
  useEffect(() => {
    if (path && path !== loc.pathname) nav(path);
  }, [path]); // eslint-disable-line react-hooks/exhaustive-deps

  // Find and follow the target until the step changes.
  useEffect(() => {
    if (!step) return;
    setHole(null);
    const skip = () => {
      const cur = tRef.current;
      if (cur.dir === -1 && cur.index > 0) cur.back();
      else cur.next();
    };
    // Skip soon once the page (and the case record) has loaded without the
    // target; wait longer while it is still loading.
    const GRACE = 1500;
    const LOAD_TIMEOUT = 8000;
    let lastSeen = Date.now();
    let readyAt: number | null = null;
    let shown = false;
    let done = false;

    const tick = () => {
      if (done) return;
      setVp((v) => (v.w === window.innerWidth && v.h === window.innerHeight ? v : { w: window.innerWidth, h: window.innerHeight }));
      // The notice + header bar is sticky. Scroll targets out from under it,
      // and never spotlight the part of a target it covers.
      const bar = Math.round(document.querySelector(".app-top")?.getBoundingClientRect().bottom ?? 0);
      document.documentElement.style.scrollPaddingTop = `${bar + PAD + 4}px`;
      const want = pathFor(step, tRef.current.caseId, window.location.pathname);
      const onPage = window.location.pathname === want;
      let tabReady = true;
      if (onPage && step.tab) {
        // The case view may reset its tab once the record loads, so keep checking.
        const tab = document.querySelector<HTMLElement>(`[data-tour="tab-${step.tab}"]`);
        tabReady = tab?.getAttribute("aria-selected") === "true";
        if (tab && !tabReady) tab.click();
      }
      const loaded = step.route !== "case" || !!document.querySelector('[data-tour="suspected"]');
      if (onPage && loaded && tabReady) readyAt ??= Date.now();
      const el = onPage ? document.querySelector<HTMLElement>(`[data-tour="${step.target}"]`) : null;
      let r = el?.getBoundingClientRect();
      if (el && r && r.width > 0 && r.height > 0) {
        if (!shown) {
          shown = true;
          el.scrollIntoView({ block: r.height > (window.innerHeight - bar) * 0.7 ? "start" : "center" });
          r = el.getBoundingClientRect();
        }
        lastSeen = Date.now();
        readyAt = null;
        const top = el.closest(".app-top") ? r.top : Math.max(r.top, bar);
        const b = { top: Math.round(top), left: Math.round(r.left), width: Math.round(r.width), height: Math.round(r.bottom - top) };
        setHole((h) => (sameBox(h, b) ? h : b));
      } else {
        if (shown) {
          shown = false;
          setHole(null);
        }
        const now = Date.now();
        if ((readyAt !== null && now - readyAt > GRACE) || now - lastSeen > LOAD_TIMEOUT) {
          done = true;
          skip();
        }
      }
    };

    tick();
    const iv = window.setInterval(tick, 200);
    window.addEventListener("scroll", tick, true);
    window.addEventListener("resize", tick);
    return () => {
      done = true;
      document.documentElement.style.scrollPaddingTop = "";
      window.clearInterval(iv);
      window.removeEventListener("scroll", tick, true);
      window.removeEventListener("resize", tick);
    };
  }, [step, t.index, t.dir, t.caseId]);

  // Measure the caption so it can be placed without overflowing.
  useLayoutEffect(() => {
    const el = capRef.current;
    if (!el) return;
    const w = el.offsetWidth;
    const h = el.offsetHeight;
    if (w !== cap.w || h !== cap.h) setCap({ w, h });
  });

  // Move focus into the caption (or the doctor ID field) when a step appears.
  const shownStep = hole ? step?.id : null;
  useEffect(() => {
    if (!shownStep) return;
    if (step?.waitFor === "doctorId" && !doctorId) {
      document.querySelector<HTMLInputElement>('[data-tour="doctor-id"] input')?.focus();
    } else {
      capRef.current?.querySelector<HTMLElement>("[data-primary]")?.focus();
    }
  }, [shownStep]); // eslint-disable-line react-hooks/exhaustive-deps

  // Esc closes, arrows move, Tab stays inside the caption (plus the doctor ID field on that step).
  useEffect(() => {
    if (!step) return;
    const onKey = (e: KeyboardEvent) => {
      const cur = tRef.current;
      const typing = (e.target as HTMLElement)?.matches?.("input, textarea, select");
      if (e.key === "Escape") {
        e.preventDefault();
        cur.stop();
      } else if (e.key === "ArrowRight" && !typing) {
        e.preventDefault();
        if (!(cur.step?.waitFor === "doctorId" && !doctorId)) cur.next();
      } else if (e.key === "ArrowLeft" && !typing) {
        e.preventDefault();
        cur.back();
      } else if (e.key === "Tab") {
        const inside = [
          ...(cur.step?.waitFor === "doctorId" ? Array.from(document.querySelectorAll<HTMLElement>('[data-tour="doctor-id"] input')) : []),
          ...Array.from(capRef.current?.querySelectorAll<HTMLElement>("button:not(:disabled)") ?? []),
        ];
        if (!inside.length) return;
        const i = inside.indexOf(document.activeElement as HTMLElement);
        e.preventDefault();
        const n = i < 0 ? 0 : (i + (e.shiftKey ? -1 : 1) + inside.length) % inside.length;
        inside[n].focus();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [step, doctorId]);

  if (!step) return null;

  // Spotlight rectangle, clamped to the viewport.
  let s = hole && {
    top: Math.max(0, hole.top - PAD),
    left: Math.max(0, hole.left - PAD),
    right: Math.min(vp.w, hole.left + hole.width + PAD),
    bottom: Math.min(vp.h, hole.top + hole.height + PAD),
  };

  // Caption position: below, else above, else beside. If none fits (a target
  // taller than the screen, like the tree or the audit log), the spotlight is
  // cut short so the caption fits below it. The caption never covers the spotlight.
  const MIN_HOLE = 120;
  let capStyle: CSSProperties;
  if (!s) {
    capStyle = { top: Math.max(16, (vp.h - cap.h) / 2), left: Math.max(16, (vp.w - cap.w) / 2) };
  } else if (vp.w < PHONE) {
    const need = cap.h + 32;
    if (vp.h - s.bottom < need && s.top < need) s = { ...s, bottom: Math.max(s.top + MIN_HOLE, vp.h - need) };
    capStyle = vp.h - s.bottom >= need || s.top < need ? { bottom: 16, left: 16, right: 16 } : { top: 16, left: 16, right: 16 };
  } else {
    const left = Math.min(Math.max(16, s.left), vp.w - cap.w - 16);
    const sideTop = Math.min(Math.max(16, s.top), vp.h - cap.h - 16);
    if (s.bottom + GAP + cap.h <= vp.h - 16) capStyle = { top: s.bottom + GAP, left };
    else if (s.top - GAP - cap.h >= 16) capStyle = { top: s.top - GAP - cap.h, left };
    else if (s.right + GAP + cap.w <= vp.w - 16) capStyle = { top: sideTop, left: s.right + GAP };
    else if (s.left - GAP - cap.w >= 16) capStyle = { top: sideTop, left: s.left - GAP - cap.w };
    else {
      s = { ...s, bottom: Math.max(s.top + MIN_HOLE, vp.h - 16 - cap.h - GAP) };
      capStyle = { top: Math.min(s.bottom + GAP, vp.h - cap.h - 16), left };
    }
  }

  const last = t.index === t.total - 1;
  const interactive = step.waitFor === "doctorId";

  return (
    <div className="tour" aria-live="polite">
      {s ? (
        <>
          <div className="tour-dim" style={{ top: 0, left: 0, right: 0, height: s.top }} />
          <div className="tour-dim" style={{ top: s.bottom, left: 0, right: 0, bottom: 0 }} />
          <div className="tour-dim" style={{ top: s.top, left: 0, width: s.left, height: s.bottom - s.top }} />
          <div className="tour-dim" style={{ top: s.top, left: s.right, right: 0, height: s.bottom - s.top }} />
          {!interactive && <div className="tour-block" style={{ top: s.top, left: s.left, width: s.right - s.left, height: s.bottom - s.top }} />}
          <div className="tour-ring" style={{ top: s.top, left: s.left, width: s.right - s.left, height: s.bottom - s.top }} />
        </>
      ) : (
        <div className="tour-dim" style={{ inset: 0 }} />
      )}
      <div
        ref={capRef}
        className={`tour-cap ${vp.w < PHONE && s ? "docked" : ""}`}
        style={capStyle}
        role="dialog"
        aria-modal="true"
        aria-labelledby="tour-title"
        aria-describedby="tour-body"
      >
        {s ? (
          <>
            <div className="tour-count">
              Step {t.index + 1} of {t.total}
            </div>
            <h4 id="tour-title">{step.title}</h4>
            <p id="tour-body">{step.body}</p>
            {blockedNext && <p className="tour-wait">Enter a doctor ID in the highlighted field to continue.</p>}
            <div className="tour-actions">
              <button className="btn ghost small" onClick={t.stop}>
                End tour
              </button>
              <span className="spacer" />
              <button className="btn ghost small" onClick={t.back} disabled={t.index === 0}>
                Back
              </button>
              <button className="btn small" data-primary onClick={t.next} disabled={blockedNext}>
                {last ? "Finish" : "Next"}
              </button>
            </div>
          </>
        ) : (
          <>
            <h4 id="tour-title">Opening the next step…</h4>
            <p id="tour-body" className="muted">
              Steps with nothing to show on this page are skipped.
            </p>
            <div className="tour-actions">
              <span className="spacer" />
              <button className="btn ghost small" data-primary onClick={t.stop}>
                End tour
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
