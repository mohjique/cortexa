import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";
import { TOUR_CASE_ID } from "./exampleCase";
import { STEPS, type TourStep } from "./steps";

// Set once the welcome card is dismissed (tour taken or skipped). If storage
// is blocked (private window), reads fail and the card simply shows again.
const SEEN_KEY = "cortexa.tutorial.v1";

function readSeen(): boolean {
  try {
    return localStorage.getItem(SEEN_KEY) === "1";
  } catch {
    return false;
  }
}

function writeSeen() {
  try {
    localStorage.setItem(SEEN_KEY, "1");
  } catch {
    /* storage blocked: the card shows again next visit */
  }
}

// The case steps always open the built-in example case (exampleCase.ts, from
// synthetic patient demo-016 at the shortlist stage), never one of the
// visitor's cases. A new workspace has no cases, and a real case may not have
// reached the stage a step points at, so every visitor sees the same 11 steps.
// It loads from the bundle, so the tour starts at once even on a cold backend.

interface Tutorial {
  active: boolean;
  index: number;
  step: TourStep | null;
  total: number;
  /** Direction of the last move, so a missing target is skipped the same way. */
  dir: 1 | -1;
  /** The case the case steps open: always the example case. */
  caseId: string;
  seen: boolean;
  start: () => void;
  next: () => void;
  back: () => void;
  stop: () => void;
  dismissWelcome: () => void;
}

const Ctx = createContext<Tutorial | null>(null);

export function TutorialProvider({ children }: { children: ReactNode }) {
  const [active, setActive] = useState(false);
  const [index, setIndex] = useState(0);
  const [dir, setDir] = useState<1 | -1>(1);
  const [seen, setSeen] = useState(readSeen);
  const returnFocus = useRef<HTMLElement | null>(null);

  const dismissWelcome = useCallback(() => {
    writeSeen();
    setSeen(true);
  }, []);

  const start = useCallback(() => {
    dismissWelcome();
    returnFocus.current = document.activeElement as HTMLElement | null;
    setIndex(0);
    setDir(1);
    setActive(true);
  }, [dismissWelcome]);

  const stop = useCallback(() => {
    setActive(false);
    returnFocus.current?.focus?.();
  }, []);

  const next = useCallback(() => {
    if (index + 1 >= STEPS.length) return stop();
    setDir(1);
    setIndex(index + 1);
  }, [index, stop]);

  const back = useCallback(() => {
    if (index === 0) return;
    setDir(-1);
    setIndex(index - 1);
  }, [index]);

  const value = useMemo<Tutorial>(
    () => ({
      active,
      index,
      step: active ? STEPS[index] : null,
      total: STEPS.length,
      dir,
      caseId: TOUR_CASE_ID,
      seen,
      start,
      next,
      back,
      stop,
      dismissWelcome,
    }),
    [active, index, dir, seen, start, next, back, stop, dismissWelcome],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useTutorial(): Tutorial {
  const t = useContext(Ctx);
  if (!t) throw new Error("useTutorial needs a TutorialProvider");
  return t;
}
