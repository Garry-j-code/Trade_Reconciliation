import { NavLink, Outlet } from "react-router-dom";
import { useEffect, useRef, useState } from "react";
import { ApiError, runRecon } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import type { ReconRunResponse } from "../api/types";
import { useTheme } from "../ThemeContext";
import type { ThemePreference } from "../theme";
import { ChangePasswordDialog } from "./ChangePasswordDialog";

const APPEARANCE_OPTIONS: { value: ThemePreference; label: string }[] = [
  { value: "system", label: "System" },
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
];

export function Layout() {
  const { logout, config, displayName, changePassword } = useAuth();
  const { preference, setPreference } = useTheme();
  const [menuOpen, setMenuOpen] = useState(false);
  const [passwordOpen, setPasswordOpen] = useState(false);
  const [running, setRunning] = useState(false);
  const [reconMsg, setReconMsg] = useState<string | null>(null);
  const [reconErr, setReconErr] = useState<string | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  const authDisabled = Boolean(config?.authDisabled);
  const identity = authDisabled ? "Local analyst" : displayName;

  useEffect(() => {
    function onDocClick(event: MouseEvent): void {
      if (!menuRef.current?.contains(event.target as Node)) {
        setMenuOpen(false);
      }
    }
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, []);

  async function onRunRecon() {
    setRunning(true);
    setReconMsg(null);
    setReconErr(null);
    try {
      const result: ReconRunResponse = await runRecon({ mode: "rematch" });
      setReconMsg(
        `Recon finished in ${result.elapsed_seconds.toFixed(1)}s — ${result.match_count} matches, ${result.break_count} breaks, ${result.normalized_rows} trades rematched.`,
      );
    } catch (err) {
      const detail = err instanceof ApiError ? err.detail : "Recon run failed";
      setReconErr(
        detail.includes("Missing broker trades")
          ? "Could not rematch the current book. Confirm the API can reach the trade database, then try again."
          : detail,
      );
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="layout">
      <header className="topbar">
        <NavLink to="/" className="brand">
          <span className="brand-mark" aria-hidden />
          <span className="brand-full">Trade Reconciliation</span>
          <span className="brand-short">Trade Recon</span>
        </NavLink>
        <nav className="nav">
          <NavLink to="/" end className={({ isActive }) => (isActive ? "active" : "")}>
            Dashboard
          </NavLink>
          <NavLink to="/breaks" className={({ isActive }) => (isActive ? "active" : "")}>
            Breaks
          </NavLink>
        </nav>
        <div className="topbar-actions">
          <div className="account-menu" ref={menuRef}>
            <button
              className="account-trigger"
              type="button"
              aria-expanded={menuOpen}
              onClick={() => setMenuOpen((open) => !open)}
            >
              <span className="analyst-chip">{identity}</span>
              <span className="account-caret" aria-hidden>
                ▾
              </span>
            </button>
            {menuOpen && (
              <div className="account-dropdown" role="menu">
                <div className="account-theme" role="group" aria-label="Appearance">
                  <p className="account-theme-label">Appearance</p>
                  {APPEARANCE_OPTIONS.map((option) => (
                    <button
                      key={option.value}
                      className={`account-item${preference === option.value ? " is-active" : ""}`}
                      type="button"
                      role="menuitemradio"
                      aria-checked={preference === option.value}
                      onClick={() => setPreference(option.value)}
                    >
                      {option.label}
                    </button>
                  ))}
                </div>
                {authDisabled ? (
                  <p className="muted account-note">
                    Change password is unavailable in local auth-disabled mode.
                  </p>
                ) : (
                  <button
                    className="account-item"
                    type="button"
                    role="menuitem"
                    onClick={() => {
                      setMenuOpen(false);
                      setPasswordOpen(true);
                    }}
                  >
                    Change password
                  </button>
                )}
                {config && !config.authDisabled && (
                  <button
                    className="account-item"
                    type="button"
                    role="menuitem"
                    onClick={() => {
                      setMenuOpen(false);
                      logout();
                    }}
                  >
                    Sign out
                  </button>
                )}
              </div>
            )}
          </div>
          <button className="btn btn-primary" onClick={onRunRecon} disabled={running}>
            {running ? (
              "Running…"
            ) : (
              <>
                <span className="btn-label-full">Run reconciliation</span>
                <span className="btn-label-short">Run recon</span>
              </>
            )}
          </button>
        </div>
      </header>
      <main className="main">
        {reconErr && <div className="banner error">{reconErr}</div>}
        {reconMsg && <div className="banner ok">{reconMsg}</div>}
        <Outlet />
      </main>
      {!authDisabled && (
        <ChangePasswordDialog
          open={passwordOpen}
          onClose={() => setPasswordOpen(false)}
          onSubmit={changePassword}
        />
      )}
    </div>
  );
}
