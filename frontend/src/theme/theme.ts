import { useEffect, useState } from "react";

export type ThemePreference = "light" | "dark" | "system";
export type ResolvedTheme = "light" | "dark";

const STORAGE_KEY = "cb-onboarding-theme";
const media = () => window.matchMedia?.("(prefers-color-scheme: light)");

function readPreference(): ThemePreference {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === "light" || stored === "dark" || stored === "system") return stored;
  } catch {
    // storage unavailable (private mode) -- fall back to the system setting
  }
  return "system";
}

function resolve(pref: ThemePreference): ResolvedTheme {
  if (pref !== "system") return pref;
  return media()?.matches ? "light" : "dark";
}

function apply(theme: ResolvedTheme) {
  document.documentElement.dataset.theme = theme;
}

/** Theme preference (light / dark / follow the OS), persisted per browser.
 * index.html applies the stored choice before first paint to avoid a flash. */
export function useTheme() {
  const [preference, setPreference] = useState<ThemePreference>(readPreference);
  const [resolved, setResolved] = useState<ResolvedTheme>(() => resolve(readPreference()));

  useEffect(() => {
    const next = resolve(preference);
    setResolved(next);
    apply(next);
    try {
      localStorage.setItem(STORAGE_KEY, preference);
    } catch {
      // ignore
    }
    if (preference !== "system") return;
    const mq = media();
    if (!mq) return;
    const onChange = () => {
      const r = resolve("system");
      setResolved(r);
      apply(r);
    };
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [preference]);

  return { preference, resolved, setPreference };
}
