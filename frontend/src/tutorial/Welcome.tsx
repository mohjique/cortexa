import { useEffect, useRef } from "react";
import { useTutorial } from "./TutorialContext";
import { WELCOME } from "./steps";

/** First-visit card: what Cortexa does, then take the tour or skip it. */
export default function Welcome() {
  const t = useTutorial();
  const take = useRef<HTMLButtonElement>(null);
  const show = !t.seen && !t.active;

  useEffect(() => {
    if (!show) return;
    take.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") t.dismissWelcome();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [show]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!show) return null;
  return (
    <div className="tour">
      <div className="tour-dim" style={{ inset: 0 }} />
      <div className="tour-cap welcome" role="dialog" aria-modal="true" aria-labelledby="welcome-title">
        <h4 id="welcome-title">{WELCOME.title}</h4>
        {WELCOME.lines.map((l) => (
          <p key={l}>{l}</p>
        ))}
        <div className="tour-actions">
          <span className="spacer" />
          <button className="btn ghost small" onClick={t.dismissWelcome}>
            Skip
          </button>
          <button ref={take} className="btn small" onClick={t.start}>
            Take the tour
          </button>
        </div>
      </div>
    </div>
  );
}
