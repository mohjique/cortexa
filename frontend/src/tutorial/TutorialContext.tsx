import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";
import { api } from "../api";
import type { Stage } from "../types";
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

// Which case the case steps open: one that has reached the shortlist if there
// is one, so the most steps have something to point at. Steps whose target is
// missing on the chosen case are skipped by the overlay.
const STAGE_RANK: Stage[] = ["review_shortlist", "no_match", "enter_results", "awaiting_pathology", "confirm_bucket"];

async function pickCase(): Promise<string | null> {
  try {
    const cases = await api.listCases();
    if (!cases.length) return null;
    const rank = (s: Stage) => {
      const i = STAGE_RANK.indexOf(s);
      return i < 0 ? STAGE_RANK.length : i;
    };
    return [...cases].sort((a, b) => rank(a.stage) - rank(b.stage))[0].case_id;
  } catch {
    return null;
  }
}

interface Tutorial {
  active: boolean;
  index: number;
  step: TourStep | null;
  total: number;
  /** Direction of the last move, so a missing target is skipped the same way. */
  dir: 1 | -1;
  caseId: string | null;
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
  const [caseId, setCaseId] = useState<string | null>(null);
  const [seen, setSeen] = useState(readSeen);
  const returnFocus = useRef<HTMLElement | null>(null);

  const dismissWelcome = useCallback(() => {
    writeSeen();
    setSeen(true);
  }, []);

  const start = useCallback(() => {
    dismissWelcome();
    returnFocus.current = document.activeElement as HTMLElement | null;
    pickCase().then((id) => {
      setCaseId(id);
      setIndex(0);
      setDir(1);
      setActive(true);
    });
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
      caseId,
      seen,
      start,
      next,
      back,
      stop,
      dismissWelcome,
    }),
    [active, index, dir, caseId, seen, start, next, back, stop, dismissWelcome],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useTutorial(): Tutorial {
  const t = useContext(Ctx);
  if (!t) throw new Error("useTutorial needs a TutorialProvider");
  return t;
}
