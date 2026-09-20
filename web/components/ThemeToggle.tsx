"use client";

import { useEffect, useState } from "react";

type Theme = "system" | "light" | "dark";

/** Theme choice, remembered per browser.
 *
 *  localStorage can throw (private windows, blocked site data), so every access
 *  is guarded and the page renders correctly when it comes back empty — the OS
 *  preference is the fallback, which is the right default anyway. */
export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>("system");

  useEffect(() => {
    try {
      const stored = localStorage.getItem("airadar-theme") as Theme | null;
      if (stored === "light" || stored === "dark") setTheme(stored);
    } catch {
      /* storage unavailable; the OS preference stands */
    }
  }, []);

  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
    try {
      if (theme === "system") localStorage.removeItem("airadar-theme");
      else localStorage.setItem("airadar-theme", theme);
    } catch {
      /* nothing to remember, nothing to do */
    }
  }, [theme]);

  const next: Record<Theme, Theme> = { system: "dark", dark: "light", light: "system" };
  const glyph: Record<Theme, string> = { system: "◐", dark: "●", light: "○" };
  const title: Record<Theme, string> = {
    system: "Tema: sistem",
    dark: "Tema: koyu",
    light: "Tema: açık",
  };

  return (
    <button
      type="button"
      onClick={() => setTheme(next[theme])}
      title={title[theme]}
      aria-label={title[theme]}
      className="border-border text-ink-secondary hover:bg-surface-2 rounded-md border px-2 py-1 text-sm"
    >
      {glyph[theme]}
    </button>
  );
}
