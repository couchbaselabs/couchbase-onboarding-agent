import { useCallback, useEffect, useState } from "react";

export type Theme = "light" | "dark";

// Keep in sync with public/theme-init.js, which sets data-theme on <html>
// before React mounts so there's no flash of the wrong theme.
const STORAGE_KEY = "cb-onboarding-theme";

export const DEFAULT_THEME: Theme = "light";

/** The saved choice, if any. Earlier versions could also store "system";
 * that (or anything else) now falls back to the default. */
export function getStoredTheme(): Theme | null {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    return stored === "light" || stored === "dark" ? stored : null;
  } catch {
    return null;
  }
}

// Light unless someone picked dark with the toggle. The OS setting is
// deliberately not consulted -- same as the Agent Operations Manager -- so a
// console that people demo, screenshot and support each other through looks
// the same on every machine.
export function getPreferredTheme(): Theme {
  return getStoredTheme() ?? DEFAULT_THEME;
}

export function applyTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme;
}

function persistTheme(theme: Theme) {
  try {
    localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    // Best effort: the theme just won't persist across reloads.
  }
}

export function useTheme() {
  const [theme, setThemeState] = useState<Theme>(() => getPreferredTheme());

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  const setTheme = useCallback((next: Theme) => {
    setThemeState(next);
    persistTheme(next);
  }, []);

  const toggleTheme = useCallback(() => {
    setTheme(theme === "dark" ? "light" : "dark");
  }, [theme, setTheme]);

  return { theme, setTheme, toggleTheme };
}
