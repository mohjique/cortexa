import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";

/** Fetch on mount, on `deps` change, and every `intervalMs` while the tab is visible. */
export function usePoll<T>(fn: () => Promise<T>, deps: unknown[], intervalMs = 10000) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(true);
  const fnRef = useRef(fn);
  fnRef.current = fn;

  const reload = useCallback(async () => {
    try {
      const d = await fnRef.current();
      setData(d);
      setError(null);
    } catch (e) {
      setError(e as Error);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    setLoading(true);
    setData(null);
    reload();
    if (!intervalMs) return;
    const t = setInterval(() => {
      if (document.visibilityState === "visible") reload();
    }, intervalMs);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { data, error, loading, reload };
}

// The doctor identity recorded on every gate decision. There is no login in
// the demo build; the ID is typed in the header and kept in this browser only.
const KEY = "cortexa.doctor_id";

function readStored(): string {
  try {
    return localStorage.getItem(KEY) ?? "";
  } catch {
    return "";
  }
}

export function useDoctorIdState() {
  const [id, setId] = useState<string>(readStored);
  const update = (v: string) => {
    setId(v);
    try {
      localStorage.setItem(KEY, v);
    } catch {
      /* storage blocked: keep it in memory */
    }
  };
  return [id, update] as const;
}

export const DoctorContext = createContext<string>("");
export const useDoctorId = () => useContext(DoctorContext).trim();
